"""Classify provider failure text into three buckets. This module is safety critical.

A call to one of the operator's accounts came back with an error. Exactly one question
matters: *does this mean the pool is spent, or not?* Getting it wrong is expensive in
both directions, and both directions have already happened in production:

``EXHAUSTED_WITH_DEADLINE``
    ``"You've hit your session limit · resets 1pm (America/Los_Angeles)"`` -- the pool is
    spent and we know when it comes back. The text names a wall-clock time and a
    timezone, but **no date and no UTC offset**, so resolving it means picking the next
    future occurrence of that wall time in that zone, across day rollover and across DST.
    Also ``agy``'s relative form: ``"Individual quota reached. ... Resets in 25m54s"``.
    If the time cannot be parsed the deadline is **unknown** -- never "now". Returning
    "now" would un-bench the account immediately and burn the next call on a certain
    failure.

``EXHAUSTED_WITHOUT_DEADLINE``
    ``"You've reached your Fable 5 limit"`` -- one *model class* is spent and the message
    carries no reset time at all. Other classes on that same account stay routable, so
    this must not be widened into an account-level block; the caller needs a re-probe
    policy instead. :attr:`FailureClassification.scope` says which case this is.

``TRANSIENT`` (never exhaustion)
    ``"Not logged in · Please run /login"`` -- an OAuth refresh race between concurrent
    headless spawns. The account is completely fine and an immediate retry succeeds.
    Misreading this as exhaustion benched a healthy account and produced ~44 spurious
    failures in production, which is why the login-race check runs *first*, before any
    exhaustion pattern, and why a bare ``rate_limit`` / HTTP 429 with no quota wording is
    also transient rather than exhaustion.

Anything unrecognized is :data:`UNKNOWN` -- deliberately a fourth outcome, not a default
into one of the three. Guessing "exhausted" on unknown text benches healthy accounts;
guessing "transient" retries into a wall. The caller decides.

Under ``claude -p --output-format json`` the limit text is **not** on stderr: it arrives
in ``.result`` with ``is_error: true`` and ``api_error_status: 429``.
:func:`classify_cli_json` reads it from the parsed payload; :func:`classify_failure_text`
takes raw text; :func:`classify_output` accepts either.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "EXHAUSTED_WITH_DEADLINE",
    "EXHAUSTED_WITHOUT_DEADLINE",
    "TRANSIENT",
    "UNKNOWN",
    "SCOPE_ACCOUNT",
    "SCOPE_MODEL_CLASS",
    "FailureClassification",
    "classify_failure_text",
    "classify_cli_json",
    "classify_output",
    "extract_failure_text",
    "parse_relative_duration_s",
    "next_occurrence_epoch",
    "epoch_from_iso",
    "TZ_ABBREVIATIONS",
]


# ======================================================================================
# Buckets
# ======================================================================================

#: Pool is spent; :attr:`FailureClassification.exhausted_until_s` may still be ``None``
#: when the message carried a reset we could not parse.
EXHAUSTED_WITH_DEADLINE: Final[str] = "exhausted_with_deadline"
#: Pool (or one model class of it) is spent and the message carries no reset at all.
EXHAUSTED_WITHOUT_DEADLINE: Final[str] = "exhausted_without_deadline"
#: Not exhaustion. Retry is expected to succeed.
TRANSIENT: Final[str] = "transient"
#: Could not tell. Not one of the three buckets -- the caller decides what to do.
UNKNOWN: Final[str] = "unknown"

#: The whole account is blocked.
SCOPE_ACCOUNT: Final[str] = "account"
#: Only one model class is blocked; the account stays routable for other classes.
SCOPE_MODEL_CLASS: Final[str] = "model_class"


@dataclass(frozen=True, slots=True)
class FailureClassification:
    """What one provider error means for routing.

    Args:
        kind: One of the four module constants.
        exhausted_until_s: Epoch seconds the pool comes back, when known. ``None`` means
            "unknown", which is *not* the same as "now" -- a caller must apply its own
            cooldown rather than retrying immediately.
        scope: :data:`SCOPE_ACCOUNT` or :data:`SCOPE_MODEL_CLASS` for the exhaustion
            kinds; ``None`` otherwise.
        model_class: Normalized class the limit applies to (``"fable"``), when scoped.
        model_label: The label exactly as the provider wrote it (``"Fable 5"``).
        reason: Human-readable explanation, safe to log.
        matched: Name of the rule that fired, for debugging the classifier itself.
    """

    kind: str
    exhausted_until_s: float | None = None
    scope: str | None = None
    model_class: str | None = None
    model_label: str | None = None
    reason: str = ""
    matched: str = ""

    @property
    def is_exhaustion(self) -> bool:
        """``True`` for either exhausted bucket."""
        return self.kind in (EXHAUSTED_WITH_DEADLINE, EXHAUSTED_WITHOUT_DEADLINE)

    @property
    def is_transient(self) -> bool:
        """``True`` when the account is healthy and the call should simply be retried."""
        return self.kind == TRANSIENT

    @property
    def blocks_account(self) -> bool:
        """``True`` only when the *whole* account should be benched.

        A model-class exhaustion must never bench the account: the other classes are
        still routable, and the whole point of per-model windows is to keep serving them.
        """
        return self.is_exhaustion and self.scope == SCOPE_ACCOUNT

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        return {
            "kind": self.kind,
            "exhausted_until_s": self.exhausted_until_s,
            "scope": self.scope,
            "model_class": self.model_class,
            "model_label": self.model_label,
            "reason": self.reason,
            "matched": self.matched,
        }


# ======================================================================================
# Timezones
# ======================================================================================

#: Abbreviations the vendors actually print, mapped to IANA zones. ``CST`` is genuinely
#: ambiguous worldwide (US Central vs China Standard); these messages come from US
#: vendors, so it resolves to America/Chicago. A full IANA name in the text is always
#: preferred over this table.
TZ_ABBREVIATIONS: Final[Mapping[str, str]] = {
    "utc": "UTC",
    "gmt": "UTC",
    "z": "UTC",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "bst": "Europe/London",
    "cet": "Europe/Paris",
    "cest": "Europe/Paris",
    "ist": "Asia/Kolkata",
    "jst": "Asia/Tokyo",
    "aest": "Australia/Sydney",
    "aedt": "Australia/Sydney",
}


def _resolve_zone(name: str | None) -> ZoneInfo | None:
    """Resolve an IANA name or a common abbreviation to a zone, or ``None``.

    A missing tzdata database (a slim Linux image) resolves to ``None`` -- which becomes
    an unknown deadline, never a guessed one.
    """
    if not name:
        return None
    raw = name.strip().strip("()").strip()
    if not raw:
        return None
    candidates = [raw, TZ_ABBREVIATIONS.get(raw.casefold(), "")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
            continue
    return None


def next_occurrence_epoch(
    hour: int, minute: int, zone: ZoneInfo, now_s: float
) -> float | None:
    """Epoch seconds of the next *future* ``hour:minute`` wall time in ``zone``.

    The provider gives a wall clock and a zone but no date, so "1pm" at 14:00 local means
    tomorrow, not four hours ago -- day rollover is the common case, not an edge case.

    DST is handled by trying both ``fold`` values on each candidate day and taking the
    earliest instant that is still in the future: on the ambiguous hour of a fall-back
    that yields the first (earlier) 1:30am rather than benching an extra hour, and on a
    nonexistent spring-forward wall time Python's shifted instant is still ordered
    correctly against the alternatives.

    Returns ``None`` if the wall time is out of range. Strictly-future semantics mean a
    message that arrives a moment *after* its own reset resolves to tomorrow; that is the
    conservative reading, since the alternative is un-benching a pool that has not
    actually rolled over yet.
    """
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    try:
        now_local = datetime.fromtimestamp(now_s, tz=zone)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return None

    best: float | None = None
    for day_offset in range(0, 3):
        day = now_local.date() + timedelta(days=day_offset)
        for fold in (0, 1):
            candidate = datetime.combine(day, time(hour, minute), tzinfo=zone).replace(fold=fold)
            try:
                stamp = candidate.timestamp()
            except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
                continue
            if stamp > now_s and (best is None or stamp < best):
                best = stamp
        if best is not None:
            return best
    return best


# ======================================================================================
# Text normalization
# ======================================================================================

_SMART_QUOTES: Final[Mapping[int, str]] = {
    ord("’"): "'",
    ord("‘"): "'",
    ord("“"): '"',
    ord("”"): '"',
    ord("·"): " . ",  # the "·" separator the CLIs use
    ord("•"): " . ",
    ord("—"): " - ",
    ord("–"): " - ",
    ord(" "): " ",
}


def _clean(text: str) -> str:
    """De-smarten and collapse whitespace, **preserving case**.

    Case must survive: ``ZoneInfo`` keys are case-sensitive (``America/Los_Angeles``
    resolves, ``america/los_angeles`` does not), and the model label is echoed back to
    the operator as the provider wrote it. Every pattern therefore matches with
    :data:`re.IGNORECASE` against this string rather than against a casefolded one.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_SMART_QUOTES)
    return re.sub(r"\s+", " ", folded).strip()


# ======================================================================================
# Patterns
# ======================================================================================

#: The OAuth refresh race. Checked before everything else: this text is emitted by a
#: perfectly healthy account and has, in production, also arrived alongside a 429.
_TRANSIENT_LOGIN_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("login_race:not_logged_in", r"\bnot logged in\b"),
    ("login_race:run_login", r"please run\s*`?/login`?"),
    ("login_race:must_login", r"\byou must be logged in\b"),
    ("login_race:please_login", r"\bplease log ?in\b"),
    ("login_race:refresh", r"\b(oauth|token) refresh (failed|race)\b"),
)

#: Quota wording. Note what is *absent*: "rate limit" on its own never appears here.
_EXHAUSTION_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("limit:your_x_limit", r"\b(?:hit|reached|exceeded)\s+your\s+(.{1,40}?)\s+limit\b"),
    ("limit:quota_reached", r"\bquota\s+(?:reached|exceeded|exhausted)\b"),
    ("limit:out_of_quota", r"\b(?:out of|no remaining)\s+(?:quota|credits?)\b"),
    ("limit:usage_limit", r"\busage limit\s+(?:reached|exceeded)\b"),
    ("limit:limit_reached", r"\b(?:session|weekly|daily|monthly)\s+limit\s+(?:reached|exceeded)\b"),
    ("limit:upgrade", r"\bupgrade to (?:increase|raise) your (?:usage )?limit\b"),
)

#: Not exhaustion, whatever the HTTP status says.
_TRANSIENT_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("transient:rate_limit", r"\brate[_ ]limit(?:_error|ed|s)?\b"),
    ("transient:429", r"\b429\b"),
    ("transient:overloaded", r"\boverloaded(?:_error)?\b"),
    ("transient:5xx", r"\b(?:500|502|503|504|529)\b"),
    ("transient:unavailable", r"\b(?:temporarily unavailable|service unavailable)\b"),
    ("transient:network", r"\b(?:econnreset|econnrefused|etimedout|socket hang ?up|connection reset|fetch failed)\b"),
    ("transient:timeout", r"\b(?:timed out|timeout)\b"),
)

#: Labels that make ``"reached your <label> limit"`` a *throttle*, not exhaustion.
#: ``"You have reached your rate limit"`` is 429 wording; treating it as a spent pool
#: would bench a healthy account, the expensive direction of the two.
_THROTTLE_LABELS: Final[frozenset[str]] = frozenset({"rate", "concurrent", "concurrency"})

#: Labels that name the *account's* limit rather than a model class.
_ACCOUNT_SCOPE_LABELS: Final[frozenset[str]] = frozenset(
    {
        "session",
        "usage",
        "weekly",
        "daily",
        "monthly",
        "account",
        "plan",
        "rate",
        "message",
        "request",
        "token",
        "credit",
        "credits",
        "individual",
        "organization",
        "spend",
        "5-hour",
        "five-hour",
        "5 hour",
        "current",
    }
)

#: ``resets in 25m54s`` / ``resets in 1h 5m`` / ``try again in 45 minutes``.
_RELATIVE_RESET_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:resets?|try again|retry|available again|back)\s+in\s+([0-9][0-9a-z .]*)",
    re.IGNORECASE,
)

#: ``resets 1pm (America/Los_Angeles)`` / ``resets at 13:00 PT``. The ``(?!in\b)`` guard
#: keeps the relative form from being read as an absolute clock time. The zone may be
#: parenthesized, a bare IANA name, or an abbreviation from :data:`TZ_ABBREVIATIONS`.
_ABSOLUTE_RESET_RE: Final[re.Pattern[str]] = re.compile(
    r"resets?\s+(?:at\s+)?(?!in\b)"
    r"(\d{1,2})(?::(\d{2}))?\s*"
    r"(a\.?m\.?|p\.?m\.?)?\s*"
    r"(?:\(([^)]{1,40})\)|\b([A-Za-z_]+/[A-Za-z_+\-]+)\b|\b([A-Za-z]{2,4})\b)?",
    re.IGNORECASE,
)

#: Units may butt straight up against the next number -- ``25m54s`` is one duration, not
#: a stray ``54s``. A trailing ``\b`` would break exactly there (``m`` followed by ``5``
#: is not a word boundary), so the guard is "not followed by another letter", which still
#: keeps ``5 mango`` from parsing as five minutes.
_DURATION_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])",
    re.IGNORECASE,
)

_DURATION_SCALE: Final[Mapping[str, float]] = {
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
}


def parse_relative_duration_s(text: str) -> float | None:
    """Seconds encoded by a duration phrase (``"25m54s"``, ``"1h 5m"``, ``"2 hours"``).

    Returns ``None`` when nothing parses, so the caller reports an unknown deadline
    rather than a fabricated one.
    """
    total = 0.0
    found = False
    for amount, unit in _DURATION_RE.findall(_clean(text)):
        scale = _DURATION_SCALE.get(unit.casefold())
        if scale is None:
            continue
        try:
            total += float(amount) * scale
        except ValueError:  # pragma: no cover - regex guarantees a number
            continue
        found = True
    return total if found and total > 0 else None


def _normalize_model_label(label: str) -> tuple[str | None, str | None]:
    """``"Fable 5"`` -> ``("fable", "Fable 5")``; account-level labels -> ``(None, label)``.

    A trailing version token is dropped so the class lines up with the window names the
    oracle publishes (cswap scopes its window by ``"Fable"``, not ``"Fable 5"``).
    """
    cleaned = label.strip().strip("'\"").strip()
    if not cleaned:
        return None, None
    folded = cleaned.casefold()
    if folded in _ACCOUNT_SCOPE_LABELS:
        return None, cleaned
    tokens = [token for token in re.split(r"\s+", folded) if token]
    while tokens and re.fullmatch(r"\d+(?:\.\d+)?", tokens[-1]):
        tokens.pop()
    if not tokens:
        return None, cleaned
    if any(token in _ACCOUNT_SCOPE_LABELS for token in tokens):
        return None, cleaned
    return "-".join(tokens), cleaned


def _resolve_deadline(
    cleaned: str, now_s: float, *, default_zone: ZoneInfo | None
) -> tuple[float | None, bool, str]:
    """``(deadline, a reset was mentioned, explanation)``.

    The middle value is what separates "no reset in the message" (bucket 2) from "a reset
    we could not parse" (bucket 1 with an unknown deadline). Neither path ever falls back
    to ``now_s``.
    """
    relative = _RELATIVE_RESET_RE.search(cleaned)
    if relative:
        seconds = parse_relative_duration_s(relative.group(1))
        if seconds is not None:
            return now_s + seconds, True, f"resets in {seconds:g}s"
        return None, True, "relative reset present but unparseable"

    absolute = _ABSOLUTE_RESET_RE.search(cleaned)
    if absolute:
        raw_hour, raw_minute, meridiem, paren_zone, iana_zone, abbrev_zone = absolute.groups()
        try:
            hour = int(raw_hour)
        except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
            return None, True, "absolute reset present but unparseable"
        minute = int(raw_minute) if raw_minute else 0
        if meridiem:
            marker = meridiem.replace(".", "").casefold()
            if marker == "pm" and hour != 12:
                hour += 12
            elif marker == "am" and hour == 12:
                hour = 0
        zone = _resolve_zone(paren_zone or iana_zone or abbrev_zone) or default_zone
        if zone is None:
            return None, True, "reset time given without a resolvable timezone"
        deadline = next_occurrence_epoch(hour, minute, zone, now_s)
        if deadline is None:
            return None, True, "absolute reset present but out of range"
        return deadline, True, f"next {hour:02d}:{minute:02d} in {zone.key}"

    if re.search(r"\bresets?\b", cleaned, re.IGNORECASE):
        return None, True, "reset mentioned but in an unrecognized format"
    return None, False, "no reset time in the message"


def classify_failure_text(
    text: str | None,
    now_s: float,
    *,
    default_tz: str | None = None,
) -> FailureClassification:
    """Classify raw provider error text. Never raises.

    Args:
        text: Whatever the provider printed (stderr, a ``.result`` string, a log line).
        now_s: Epoch seconds "now" -- passed in, never read from the clock, so relative
            deadlines and day-rollover are deterministic and testable.
        default_tz: Zone to assume when a message gives a clock time but no zone. Left
            ``None`` on purpose: guessing the operator's zone would silently shift a
            deadline by hours, so with no default the deadline stays unknown.

    Order matters and is the whole safety story: the login race is checked before any
    exhaustion pattern (it has arrived carrying a 429), exhaustion is checked before the
    generic rate-limit patterns (so a real limit is not downgraded to a retry), and
    nothing falls through into a bucket by default.
    """
    if not text or not str(text).strip():
        return FailureClassification(
            kind=UNKNOWN, reason="empty failure text", matched="none"
        )

    cleaned = _clean(str(text))

    for name, pattern in _TRANSIENT_LOGIN_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return FailureClassification(
                kind=TRANSIENT,
                reason=(
                    "OAuth refresh race under concurrent spawns; the account is healthy "
                    "and an immediate retry succeeds"
                ),
                matched=name,
            )

    for name, pattern in _EXHAUSTION_PATTERNS:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if not match:
            continue

        model_class: str | None = None
        model_label: str | None = None
        if match.groups():
            captured = match.group(1)
            if captured:
                if captured.strip().casefold() in _THROTTLE_LABELS:
                    # "reached your rate limit" is throttle wording, not a spent pool.
                    break
                model_class, model_label = _normalize_model_label(captured)

        deadline, has_reset, detail = _resolve_deadline(
            cleaned, now_s, default_zone=_resolve_zone(default_tz)
        )
        scope = SCOPE_MODEL_CLASS if model_class else SCOPE_ACCOUNT
        if has_reset:
            return FailureClassification(
                kind=EXHAUSTED_WITH_DEADLINE,
                exhausted_until_s=deadline,
                scope=scope,
                model_class=model_class,
                model_label=model_label,
                reason=(
                    f"quota exhausted; {detail}"
                    if deadline is not None
                    else f"quota exhausted; deadline UNKNOWN ({detail})"
                ),
                matched=name,
            )
        return FailureClassification(
            kind=EXHAUSTED_WITHOUT_DEADLINE,
            exhausted_until_s=None,
            scope=scope,
            model_class=model_class,
            model_label=model_label,
            reason=(
                f"quota exhausted for {model_label or 'this account'}; no reset time given "
                f"- re-probe rather than assuming a deadline"
            ),
            matched=name,
        )

    for name, pattern in _TRANSIENT_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return FailureClassification(
                kind=TRANSIENT,
                reason="rate-limit/transport error with no quota wording; safe to retry",
                matched=name,
            )

    return FailureClassification(
        kind=UNKNOWN,
        reason="no rule matched; caller decides (do not assume exhaustion)",
        matched="none",
    )


# ======================================================================================
# JSON entry points
# ======================================================================================

#: Fields, in priority order, that carry an error message in the CLI JSON envelopes.
_ERROR_TEXT_KEYS: Final[tuple[str, ...]] = (
    "error",
    "error_message",
    "errorMessage",
    "message",
    "detail",
    "stderr",
)


def _text_from_error_field(value: Any) -> str | None:
    """Flatten an ``error`` field that may be a string or an ``{type, message}`` object."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, Mapping):
        parts = [
            str(value[key])
            for key in ("type", "code", "message", "detail")
            if isinstance(value.get(key), (str, int))
        ]
        return " ".join(parts) or None
    return None


def extract_failure_text(payload: Any) -> str | None:
    """Pull the human-readable failure text out of a parsed CLI JSON envelope.

    ``claude -p --output-format json`` puts the limit message in ``.result`` and flags it
    with ``is_error: true`` / ``api_error_status: 429`` -- it never reaches stderr -- so
    ``result`` is read only when the payload actually says it is an error. Other CLIs use
    ``error`` / ``message`` / ``stderr``, and a nested ``error`` object contributes both
    its ``type`` and its ``message`` so wording like ``rate_limit_error`` stays visible.
    """
    if isinstance(payload, str):
        return payload or None
    if not isinstance(payload, Mapping):
        return None

    parts: list[str] = []
    for key in _ERROR_TEXT_KEYS:
        if key in payload:
            text = _text_from_error_field(payload.get(key))
            if text:
                parts.append(text)

    is_error = bool(payload.get("is_error")) or payload.get("subtype") == "error_during_execution"
    result = payload.get("result")
    if is_error and isinstance(result, str) and result.strip():
        parts.append(result)

    nested = payload.get("response")
    if isinstance(nested, Mapping):
        nested_text = extract_failure_text(nested)
        if nested_text:
            parts.append(nested_text)

    combined = " . ".join(dict.fromkeys(part.strip() for part in parts if part.strip()))
    return combined or None


def classify_cli_json(
    payload: Any,
    now_s: float,
    *,
    default_tz: str | None = None,
) -> FailureClassification:
    """Classify a parsed CLI JSON envelope. Never raises.

    Adds one rule on top of :func:`classify_failure_text`: a payload flagged as an error
    with ``api_error_status`` 429 (or 5xx) but no recognizable wording is **transient**,
    not exhaustion. A bare 429 is a throttle; the quota messages always spell the limit
    out in words.
    """
    text = extract_failure_text(payload)
    classification = classify_failure_text(text, now_s, default_tz=default_tz)
    if classification.kind != UNKNOWN or not isinstance(payload, Mapping):
        return classification

    status = payload.get("api_error_status", payload.get("status"))
    if isinstance(status, bool) or not isinstance(status, (int, str)):
        return classification
    try:
        code = int(status)
    except (TypeError, ValueError):
        return classification
    if code == 429 or 500 <= code <= 599:
        return FailureClassification(
            kind=TRANSIENT,
            reason=f"HTTP {code} with no quota wording; treated as a throttle, not exhaustion",
            matched="transient:status",
        )
    return classification


def classify_output(
    value: Any,
    now_s: float,
    *,
    default_tz: str | None = None,
) -> FailureClassification:
    """Classify whatever a caller has: a mapping, a JSON string, or raw text.

    A string that parses as a JSON object is routed through :func:`classify_cli_json` so
    ``api_error_status`` is not lost; anything else is treated as plain text.
    """
    if isinstance(value, Mapping):
        return classify_cli_json(value, now_s, default_tz=default_tz)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, Mapping):
                return classify_cli_json(parsed, now_s, default_tz=default_tz)
        return classify_failure_text(value, now_s, default_tz=default_tz)
    return FailureClassification(
        kind=UNKNOWN, reason=f"unclassifiable payload type {type(value).__name__}", matched="none"
    )


# Re-exported for callers that want to build a deadline themselves (e.g. from a header).
def epoch_from_iso(text: str) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds, or ``None``."""
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
