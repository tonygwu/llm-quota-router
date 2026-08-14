"""Antigravity source: there isn't one.

Verified on the operator's machine: ``agy`` exposes no usage/quota subcommand and writes
no quota fields anywhere on disk. There is no oracle to read, no cache to tail, no
percentage to parse. Pretending otherwise -- assuming "probably half full", or deriving a
number from call counts -- would be inventing data, and inventing data is how a router
silently sends every call to an exhausted pool.

So this adapter reports exactly what is true: an **unobservable** account. Its snapshot
carries no windows and ``confidence=0.0``. The only real signal it can offer is
*failure-learned*: when a call to an Antigravity pool comes back with a quota error,
:mod:`quota_router.failure_text` extracts a deadline (``"Individual quota reached. ...
Resets in 25m54s"``) and whoever persists that hands it back here as
``exhausted_until``. Until that deadline passes, the account is marked unavailable; after
it passes, the account is routable again -- still unobservable, still confidence zero.

Two Antigravity pools sit behind the one CLI (``antigravity_gemini`` and
``antigravity_claude``, selected by ``AGY_MODEL``), and they exhaust independently, so
each gets its own snapshot and its own deadline.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from ..types import (
    ACCOUNT_ANTIGRAVITY_CLAUDE,
    ACCOUNT_ANTIGRAVITY_GEMINI,
    SOURCE_ASSUMED,
    TIER_UNKNOWN,
    AccountSnapshot,
)
from .base import ProviderAdapter, parse_timestamp, read_json_file

__all__ = [
    "ANTIGRAVITY_BINARY",
    "ANTIGRAVITY_ACCOUNT_IDS",
    "ANTIGRAVITY_STATE_ENV",
    "AntigravityAdapter",
]

#: The CLI the executor spawns for these pools. Never invoked here -- its mere presence
#: is the only thing this adapter checks, and ``shutil.which`` is a PATH lookup, not a
#: process.
ANTIGRAVITY_BINARY: Final[str] = "agy"

#: The two pools behind that one binary.
ANTIGRAVITY_ACCOUNT_IDS: Final[tuple[str, ...]] = (
    ACCOUNT_ANTIGRAVITY_GEMINI,
    ACCOUNT_ANTIGRAVITY_CLAUDE,
)

#: Optional path to a JSON file of failure-learned deadlines, written by whatever layer
#: handles failures: ``{"antigravity_gemini": 1786752000}``. Values may be epoch seconds
#: or ISO-8601. Unset means "no learned state", not an error.
ANTIGRAVITY_STATE_ENV: Final[str] = "QUOTA_ROUTER_ANTIGRAVITY_STATE"

#: Confidence for an account with no observable quota at all. Not "low" -- *none*.
UNOBSERVABLE_CONFIDENCE: Final[float] = 0.0

#: Type of the injected deadline source: a mapping or a callable per account id.
ExhaustedUntil = Mapping[str, Any] | Callable[[str], Any]


def _format_deadline(epoch_s: float) -> str:
    """Render a deadline as UTC ISO-8601 for the human-readable note."""
    try:
        return (
            datetime.fromtimestamp(epoch_s, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return str(epoch_s)


class AntigravityAdapter(ProviderAdapter):
    """Report the Antigravity pools as unobservable, plus any failure-learned deadline.

    Args:
        account_ids: Pools to report on.
        exhausted_until: Failure-learned deadlines -- a mapping ``account_id -> epoch/ISO``
            or a callable taking an account id. Values in the past (or ``None``) mean
            "not known to be exhausted".
        state_path: Explicit path to the JSON deadline file; otherwise
            :data:`ANTIGRAVITY_STATE_ENV` is consulted, and if that is unset no file is
            read at all.
        which: Injected ``shutil.which`` for the binary-presence check.
        env: Environment mapping, injected for tests.
    """

    name = "antigravity"

    def __init__(
        self,
        *,
        account_ids: Iterable[str] = ANTIGRAVITY_ACCOUNT_IDS,
        exhausted_until: ExhaustedUntil | None = None,
        state_path: Path | str | None = None,
        binary: str = ANTIGRAVITY_BINARY,
        which: Callable[[str], str | None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._account_ids = tuple(account_ids)
        self._exhausted_until = exhausted_until
        self._state_path = state_path
        self._binary = binary
        self._which = which if which is not None else shutil.which
        self._env = env
        self.warnings: tuple[str, ...] = ()

    # -- deadline resolution -------------------------------------------------------------

    def _state_file(self) -> Path | None:
        if self._state_path is not None:
            return Path(os.path.expanduser(str(self._state_path)))
        env = self._env if self._env is not None else os.environ
        raw = env.get(ANTIGRAVITY_STATE_ENV)
        if raw and raw.strip():
            return Path(os.path.expanduser(raw.strip()))
        return None

    def _deadlines(self) -> dict[str, float]:
        """Merge injected deadlines with the optional state file. Never raises."""
        merged: dict[str, float] = {}

        state_file = self._state_file()
        if state_file is not None:
            payload = read_json_file(state_file)
            if isinstance(payload, Mapping):
                source: Any = payload.get("exhausted_until", payload)
                if isinstance(source, Mapping):
                    for account_id, value in source.items():
                        deadline = parse_timestamp(value)
                        if isinstance(account_id, str) and deadline is not None:
                            merged[account_id] = deadline

        provided = self._exhausted_until
        if callable(provided):
            for account_id in self._account_ids:
                deadline = parse_timestamp(provided(account_id))
                if deadline is not None:
                    merged[account_id] = deadline
        elif isinstance(provided, Mapping):
            for account_id, value in provided.items():
                deadline = parse_timestamp(value)
                if isinstance(account_id, str) and deadline is not None:
                    merged[account_id] = deadline

        return merged

    # -- protocol -------------------------------------------------------------------------

    def snapshot(self, now_s: float) -> list[AccountSnapshot]:
        """One unobservable snapshot per pool. Never raises."""
        warnings: list[str] = []
        deadlines = self._deadlines()
        installed = self._which(self._binary) is not None
        if not installed:
            warnings.append(f"antigravity: {self._binary} not on PATH; pools marked unavailable")

        snapshots: list[AccountSnapshot] = []
        for account_id in self._account_ids:
            deadline = deadlines.get(account_id)
            exhausted = deadline is not None and deadline > now_s

            if not installed:
                note = f"{self._binary} not installed"
            elif exhausted:
                note = (
                    f"no usage API; failure-learned exhaustion until "
                    f"{_format_deadline(float(deadline))}"
                )
            elif deadline is not None:
                note = (
                    f"no usage API; last learned exhaustion expired at "
                    f"{_format_deadline(float(deadline))}"
                )
            else:
                note = "no usage API; unobservable, no failure-learned deadline"

            try:
                snapshots.append(
                    AccountSnapshot(
                        id=account_id,
                        windows=(),
                        tier=TIER_UNKNOWN,
                        source=SOURCE_ASSUMED,
                        confidence=UNOBSERVABLE_CONFIDENCE,
                        available=installed and not exhausted,
                        note=note,
                    )
                )
            except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
                warnings.append(f"antigravity: {account_id} rejected ({exc})")

        self.warnings = tuple(warnings)
        return snapshots
