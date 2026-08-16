"""Fallback Claude source: the statusline rate-limit cache on disk.

Claude Code's statusline hook writes what the vendor told it about the current account's
limits into ``~/Library/Caches/twin-networking/claude{,-b,-c}-rate-limits.json``. That
file costs nothing to read and needs no subprocess, which makes it the natural standby
for when :mod:`~quota_router.providers.claude_live usage` cannot run (binary missing, oracle
timing out, the operator offline).

Verified shape::

    {"observedAt": 1786734090784,          # milliseconds since epoch
     "provider": "claude",
     "source": "claude_status_line",
     "rate_limits": {"five_hour":  {"used_percentage": 0,  "resets_at": 1786752000},
                     "seven_day":  {"used_percentage": 60, "resets_at": 1787007600}}}

Note the mixed units in one document -- ``observedAt`` is milliseconds while
``resets_at`` is seconds -- which is why every timestamp goes through
:func:`~quota_router.providers.base.parse_timestamp`.

Unknown keys under ``rate_limits`` are passed through *generically*: an entry the writer
adds later (say a per-model ``"fable"`` bucket) becomes a model-scoped
:class:`~quota_router.types.Window` with no code change here. Being permissive is the
safe direction -- an extra window can only constrain routing further, never loosen it.

Because this is a cache, snapshots are stamped ``SOURCE_CACHE`` and their confidence
decays with age; a caller that has both this and live usage should prefer live usage.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from ..types import (
    ACCOUNT_CLAUDE,
    ACCOUNT_CLAUDE_B,
    ACCOUNT_CLAUDE_C,
    SOURCE_CACHE,
    TIER_UNKNOWN,
    AccountSnapshot,
    Window,
    normalize_model_class,
)
from .base import (
    FIVE_HOUR_S,
    SEVEN_DAY_S,
    WINDOW_KEY_5H,
    WINDOW_KEY_7D,
    ProviderAdapter,
    decay_confidence,
    make_window,
    parse_timestamp,
    pct_to_fraction,
    read_json_file,
)
from .claude_cli_config import ClaudeAccountConfig, discover_claude_configs

__all__ = [
    "DEFAULT_STATUSLINE_DIR",
    "STATUSLINE_DIR_ENV",
    "STATUSLINE_PATHS_ENV",
    "DEFAULT_STATUSLINE_FILENAMES",
    "ClaudeStatuslineAdapter",
    "parse_statusline_payload",
]

#: Where the statusline hook writes, relative to ``$HOME``.
DEFAULT_STATUSLINE_DIR: Final[str] = "Library/Caches/twin-networking"

#: Override the directory (tests, a relocated cache, XDG on Linux).
STATUSLINE_DIR_ENV: Final[str] = "QUOTA_ROUTER_STATUSLINE_DIR"
#: Override individual files: ``{"claude": "/abs/file.json", ...}`` as JSON.
STATUSLINE_PATHS_ENV: Final[str] = "QUOTA_ROUTER_STATUSLINE_PATHS"

#: Account id -> cache filename.
DEFAULT_STATUSLINE_FILENAMES: Final[Mapping[str, str]] = {
    ACCOUNT_CLAUDE: "claude-rate-limits.json",
    ACCOUNT_CLAUDE_B: "claude-b-rate-limits.json",
    ACCOUNT_CLAUDE_C: "claude-c-rate-limits.json",
}

#: A statusline file is rewritten on every render, so a few minutes old is normal and a
#: half-hour old means nobody has used that account recently -- still evidence, but weak.
_FRESH_S: Final[float] = 120.0
_STALE_S: Final[float] = 1800.0

_UNKNOWN_TIER_CONFIDENCE: Final[float] = 0.6

#: Accepted spellings for the two account-wide windows -> (canonical key, length).
_KNOWN_WINDOWS: Final[Mapping[str, tuple[str, float]]] = {
    "five_hour": (WINDOW_KEY_5H, FIVE_HOUR_S),
    "fivehour": (WINDOW_KEY_5H, FIVE_HOUR_S),
    "5h": (WINDOW_KEY_5H, FIVE_HOUR_S),
    "session": (WINDOW_KEY_5H, FIVE_HOUR_S),
    "seven_day": (WINDOW_KEY_7D, SEVEN_DAY_S),
    "sevenday": (WINDOW_KEY_7D, SEVEN_DAY_S),
    "7d": (WINDOW_KEY_7D, SEVEN_DAY_S),
    "weekly": (WINDOW_KEY_7D, SEVEN_DAY_S),
}

#: Suffixes that tell us how long an otherwise-unknown window is.
_LENGTH_SUFFIXES: Final[tuple[tuple[str, float], ...]] = (
    ("_five_hour", FIVE_HOUR_S),
    ("_fivehour", FIVE_HOUR_S),
    ("_5h", FIVE_HOUR_S),
    ("_session", FIVE_HOUR_S),
    ("_seven_day", SEVEN_DAY_S),
    ("_sevenday", SEVEN_DAY_S),
    ("_7d", SEVEN_DAY_S),
    ("_weekly", SEVEN_DAY_S),
)

#: Accepted spellings for the used-percentage field.
_USED_KEYS: Final[tuple[str, ...]] = ("used_percentage", "used_percent", "usedPct", "pct")
#: Accepted spellings for the pacing baseline, if a future writer starts emitting one.
_EXPECTED_KEYS: Final[tuple[str, ...]] = (
    "expected_percentage",
    "expected_percent",
    "expectedPct",
)
#: Accepted spellings for the reset instant.
_RESET_KEYS: Final[tuple[str, ...]] = ("resets_at", "resetsAt", "reset_at")


def _first_present(entry: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in entry:
            return entry[key]
    return None


def _explicit_length_s(entry: Mapping[str, Any]) -> float | None:
    """Window length declared by the entry itself, in whatever unit it chose."""
    for key, scale in (
        ("window_seconds", 1.0),
        ("window_length_s", 1.0),
        ("window_minutes", 60.0),
        ("window_hours", 3600.0),
    ):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value) * scale
    return None


def _resolve_window_shape(
    raw_key: str, entry: Mapping[str, Any]
) -> tuple[str, float, frozenset[str] | None]:
    """Map a ``rate_limits`` key onto ``(window key, length, applies_to)``.

    Known account-wide keys normalize onto the shared ``"5h"`` / ``"7d"`` vocabulary so
    that a statusline snapshot and a live usage snapshot describe the same window with the
    same name. Anything else is treated as model-scoped: the key minus a recognized
    window suffix is the model class, so ``"fable"`` and ``"fable_seven_day"`` both gate
    on ``fable`` while keeping distinct keys.
    """
    key = raw_key.strip().casefold()
    known = _KNOWN_WINDOWS.get(key)
    if known is not None:
        canonical, length = known
        return canonical, _explicit_length_s(entry) or length, None

    length = _explicit_length_s(entry)
    class_part = key
    for suffix, suffix_length in _LENGTH_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix):
            class_part = key[: -len(suffix)]
            if length is None:
                length = suffix_length
            break
    model_class = normalize_model_class(class_part)
    applies_to = frozenset({model_class}) if model_class else None
    return key, length or SEVEN_DAY_S, applies_to


def parse_statusline_payload(
    payload: Any,
    *,
    account_id: str,
    now_s: float,
    config: ClaudeAccountConfig | None = None,
    fallback_observed_at_s: float | None = None,
    warnings: list[str] | None = None,
) -> AccountSnapshot | None:
    """Turn one decoded statusline document into a snapshot, or ``None``. Never raises."""
    notes = warnings if warnings is not None else []
    if not isinstance(payload, Mapping):
        notes.append(f"statusline: {account_id} payload was not a JSON object")
        return None

    limits = payload.get("rate_limits")
    if not isinstance(limits, Mapping):
        notes.append(f"statusline: {account_id} had no 'rate_limits' object")
        return None

    observed_at_s = parse_timestamp(payload.get("observedAt"))
    if observed_at_s is None:
        observed_at_s = parse_timestamp(payload.get("observed_at"))
    if observed_at_s is None:
        observed_at_s = fallback_observed_at_s if fallback_observed_at_s is not None else now_s

    windows: list[Window] = []
    taken: set[str] = set()
    for raw_key, entry in limits.items():
        if not isinstance(raw_key, str) or not isinstance(entry, Mapping):
            continue
        key, length_s, applies_to = _resolve_window_shape(raw_key, entry)
        if key in taken:
            continue
        window = make_window(
            key=key,
            used_fraction=pct_to_fraction(_first_present(entry, _USED_KEYS)),
            length_s=length_s,
            resets_at_s=parse_timestamp(_first_present(entry, _RESET_KEYS)),
            observed_at_s=observed_at_s,
            applies_to=applies_to,
            expected_used_fraction=pct_to_fraction(_first_present(entry, _EXPECTED_KEYS)),
        )
        if window is None:
            notes.append(f"statusline: {account_id} dropped unusable window {raw_key!r}")
            continue
        taken.add(window.key)
        windows.append(window)

    if not windows:
        notes.append(f"statusline: {account_id} had no parseable window")
        return None

    tier = config.tier if config is not None else TIER_UNKNOWN
    confidence = decay_confidence(
        max(0.0, now_s - observed_at_s), fresh_s=_FRESH_S, stale_s=_STALE_S
    )
    if tier == TIER_UNKNOWN:
        confidence *= _UNKNOWN_TIER_CONFIDENCE

    try:
        return AccountSnapshot(
            id=account_id,
            windows=tuple(windows),
            tier=tier,
            source=SOURCE_CACHE,
            confidence=confidence,
            available=True,
            note="claude statusline cache",
            identity=config.identity if config is not None else None,
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        notes.append(f"statusline: {account_id} rejected ({type(exc).__name__}: {exc})")
        return None


class ClaudeStatuslineAdapter(ProviderAdapter):
    """Read the on-disk statusline rate-limit caches. No subprocess, ever.

    Args:
        paths: Explicit ``account_id -> file`` mapping; wins over every other source.
        directory: Directory holding the default filenames.
        home / env: Used to compute the defaults and to read the env overrides.
        configs: Pre-resolved Claude configs (for tier + identity); discovered if absent.
    """

    name = "claude_statusline"

    def __init__(
        self,
        *,
        paths: Mapping[str, Path | str] | None = None,
        directory: Path | str | None = None,
        home: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        configs: Iterable[ClaudeAccountConfig] | None = None,
    ) -> None:
        self._explicit_paths = paths
        self._directory = directory
        self._home = home
        self._env = env
        self._configs = tuple(configs) if configs is not None else None
        self.warnings: tuple[str, ...] = ()

    # -- path resolution ----------------------------------------------------------------

    def _env_map(self) -> Mapping[str, str]:
        return self._env if self._env is not None else os.environ

    def paths(self) -> dict[str, Path]:
        """Resolve ``account_id -> cache file`` from arguments, env, then defaults."""
        if self._explicit_paths is not None:
            return {
                str(key): Path(os.path.expanduser(str(value)))
                for key, value in self._explicit_paths.items()
            }

        env = self._env_map()
        raw = env.get(STATUSLINE_PATHS_ENV)
        if raw and raw.strip():
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, Mapping):
                resolved = {
                    str(key): Path(os.path.expanduser(str(value)))
                    for key, value in parsed.items()
                    if isinstance(value, str) and value.strip()
                }
                if resolved:
                    return resolved

        if self._directory is not None:
            directory = Path(os.path.expanduser(str(self._directory)))
        elif env.get(STATUSLINE_DIR_ENV):
            directory = Path(os.path.expanduser(env[STATUSLINE_DIR_ENV]))
        else:
            base = Path(self._home) if self._home is not None else Path(os.path.expanduser("~"))
            directory = base / DEFAULT_STATUSLINE_DIR
        return {
            account_id: directory / filename
            for account_id, filename in DEFAULT_STATUSLINE_FILENAMES.items()
        }

    def configs(self) -> tuple[ClaudeAccountConfig, ...]:
        """Local Claude configs, for tier and identity (cached per instance)."""
        if self._configs is None:
            self._configs = discover_claude_configs(home=self._home, env=self._env)
        return self._configs

    # -- protocol -----------------------------------------------------------------------

    def snapshot(self, now_s: float) -> list[AccountSnapshot]:
        """One snapshot per readable cache file. Never raises."""
        warnings: list[str] = []
        by_account = {config.account_id: config for config in self.configs()}
        snapshots: list[AccountSnapshot] = []

        for account_id, path in self.paths().items():
            payload = read_json_file(path)
            if payload is None:
                continue
            try:
                mtime: float | None = os.stat(path).st_mtime
            except OSError:  # pragma: no cover - the read above already succeeded
                mtime = None
            snapshot = parse_statusline_payload(
                payload,
                account_id=account_id,
                now_s=now_s,
                config=by_account.get(account_id),
                fallback_observed_at_s=mtime,
                warnings=warnings,
            )
            if snapshot is not None:
                snapshots.append(snapshot)

        self.warnings = tuple(warnings)
        return snapshots
