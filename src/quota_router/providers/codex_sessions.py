"""Codex source: the ``rate_limits`` rows inside Codex session transcripts.

The Codex CLI has no "show me my quota" command, but it writes what the API told it into
every session rollout under ``$CODEX_HOME/sessions/YYYY/MM/DD/*.jsonl``. Each assistant
turn emits an ``event_msg`` / ``token_count`` line carrying::

    "rate_limits": {"limit_id": "codex", "limit_name": null,
                    "primary": {"used_percent": 59.0, "window_minutes": 10080,
                                "resets_at": 1787196557},
                    "secondary": null, "plan_type": "pro", ...}

Verified against the live machine: ``window_minutes`` is always ``10080`` (7 days),
``secondary`` is always ``null``, and three ``limit_id`` values occur -- ``"codex"``
(the account-wide limit, ``limit_name: null``), ``"codex_bengalfox"``
(``limit_name: "GPT-5.3-Codex-Spark"``, a per-model bucket) and ``"premium"``
(``primary: null``, i.e. no window at all). Both other shapes are still handled, because
"always" here means "always so far".

**Speed is a correctness property.** These transcripts are hundreds of megabytes and this
runs on every invocation, so the adapter reads only the *tail* of only the newest handful
of files. Never call anything here that walks a whole transcript.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ..types import (
    ACCOUNT_CODEX,
    SOURCE_CACHE,
    TIER_UNKNOWN,
    AccountSnapshot,
    Identity,
    Window,
    normalize_model_class,
    normalize_tier,
)
from .base import (
    ProviderAdapter,
    decay_confidence,
    make_window,
    parse_timestamp,
    pct_to_fraction,
    read_json_file,
    read_tail_lines,
)

__all__ = [
    "CODEX_HOME_ENV",
    "DEFAULT_CODEX_HOME",
    "DEFAULT_MAX_FILES",
    "DEFAULT_TAIL_BYTES",
    "DEFAULT_LIMIT_IDS",
    "CodexSessionsAdapter",
    "window_key_for_minutes",
]

CODEX_HOME_ENV: Final[str] = "CODEX_HOME"
DEFAULT_CODEX_HOME: Final[str] = "~/.codex"

#: How many session files to look at, newest first.
DEFAULT_MAX_FILES: Final[int] = 8
#: How much of each file's tail to read. 256 KiB covers hundreds of token_count rows.
DEFAULT_TAIL_BYTES: Final[int] = 262_144
#: Stop parsing a file once this many rate-limit rows have been read from its tail.
DEFAULT_MAX_ROWS_PER_FILE: Final[int] = 400

#: ``limit_id`` values that mean "the account-wide limit", i.e. not model-scoped.
DEFAULT_LIMIT_IDS: Final[frozenset[str]] = frozenset({"codex", "default", "primary", ""})

#: Window length assumed when a row omits ``window_minutes``. Every row on the live
#: machine reports 10080 (7 days). The alternative -- dropping a window we cannot size --
#: would delete a real constraint and make Codex look unconstrained, which is the
#: dangerous direction; the length only feeds the pacing prior, and the used fraction and
#: reset instant in the row are still real.
DEFAULT_WINDOW_MINUTES: Final[float] = 10080.0

#: Cheap substring gate applied before any JSON parsing.
_ROW_MARKER: Final[str] = '"rate_limits"'

#: Session transcripts are written as the turn happens, so "fresh" is generous here: the
#: operator may simply not have run Codex for a while, which is not the same as bad data.
_FRESH_S: Final[float] = 900.0
_STALE_S: Final[float] = 6 * 3600.0


def window_key_for_minutes(minutes: float | None) -> str:
    """Human-stable window key for a window length in minutes.

    ``10080 -> "7d"`` and ``300 -> "5h"``, matching the vocabulary the Claude adapters
    use, so a reader comparing accounts sees the same names for the same shapes.
    """
    if minutes is None or minutes <= 0:
        return "window"
    whole = int(round(minutes))
    if abs(minutes - whole) > 1e-6:
        return f"{minutes:g}m"
    if whole % 1440 == 0:
        return f"{whole // 1440}d"
    if whole % 60 == 0:
        return f"{whole // 60}h"
    return f"{whole}m"


@dataclass(frozen=True, slots=True)
class _LimitObservation:
    """The newest ``rate_limits`` row seen for one ``limit_id``."""

    limit_id: str
    limit_name: str | None
    plan_type: str | None
    observed_at_s: float
    primary: Mapping[str, Any] | None
    secondary: Mapping[str, Any] | None


def _model_class_aliases(limit_id: str, limit_name: str | None) -> frozenset[str]:
    """Model classes a scoped Codex limit should gate on.

    Deliberately generous: the operator may name the class by its ``limit_id``
    (``codex_bengalfox``), by the short form (``bengalfox``) or by the marketing name
    (``GPT-5.3-Codex-Spark``). Extra aliases can only make the window *apply* more often,
    and applying a real constraint too often is safe -- dropping one is not.
    """
    aliases: set[str] = set()
    for candidate in (
        limit_id,
        limit_id.removeprefix("codex_") if limit_id.startswith("codex_") else None,
        limit_name,
    ):
        normalized = normalize_model_class(candidate)
        if normalized:
            aliases.add(normalized)
    return frozenset(aliases)


def _session_files(sessions_dir: Path, *, limit: int) -> list[Path]:
    """The ``limit`` newest ``*.jsonl`` transcripts, newest first.

    Walks ``YYYY/MM/DD`` newest-first and stops as soon as it has enough, so an archive
    with years of history costs a handful of ``listdir`` calls.
    """

    def subdirs(parent: Path) -> list[Path]:
        try:
            entries = [entry for entry in parent.iterdir() if entry.is_dir()]
        except OSError:
            return []
        return sorted(entries, key=lambda path: path.name, reverse=True)

    found: list[Path] = []
    for year in subdirs(sessions_dir):
        for month in subdirs(year):
            for day in subdirs(month):
                try:
                    files = [
                        entry
                        for entry in day.iterdir()
                        if entry.is_file() and entry.suffix == ".jsonl"
                    ]
                except OSError:
                    continue

                def sort_key(path: Path) -> tuple[float, str]:
                    try:
                        return (path.stat().st_mtime, path.name)
                    except OSError:  # pragma: no cover - race with a deleted file
                        return (0.0, path.name)

                found.extend(sorted(files, key=sort_key, reverse=True))
                if len(found) >= limit:
                    return found[:limit]
    return found[:limit]


def _rate_limit_rows(line: str) -> tuple[Mapping[str, Any], float | None] | None:
    """Extract ``(rate_limits, timestamp)`` from one transcript line, or ``None``."""
    if _ROW_MARKER not in line:
        return None
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, Mapping):
        return None
    payload = event.get("payload")
    container: Mapping[str, Any] | None = None
    if isinstance(payload, Mapping) and isinstance(payload.get("rate_limits"), Mapping):
        container = payload
    elif isinstance(event.get("rate_limits"), Mapping):
        container = event
    if container is None:
        return None
    limits = container["rate_limits"]
    if not isinstance(limits, Mapping):
        return None
    timestamp = parse_timestamp(event.get("timestamp")) or parse_timestamp(
        container.get("timestamp")
    )
    return limits, timestamp


def _collect_observations(
    files: Iterable[Path],
    *,
    tail_bytes: int,
    max_rows_per_file: int,
) -> dict[str, _LimitObservation]:
    """Newest observation per ``limit_id`` across the given files.

    Keyed on the event timestamp rather than on file order, so an out-of-order mtime (a
    resumed session, a copied transcript) cannot make a stale row win.
    """
    newest: dict[str, _LimitObservation] = {}
    for path in files:
        try:
            fallback_time: float | None = path.stat().st_mtime
        except OSError:  # pragma: no cover - race with a deleted file
            fallback_time = None
        rows = 0
        for line in reversed(read_tail_lines(path, max_bytes=tail_bytes)):
            if rows >= max_rows_per_file:
                break
            extracted = _rate_limit_rows(line)
            if extracted is None:
                continue
            rows += 1
            limits, timestamp = extracted
            limit_id = limits.get("limit_id")
            if not isinstance(limit_id, str):
                limit_id = ""
            observed = timestamp if timestamp is not None else fallback_time
            if observed is None:
                continue
            existing = newest.get(limit_id)
            if existing is not None and existing.observed_at_s >= observed:
                continue
            limit_name = limits.get("limit_name")
            plan_type = limits.get("plan_type")
            primary = limits.get("primary")
            secondary = limits.get("secondary")
            newest[limit_id] = _LimitObservation(
                limit_id=limit_id,
                limit_name=limit_name if isinstance(limit_name, str) else None,
                plan_type=plan_type if isinstance(plan_type, str) else None,
                observed_at_s=observed,
                primary=primary if isinstance(primary, Mapping) else None,
                secondary=secondary if isinstance(secondary, Mapping) else None,
            )
    return newest


def _windows_from_observation(
    observation: _LimitObservation, *, taken: set[str]
) -> list[Window]:
    """Build the ``primary`` (and, if it ever appears, ``secondary``) windows."""
    scoped = observation.limit_id.strip().casefold() not in DEFAULT_LIMIT_IDS
    applies_to = (
        _model_class_aliases(observation.limit_id, observation.limit_name) or None
        if scoped
        else None
    )

    windows: list[Window] = []
    for slot, entry in (("primary", observation.primary), ("secondary", observation.secondary)):
        if not isinstance(entry, Mapping):
            continue
        minutes = entry.get("window_minutes")
        minutes_value = (
            float(minutes)
            if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0
            else None
        )
        length_s = (minutes_value if minutes_value is not None else DEFAULT_WINDOW_MINUTES) * 60.0
        base_key = observation.limit_id if scoped else window_key_for_minutes(minutes_value)
        key = base_key if slot == "primary" else f"{base_key}#secondary"
        while key in taken:
            key = f"{key}#"
        window = make_window(
            key=key,
            used_fraction=pct_to_fraction(entry.get("used_percent")),
            length_s=length_s,
            resets_at_s=parse_timestamp(entry.get("resets_at")),
            observed_at_s=observation.observed_at_s,
            applies_to=applies_to,
        )
        if window is None:
            continue
        taken.add(window.key)
        windows.append(window)
    return windows


def _identity_from_auth(auth_path: Path) -> tuple[Identity | None, str | None]:
    """Best-effort identity + plan from ``$CODEX_HOME/auth.json``.

    The file holds an OAuth ``id_token``; its *payload* segment is plain base64url JSON
    carrying the account email, the ChatGPT account id and the plan type. Only those
    three fields are read, the token itself is never logged or transmitted, and the
    signature is deliberately not verified -- this is a local hint for identity keying,
    not an authentication decision. Any failure returns ``(None, None)``.
    """
    payload = read_json_file(auth_path)
    if not isinstance(payload, Mapping):
        return None, None
    tokens = payload.get("tokens")
    token = tokens.get("id_token") if isinstance(tokens, Mapping) else None
    if not isinstance(token, str) or token.count(".") != 2:
        return None, None
    body = token.split(".")[1]
    body += "=" * (-len(body) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(body).decode("utf-8", errors="replace"))
    except (ValueError, TypeError, binascii.Error):
        return None, None
    if not isinstance(claims, Mapping):
        return None, None
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, None
    auth_claims = claims.get("https://api.openai.com/auth")
    account_id = ""
    plan: str | None = None
    if isinstance(auth_claims, Mapping):
        raw_account = auth_claims.get("chatgpt_account_id")
        if isinstance(raw_account, str):
            account_id = raw_account
        raw_plan = auth_claims.get("chatgpt_plan_type")
        if isinstance(raw_plan, str):
            plan = raw_plan
    try:
        return Identity(email=email, organization_uuid=account_id), plan
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None, plan


class CodexSessionsAdapter(ProviderAdapter):
    """Derive the Codex account's windows from recent session transcripts.

    Args:
        codex_home: ``$CODEX_HOME`` override. Defaults to the env var, then ``~/.codex``.
        max_files / tail_bytes / max_rows_per_file: the speed guardrails.
        read_identity: read ``auth.json`` for the account email (local file only).
        env: environment mapping, injected for tests.
    """

    name = "codex_sessions"

    def __init__(
        self,
        *,
        codex_home: Path | str | None = None,
        max_files: int = DEFAULT_MAX_FILES,
        tail_bytes: int = DEFAULT_TAIL_BYTES,
        max_rows_per_file: int = DEFAULT_MAX_ROWS_PER_FILE,
        read_identity: bool = True,
        account_id: str = ACCOUNT_CODEX,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._codex_home = codex_home
        self._max_files = max_files
        self._tail_bytes = tail_bytes
        self._max_rows_per_file = max_rows_per_file
        self._read_identity = read_identity
        self._account_id = account_id
        self._env = env
        self.warnings: tuple[str, ...] = ()

    def codex_home(self) -> Path:
        """Resolve ``$CODEX_HOME``."""
        if self._codex_home is not None:
            return Path(os.path.expanduser(str(self._codex_home)))
        env = self._env if self._env is not None else os.environ
        raw = env.get(CODEX_HOME_ENV) or DEFAULT_CODEX_HOME
        return Path(os.path.expanduser(raw))

    def snapshot(self, now_s: float) -> list[AccountSnapshot]:
        """Read the newest transcript tails and build one Codex snapshot. Never raises."""
        warnings: list[str] = []
        home = self.codex_home()
        sessions_dir = home / "sessions"

        files = _session_files(sessions_dir, limit=max(1, self._max_files))
        if not files:
            warnings.append(f"codex: no session transcripts under {sessions_dir}")
            self.warnings = tuple(warnings)
            return []

        observations = _collect_observations(
            files, tail_bytes=self._tail_bytes, max_rows_per_file=self._max_rows_per_file
        )
        if not observations:
            warnings.append("codex: no rate_limits rows in the newest session tails")
            self.warnings = tuple(warnings)
            return []

        windows: list[Window] = []
        taken: set[str] = set()
        # Account-wide limits first so they keep the plain "7d"/"5h" keys.
        ordered = sorted(
            observations.values(),
            key=lambda obs: (obs.limit_id.strip().casefold() not in DEFAULT_LIMIT_IDS, obs.limit_id),
        )
        for observation in ordered:
            built = _windows_from_observation(observation, taken=taken)
            if not built and observation.primary is None:
                # e.g. limit_id "premium", which reports no window at all.
                warnings.append(f"codex: limit {observation.limit_id!r} reported no window")
            windows.extend(built)

        if not windows:
            warnings.append("codex: no parseable window in any rate_limits row")
            self.warnings = tuple(warnings)
            return []

        identity: Identity | None = None
        plan_from_auth: str | None = None
        if self._read_identity:
            identity, plan_from_auth = _identity_from_auth(home / "auth.json")

        plan = next(
            (obs.plan_type for obs in ordered if obs.plan_type),
            plan_from_auth,
        )
        tier = normalize_tier(plan)
        observed_at_s = max(obs.observed_at_s for obs in observations.values())
        confidence = decay_confidence(
            max(0.0, now_s - observed_at_s), fresh_s=_FRESH_S, stale_s=_STALE_S
        )
        if tier == TIER_UNKNOWN:
            warnings.append(f"codex: unrecognized plan_type {plan!r}; capacity stays neutral")

        try:
            snapshot = AccountSnapshot(
                id=self._account_id,
                windows=tuple(windows),
                tier=tier,
                source=SOURCE_CACHE,
                confidence=confidence,
                available=True,
                note=f"codex session transcripts under {sessions_dir}",
                identity=identity,
            )
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            warnings.append(f"codex: snapshot rejected ({type(exc).__name__}: {exc})")
            self.warnings = tuple(warnings)
            return []

        self.warnings = tuple(warnings)
        return [snapshot]
