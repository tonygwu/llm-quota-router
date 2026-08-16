"""Read Claude per-account usage directly, without ever minting a credential.

WHAT THIS REPLACES AND WHY
--------------------------
This adapter supersedes the ``cswap`` oracle. The oracle was not read-only in the
way that mattered: to read usage it redeemed the account's refresh token, and
Anthropic's OAuth rotates refresh tokens -- redeeming one invalidates it and
issues a replacement. The oracle kept the replacement in its own store and never
wrote it back to the entry Claude Code reads, so Claude Code was left holding a
revoked token and blanked the login on its next launch. Two accounts were lost
that way on 2026-08-15.

The fix is not to synchronize the two copies; that only narrows the race. It is
to stop being a second redeemer at all. **Single writer:** exactly one process
may redeem a refresh token, and that process is Claude Code. This module is a
pure reader.

HOW IT WORKS
------------
1. Read the account's *access* token out of the macOS Keychain. Claude Code keeps
   one entry per config directory, named by a hash of the directory path (see
   :func:`keychain_service_for`).
2. ``GET https://api.anthropic.com/api/oauth/usage`` with that token as a plain
   bearer credential.
3. Parse the ``limits[]`` rows into windows.

Verified live on 2026-08-16 against a real account: HTTP 200, the full window set
including the model-scoped Fable row, and the refresh token fingerprint
byte-identical before and after the call. Reading usage requires no rotation.

WHAT WE GIVE UP, DELIBERATELY
-----------------------------
An access token lives about eight hours. If it has expired we report the account
as unavailable rather than mint a new one, so an account nobody has touched in a
day goes ``unknown``. That is the honest failure: the alternative is either a
guess (invents data) or a rotation (destroys the login). In practice the gap is
narrow, because any account the router routes work to is refreshed by that very
traffic -- including headless ``claude -p`` runs, which refresh exactly like
interactive sessions do.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from ..types import (
    SOURCE_LIVE,
    TIER_UNKNOWN,
    AccountSnapshot,
    Identity,
    Window,
    normalize_model_class,
)
from .base import (
    FIVE_HOUR_S,
    SEVEN_DAY_S,
    WINDOW_KEY_5H,
    WINDOW_KEY_7D,
    ProviderAdapter,
    Runner,
    make_window,
    parse_timestamp,
    pct_to_fraction,
    run_command,
)
from .claude_cli_config import ClaudeAccountConfig, discover_claude_configs

__all__ = [
    "USAGE_URL",
    "HTTP_METHOD",
    "KEYCHAIN_SERVICE_BASE",
    "keychain_service_for",
    "read_access_token",
    "usage_cache_path",
    "parse_usage_payload",
    "ClaudeOAuthAdapter",
]

#: The endpoint Claude Code itself reads for ``/usage``. Undocumented and
#: therefore drift-prone: parse it strictly and fail loud, never defensively.
USAGE_URL: Final[str] = "https://api.anthropic.com/api/oauth/usage"

#: Read-only by construction. A POST here would be a token exchange; there is no
#: code path in this package that performs one.
HTTP_METHOD: Final[str] = "GET"

#: Keychain service name for the default config directory (``~/.claude``).
KEYCHAIN_SERVICE_BASE: Final[str] = "Claude Code-credentials"

#: The container key Claude Code stores its OAuth blob under.
_OAUTH_KEY: Final[str] = "claudeAiOauth"

#: Unknown tier means we cannot convert "percent used" into "tokens available",
#: so the reading is real but less actionable. Mirrors the prior adapter.
_UNKNOWN_TIER_CONFIDENCE: Final[float] = 0.6

#: How long a fetched usage payload is reused before we ask again. The endpoint
#: rate-limits (observed: HTTP 429 under development-rate probing), and the router
#: is invoked once per LLM call across N accounts, so an uncached read would
#: throttle itself almost immediately. Usage moves on the scale of minutes; a
#: two-minute reuse window costs no meaningful accuracy.
DEFAULT_USAGE_TTL_S: Final[float] = 120.0

#: How long a *stale* cached payload may still be served when the endpoint is
#: unreachable or throttling. Beyond this we report unavailable rather than route
#: on numbers old enough to be wrong.
DEFAULT_USAGE_STALE_MAX_S: Final[float] = 900.0

#: ``limits[].kind`` -> (window key, window length). ``weekly_scoped`` is handled
#: separately because its key comes from the model it is scoped to.
_KIND_TO_WINDOW: Final[dict[str, tuple[str, float]]] = {
    "session": (WINDOW_KEY_5H, FIVE_HOUR_S),
    "weekly_all": (WINDOW_KEY_7D, SEVEN_DAY_S),
}

_SCOPED_KIND: Final[str] = "weekly_scoped"


def keychain_service_for(config_dir: Path | str, *, home: Path | str | None = None) -> tuple[str, ...]:
    """Candidate Keychain service names for a config directory, best first.

    Claude Code names the default directory's entry ``Claude Code-credentials``
    and every other directory's ``Claude Code-credentials-<first 8 hex of the
    SHA-256 of the absolute directory path>``. Verified against all three of this
    operator's accounts:

    ``~/.claude-b`` -> ``6bf31a73``, ``~/.claude-c`` -> ``8af63c1d``.

    Both candidates are returned rather than one guessed, because the mapping is
    an observed convention rather than a documented contract -- if Claude Code
    ever suffixes the default directory too, the fallback keeps us working.
    """
    resolved = Path(os.path.expanduser(str(config_dir)))
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:8]
    suffixed = f"{KEYCHAIN_SERVICE_BASE}-{digest}"
    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    if resolved == base / ".claude":
        return (KEYCHAIN_SERVICE_BASE, suffixed)
    return (suffixed, KEYCHAIN_SERVICE_BASE)


def usage_cache_path(
    account_id: str,
    env: Mapping[str, str] | None = None,
    *,
    home: Path | str | None = None,
) -> Path:
    """Where one account's last usage payload is cached.

    Mirrors the history/state resolver: ``QUOTA_ROUTER_USAGE_CACHE`` override, then
    ``$XDG_STATE_HOME/quota-router/usage``, then ``~/.local/state/quota-router/usage``.

    ``home`` overrides the resolved home directory. An adapter constructed against
    a synthetic home must cache inside it -- otherwise a test writes into, and
    reads back from, the operator's real cache, which both pollutes their state and
    makes the test pass or fail depending on what the machine did five minutes ago.
    """
    environ = os.environ if env is None else env
    override = (environ.get("QUOTA_ROUTER_USAGE_CACHE") or "").strip()
    if override:
        base = Path(os.path.expanduser(override))
    else:
        xdg = (environ.get("XDG_STATE_HOME") or "").strip()
        if home is not None:
            base = Path(home) / ".local" / "state"
        elif xdg:
            base = Path(os.path.expanduser(xdg))
        else:
            base = Path(environ.get("HOME") or str(Path.home())) / ".local" / "state"
        base = base / "quota-router" / "usage"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in account_id)
    return base / f"{safe}.json"


def _read_usage_cache(path: Path) -> tuple[Any | None, float | None]:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(blob, Mapping):
        return None, None
    fetched_at = blob.get("fetched_at_s")
    payload = blob.get("payload")
    if not isinstance(fetched_at, (int, float)) or payload is None:
        return None, None
    return payload, float(fetched_at)


def _write_usage_cache(path: Path, payload: Any, fetched_at_s: float) -> None:
    """Best-effort cache write. A cache that cannot be written is not an error --
    the next call simply re-fetches."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"fetched_at_s": fetched_at_s, "payload": payload}), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        pass


@dataclass(frozen=True, slots=True)
class TokenRead:
    """The outcome of looking for one account's access token."""

    token: str | None
    expires_at_s: float | None
    service: str | None
    problem: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.token) and self.problem is None


def read_access_token(
    config_dir: Path | str,
    *,
    runner: Runner | None = None,
    home: Path | str | None = None,
    timeout_s: float = 10.0,
    now_s: float | None = None,
) -> TokenRead:
    """Read the access token for one config directory out of the Keychain.

    Never returns a partial success: a blank token is a *problem*, not an empty
    string to pass along. The blank case is exactly what a rotation casualty
    looks like -- Claude Code zeroes the token in place rather than deleting the
    entry -- so it must be reported, not silently treated as "no data".
    """
    hard_failure: str | None = None
    for service in keychain_service_for(config_dir, home=home):
        result = run_command(
            ["security", "find-generic-password", "-s", service, "-w"],
            runner=runner,
            timeout_s=timeout_s,
        )
        if result.returncode is None:
            # ``run_command`` flattens every failure into a CommandOutcome, so the
            # only thing separating "the tool never ran" from "the tool ran and
            # found nothing" is whether a returncode exists at all. A missing
            # binary, a timeout, or a Keychain prompt with no one to answer it all
            # land here -- categorically different from "this account has no
            # entry", and collapsing the two sends the operator to re-login an
            # account that was never logged out.
            hard_failure = hard_failure or (
                f"could not run `security` (missing binary, timeout, or a Keychain "
                f"prompt with no one to answer it): {result.error}"
            )
            continue
        if result.returncode != 0:
            continue
        raw = (result.stdout or "").strip()
        if not raw:
            continue
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            return TokenRead(None, None, service, "keychain entry is not JSON")
        oauth = blob.get(_OAUTH_KEY) if isinstance(blob, Mapping) else None
        if not isinstance(oauth, Mapping):
            return TokenRead(None, None, service, f"keychain entry has no {_OAUTH_KEY} block")
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not token:
            return TokenRead(
                None, None, service,
                "access token is blank -- this account is logged out; run `claude` "
                "in that config dir and `/login`",
            )
        expires_at_s = parse_timestamp(oauth.get("expiresAt"))
        if expires_at_s is not None and now_s is not None and expires_at_s <= now_s:
            return TokenRead(
                token, expires_at_s, service,
                "access token expired; it will be renewed the next time this account "
                "is used, and we will not renew it ourselves",
            )
        return TokenRead(token, expires_at_s, service)
    return TokenRead(None, None, None, hard_failure or "no Keychain entry found for this config dir")


def _fetch_usage(token: str, *, timeout_s: float, opener: Any = None) -> tuple[Any | None, str | None]:
    """GET the usage endpoint. Returns ``(payload, error)`` -- never raises."""
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "llm-quota-router/1",
        },
        method=HTTP_METHOD,
    )
    fetch = opener or urllib.request.urlopen
    try:
        with fetch(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode()), None
    except urllib.error.HTTPError as exc:
        detail = "401 unauthorized (token rejected)" if exc.code == 401 else f"HTTP {exc.code}"
        return None, detail
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"unparseable usage payload ({type(exc).__name__}: {exc})"


def _model_class_for(entry: Mapping[str, Any]) -> str | None:
    scope = entry.get("scope")
    if not isinstance(scope, Mapping):
        return None
    model = scope.get("model")
    if not isinstance(model, Mapping):
        return None
    for field in ("display_name", "name", "id"):
        value = model.get(field)
        if isinstance(value, str) and value.strip():
            return normalize_model_class(value)
    return None


def parse_usage_payload(payload: Any, *, observed_at_s: float) -> tuple[list[Window], list[str]]:
    """Turn one ``/api/oauth/usage`` response into windows.

    Only ``limits[]`` is read. The top-level ``five_hour`` / ``seven_day`` blocks
    carry the same numbers, but ``limits[]`` states each row's ``kind`` and
    ``scope`` explicitly, so one uniform code path covers the account-wide and
    model-scoped windows instead of two that can drift apart.

    No ``expected_used_fraction`` is available from this endpoint, so
    :class:`Window` derives the pacing baseline from ``resets_at`` and the window
    length. That reproduces what the old oracle supplied: against a live capture,
    its ``expectedPct`` of 0.405 over a 7-day window with 4.2 days remaining
    matches the derived 4.2/7 = 0.60 complement to within a percentage point.
    """
    warnings: list[str] = []
    if not isinstance(payload, Mapping):
        return [], ["usage payload is not a JSON object"]
    rows = payload.get("limits")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return [], ["usage payload has no limits[] array"]

    windows: list[Window] = []
    taken: set[str] = set()
    for index, entry in enumerate(rows):
        if not isinstance(entry, Mapping):
            continue
        kind = entry.get("kind")
        applies_to: frozenset[str] | None = None
        if kind in _KIND_TO_WINDOW:
            key, length_s = _KIND_TO_WINDOW[kind]
        elif kind == _SCOPED_KIND:
            model_class = _model_class_for(entry)
            # A scoped row we cannot name is kept account-wide on purpose:
            # dropping it would delete a real constraint and flatter the account.
            key = model_class or "scoped"
            length_s = SEVEN_DAY_S
            applies_to = frozenset({model_class}) if model_class else None
        else:
            warnings.append(f"unrecognized limits[] kind {kind!r}; window ignored")
            continue
        if key in taken:
            key = f"{key}#{index + 1}"
        window = make_window(
            key=key,
            used_fraction=pct_to_fraction(entry.get("percent")),
            length_s=length_s,
            resets_at_s=parse_timestamp(entry.get("resets_at")),
            observed_at_s=observed_at_s,
            applies_to=applies_to,
        )
        if window is None:
            warnings.append(f"limits[] row {kind!r} lacked percent or resets_at; dropped")
            continue
        taken.add(window.key)
        windows.append(window)
    return windows, warnings


class ClaudeOAuthAdapter(ProviderAdapter):
    """Per-account Claude usage, read with the token the account already holds.

    Args:
        runner: Injected process runner for the Keychain read. Tests inject.
        opener: Injected URL opener. Tests inject; nothing here touches the
            network under test.
        timeout_s: Budget for each Keychain read and each HTTP call.
        home / config_dirs / env: Passed through to config discovery.
        configs: Pre-resolved configs, for callers that already did discovery.
    """

    #: Stable identifier used to attribute warnings back to this adapter. Without
    #: it ``collect_snapshots`` falls back to the class name and every warning is
    #: filed under the wrong source.
    name = "claude_oauth"

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        opener: Any = None,
        timeout_s: float = 10.0,
        ttl_s: float = DEFAULT_USAGE_TTL_S,
        stale_max_s: float = DEFAULT_USAGE_STALE_MAX_S,
        env: Mapping[str, str] | None = None,
        home: Path | str | None = None,
        configs: Sequence[ClaudeAccountConfig] | None = None,
    ) -> None:
        self._runner = runner
        self._opener = opener
        self._timeout_s = timeout_s
        self._ttl_s = ttl_s
        self._stale_max_s = stale_max_s
        self._env = env
        self._home = home
        self._configs = tuple(configs) if configs is not None else None

    @property
    def configs(self) -> tuple[ClaudeAccountConfig, ...]:
        if self._configs is None:
            self._configs = discover_claude_configs(home=self._home, env=self._env)
        return self._configs

    def snapshot(self, now_s: float) -> list[AccountSnapshot]:
        snapshots: list[AccountSnapshot] = []
        for config in self.configs:
            snapshots.extend(self._snapshot_one(config, now_s))
        return snapshots

    def _snapshot_one(self, config: ClaudeAccountConfig, now_s: float) -> list[AccountSnapshot]:
        if not config.config_dir.exists():
            return []
        read = read_access_token(
            config.config_dir,
            runner=self._runner,
            home=self._home,
            timeout_s=self._timeout_s,
            now_s=now_s,
        )
        identity = config.identity
        label = identity.email if identity and identity.email else config.account_id

        if not read.usable:
            # Unavailable, not absent, and never guessed at: the operator needs to
            # see *which* account went dark and why.
            return [
                AccountSnapshot(
                    id=config.account_id,
                    windows=(),
                    tier=config.tier,
                    source=SOURCE_LIVE,
                    confidence=0.0,
                    available=False,
                    note=f"{label}: {read.problem}",
                    identity=identity,
                )
            ]

        cache_path = usage_cache_path(config.account_id, self._env, home=self._home)
        cached, cached_at = _read_usage_cache(cache_path)

        payload: Any | None = None
        observed_at_s = now_s
        extra: list[str] = []

        if cached is not None and cached_at is not None and (now_s - cached_at) < self._ttl_s:
            payload, observed_at_s = cached, cached_at
        else:
            payload, error = _fetch_usage(
                read.token or "", timeout_s=self._timeout_s, opener=self._opener
            )
            if error is None and payload is not None:
                _write_usage_cache(cache_path, payload, now_s)
            elif cached is not None and cached_at is not None and (now_s - cached_at) < self._stale_max_s:
                # Endpoint unreachable or throttling, but we have a recent reading.
                # Serving it beats going dark; saying so beats pretending it is fresh.
                payload, observed_at_s = cached, cached_at
                extra.append(f"usage endpoint unavailable ({error}); served cache")
            else:
                return [
                    AccountSnapshot(
                        id=config.account_id,
                        windows=(),
                        tier=config.tier,
                        source=SOURCE_LIVE,
                        confidence=0.0,
                        available=False,
                        note=f"{label}: usage read failed ({error})",
                        identity=identity,
                    )
                ]

        windows, warnings = parse_usage_payload(payload, observed_at_s=observed_at_s)
        warnings.extend(extra)
        if not windows:
            return [
                AccountSnapshot(
                    id=config.account_id,
                    windows=(),
                    tier=config.tier,
                    source=SOURCE_LIVE,
                    confidence=0.0,
                    available=False,
                    note=f"{label}: usage payload carried no usable window "
                    f"({'; '.join(warnings) or 'no reason given'})",
                    identity=identity,
                )
            ]

        confidence = 1.0
        notes = [f"live usage endpoint; config dir {config.config_dir}"]
        if observed_at_s < now_s:
            notes.append(f"reading is {now_s - observed_at_s:.0f}s old (cached)")
        if config.tier == TIER_UNKNOWN:
            confidence *= _UNKNOWN_TIER_CONFIDENCE
            notes.append("tier unknown; capacity neutral, confidence lowered")
        notes.extend(warnings)

        return [
            AccountSnapshot(
                id=config.account_id,
                windows=tuple(windows),
                tier=config.tier,
                source=SOURCE_LIVE,
                confidence=confidence,
                available=True,
                note="; ".join(notes),
                identity=identity,
            )
        ]
