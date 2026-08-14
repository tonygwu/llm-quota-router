"""Cross-invocation state: sticky incumbents and pileup reservations.

Two things must survive between invocations, and neither may ever be allowed to delay or
break one:

**Sticky incumbents.** Which account served the last call for a given model class, so the
selection layer can apply hysteresis and stop the router from flapping between two pools
whose slack differs by a rounding error.

**Pileup reservations.** ``{account, at_s, cost}`` per pick. Ten processes launched at
the same instant all read the *same* oracle snapshot, all conclude "claude_b has the most
slack", and all pile onto claude_b -- the snapshot cannot show a burn that has not been
reported yet. Subtracting recent reservations from an account's remaining budget before
scoring makes concurrent callers spread out.

Failure policy
--------------
State is an optimization. The router must answer even when the state file is locked,
corrupt, unreadable or on a full disk, so **every** function here degrades instead of
raising:

* the advisory lock has a **hard 2 second** budget; on timeout we proceed *statelessly*
  -- no sticky incumbent is reported, no reservation is written, nothing blocks;
* a corrupt or unreadable file is reported as a warning and treated as empty;
* writes are ``tmp + fsync + os.replace``, so a crash mid-write can never leave a
  half-written state file behind.

Location: ``$QUOTA_ROUTER_STATE`` overrides everything, else
``$XDG_STATE_HOME/quota-router/state.json``, else ``~/.local/state/quota-router/state.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Final, Iterator

try:  # pragma: no cover - POSIX everywhere this router runs
    import fcntl
except ImportError:  # pragma: no cover - Windows: no advisory locking available
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "STATE_VERSION",
    "DEFAULT_LOCK_TIMEOUT_S",
    "GLOBAL_SCOPE",
    "StickyEntry",
    "Reservation",
    "StateSnapshot",
    "StateWrite",
    "StateStore",
    "state_path",
]

#: On-disk schema version. Anything else is discarded rather than misread.
STATE_VERSION: Final[int] = 1

#: Hard ceiling on advisory-lock acquisition. Not a suggestion: a router that blocks is
#: worse than a router that forgets, because the caller is a human's CLI invocation.
DEFAULT_LOCK_TIMEOUT_S: Final[float] = 2.0

#: Sticky scope used when the caller did not name a model class.
GLOBAL_SCOPE: Final[str] = "*"

_LOCK_POLL_S: Final[float] = 0.01
_MAX_STICKY_SCOPES: Final[int] = 32


# ======================================================================================
# Location
# ======================================================================================


def state_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the state file path.

    ``$QUOTA_ROUTER_STATE`` wins; if it names an existing directory the file is placed
    inside it, otherwise it *is* the file. Then ``$XDG_STATE_HOME``, then the XDG default
    ``~/.local/state``.
    """
    environ = os.environ if env is None else env

    override = (environ.get("QUOTA_ROUTER_STATE") or "").strip()
    if override:
        path = Path(_expand(override, environ))
        return path / "state.json" if path.is_dir() else path

    xdg = (environ.get("XDG_STATE_HOME") or "").strip()
    if xdg:
        base = Path(_expand(xdg, environ))
    else:
        home = environ.get("HOME") or str(Path.home())
        base = Path(home) / ".local" / "state"
    return base / "quota-router" / "state.json"


def _expand(raw: str, env: Mapping[str, str]) -> str:
    """``~`` expansion against the *given* environment (never ``os.environ``)."""
    text = raw.strip()
    if text == "~" or text.startswith("~/"):
        home = env.get("HOME") or str(Path.home())
        return home + text[1:]
    return text


# ======================================================================================
# Records
# ======================================================================================


@dataclass(frozen=True, slots=True)
class StickyEntry:
    """The incumbent for one sticky scope."""

    account: str
    at_s: float
    scope: str = GLOBAL_SCOPE

    def is_fresh(self, now_s: float, ttl_s: float) -> bool:
        """``True`` while the incumbent is still within its time-to-live.

        A negative age (a state file written by a machine whose clock later moved back)
        counts as fresh: it is a clock artifact, not evidence the incumbent is stale.
        """
        if ttl_s <= 0.0:
            return False
        return (now_s - self.at_s) <= ttl_s

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping."""
        return {"account": self.account, "at_s": self.at_s, "scope": self.scope}


@dataclass(frozen=True, slots=True)
class Reservation:
    """One in-flight call's claim on an account's budget.

    Args:
        account: Account id the call was routed to.
        at_s: When the pick happened (epoch seconds).
        cost: Fraction of that account's window budget the call is expected to burn,
            i.e. ``demand_multiplier / calls_per_window``.
    """

    account: str
    at_s: float
    cost: float

    def is_active(self, now_s: float, window_s: float) -> bool:
        """``True`` while this reservation should still be subtracted."""
        age = now_s - self.at_s
        return -_CLOCK_SLOP_S <= age <= window_s

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping."""
        return {"account": self.account, "at_s": self.at_s, "cost": self.cost}


#: Tolerated backwards clock skew for reservations written "in the future".
_CLOCK_SLOP_S: Final[float] = 5.0


# ======================================================================================
# Snapshot
# ======================================================================================


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """An immutable read of the state file.

    Args:
        path: Where it was read from (or would have been).
        sticky: Scope -> incumbent.
        reservations: Recent picks, oldest first.
        stateless: ``True`` when state was skipped entirely -- lock timeout, disabled, or
            an unreadable file. A stateless snapshot reports no incumbent and no
            reservations, and the caller must not treat that as "nothing is running".
        warnings: Why anything above degraded.
    """

    path: Path | None = None
    sticky: Mapping[str, StickyEntry] = field(default_factory=dict)
    reservations: tuple[Reservation, ...] = ()
    stateless: bool = False
    warnings: tuple[str, ...] = ()
    #: Opaque blob owned by the selection layer (its dwell counters and rate-limit
    #: cooldowns). This module deliberately does not interpret it: selection is pure and
    #: cannot do I/O, so persistence lives here, but the *shape* stays over there.
    selection: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sticky", MappingProxyType(dict(self.sticky)))
        object.__setattr__(self, "reservations", tuple(self.reservations))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "selection", dict(self.selection))

    def incumbent(
        self, scope: str | None, now_s: float, ttl_s: float
    ) -> str | None:
        """The sticky account for ``scope``, or ``None`` if there is none/it expired.

        Scopes fall back to :data:`GLOBAL_SCOPE`: a call that names no model class may
        stick to whatever last ran, but a Fable call never inherits an opus incumbent that
        might be pinned behind an exhausted Fable-scoped window.
        """
        if self.stateless:
            return None
        key = scope or GLOBAL_SCOPE
        entry = self.sticky.get(key)
        if entry is None and key == GLOBAL_SCOPE:
            return None
        if entry is None:
            return None
        return entry.account if entry.is_fresh(now_s, ttl_s) else None

    def reserved_cost(self, account: str, now_s: float, window_s: float) -> float:
        """Total active reserved cost for one account."""
        return sum(
            reservation.cost
            for reservation in self.reservations
            if reservation.account == account and reservation.is_active(now_s, window_s)
        )

    def reserved_by_account(self, now_s: float, window_s: float) -> dict[str, float]:
        """Active reserved cost per account (only accounts with a non-zero claim)."""
        out: dict[str, float] = {}
        for reservation in self.reservations:
            if reservation.is_active(now_s, window_s) and reservation.cost:
                out[reservation.account] = out.get(reservation.account, 0.0) + reservation.cost
        return out

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping."""
        return {
            "path": None if self.path is None else str(self.path),
            "sticky": {scope: entry.to_dict() for scope, entry in self.sticky.items()},
            "reservations": [item.to_dict() for item in self.reservations],
            "selection": dict(self.selection),
            "stateless": self.stateless,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class StateWrite:
    """Outcome of a write attempt. Never an exception -- writes are best-effort."""

    ok: bool = False
    stateless: bool = False
    path: Path | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", tuple(self.warnings))


# ======================================================================================
# Store
# ======================================================================================


class StateStore:
    """Read/write access to the state file, with a hard lock budget.

    Args:
        path: Explicit state file; resolved from ``env`` when omitted.
        env: Environment used to resolve the path.
        lock_timeout_s: Hard ceiling on lock acquisition (default 2 seconds).
        enabled: ``False`` makes every operation a no-op returning stateless results --
            what ``--dry-run`` and a disabled sticky policy use.
        clock: Monotonic clock for the lock deadline, injectable for tests. This is
            deliberately *not* the ``now_s`` the decision uses: lock timing is real
            wall-clock behaviour, routing time is a parameter.
        sleep: Sleep function used while polling for the lock.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = Path(path) if path is not None else state_path(env)
        self.lock_timeout_s = max(0.0, float(lock_timeout_s))
        self.enabled = bool(enabled)
        self._clock = clock
        self._sleep = sleep

    # -- locking -------------------------------------------------------------------------

    @property
    def lock_path(self) -> Path:
        """Sidecar lock file, so locking never truncates or races the state file."""
        return self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[bool]:
        """Acquire the advisory lock, yielding whether it was acquired.

        Yields ``False`` on timeout (or when locking is unavailable) instead of raising:
        the caller then proceeds statelessly. The deadline is checked against an injected
        monotonic clock, and the loop can never run past it.
        """
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            yield True
            return

        handle = None
        acquired = False
        try:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.lock_path, "a+")  # noqa: SIM115 - closed in finally
            except OSError:
                yield False
                return

            mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            deadline = self._clock() + self.lock_timeout_s
            while True:
                try:
                    fcntl.flock(handle.fileno(), mode)
                    acquired = True
                    break
                except OSError:
                    if self._clock() >= deadline:
                        break
                    self._sleep(_LOCK_POLL_S)
            yield acquired
        finally:
            if handle is not None:
                if acquired:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:  # pragma: no cover - unlock of a dead fd
                        pass
                handle.close()

    # -- read --------------------------------------------------------------------------

    def load(self) -> StateSnapshot:
        """Read the state file, degrading to a stateless snapshot on any problem."""
        if not self.enabled:
            return StateSnapshot(path=self.path, stateless=True)

        with self._lock(exclusive=False) as acquired:
            if not acquired:
                return StateSnapshot(
                    path=self.path,
                    stateless=True,
                    warnings=(
                        f"state file busy for {self.lock_timeout_s:g}s "
                        f"({self.path}); proceeding statelessly (no sticky, no write)",
                    ),
                )
            return self._read_unlocked()

    def _read_unlocked(self) -> StateSnapshot:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return StateSnapshot(path=self.path)
        except OSError as exc:
            return StateSnapshot(
                path=self.path,
                stateless=True,
                warnings=(f"cannot read state file {self.path}: {exc}",),
            )

        if not raw.strip():
            return StateSnapshot(path=self.path)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return StateSnapshot(
                path=self.path,
                warnings=(
                    f"state file {self.path} is corrupt ({exc}); starting from empty state",
                ),
            )
        if not isinstance(data, Mapping):
            return StateSnapshot(
                path=self.path,
                warnings=(f"state file {self.path} is not an object; ignoring it",),
            )

        warnings: list[str] = []
        version = data.get("version")
        if version != STATE_VERSION:
            return StateSnapshot(
                path=self.path,
                warnings=(
                    f"state file {self.path} has version {version!r}, expected "
                    f"{STATE_VERSION}; ignoring it",
                ),
            )

        selection = data.get("selection")
        return StateSnapshot(
            path=self.path,
            sticky=_parse_sticky(data.get("sticky"), warnings),
            reservations=_parse_reservations(data.get("reservations"), warnings),
            selection=dict(selection) if isinstance(selection, Mapping) else {},
            warnings=tuple(warnings),
        )

    # -- write -------------------------------------------------------------------------

    def record_pick(
        self,
        account: str,
        *,
        now_s: float,
        cost: float = 0.0,
        scope: str | None = None,
        window_s: float = 60.0,
        max_records: int = 500,
        sticky_ttl_s: float = 900.0,
        record_sticky: bool = True,
        record_reservation: bool = True,
        selection: Mapping[str, Any] | None = None,
    ) -> StateWrite:
        """Record a pick: refresh the incumbent and add a pileup reservation.

        Read-modify-write under one exclusive lock. On lock timeout this is a silent
        no-op returning ``stateless=True`` -- a lost reservation costs a little pileup
        accuracy, whereas blocking costs the caller their invocation.

        Args:
            selection: The selection layer's own state blob to persist verbatim, if any.
        """
        if not self.enabled:
            return StateWrite(ok=False, stateless=True, path=self.path)
        if not account:
            return StateWrite(ok=False, path=self.path, warnings=("no account to record",))

        with self._lock(exclusive=True) as acquired:
            if not acquired:
                return StateWrite(
                    ok=False,
                    stateless=True,
                    path=self.path,
                    warnings=(
                        f"state file busy for {self.lock_timeout_s:g}s ({self.path}); "
                        f"pick not recorded (no sticky, no pileup reservation)",
                    ),
                )

            current = self._read_unlocked()
            sticky = dict(current.sticky)
            if record_sticky:
                key = scope or GLOBAL_SCOPE
                entry = StickyEntry(account=account, at_s=now_s, scope=key)
                sticky[key] = entry
                # The global scope tracks the most recent pick of any class, so an
                # unscoped call still has an incumbent to be sticky to.
                if key != GLOBAL_SCOPE:
                    sticky[GLOBAL_SCOPE] = StickyEntry(
                        account=account, at_s=now_s, scope=GLOBAL_SCOPE
                    )

            sticky = _prune_sticky(sticky, now_s, sticky_ttl_s)

            reservations = list(current.reservations)
            if record_reservation and cost:
                reservations.append(Reservation(account=account, at_s=now_s, cost=float(cost)))
            reservations = _prune_reservations(reservations, now_s, window_s, max_records)

            blob = current.selection if selection is None else selection
            return self._write_unlocked(sticky, reservations, now_s, blob)

    def _write_unlocked(
        self,
        sticky: Mapping[str, StickyEntry],
        reservations: Iterable[Reservation],
        now_s: float,
        selection: Mapping[str, Any] | None = None,
    ) -> StateWrite:
        payload = {
            "version": STATE_VERSION,
            "updated_at_s": now_s,
            "sticky": {scope: entry.to_dict() for scope, entry in sticky.items()},
            "reservations": [item.to_dict() for item in reservations],
            "selection": dict(selection or {}),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.path.parent),
                prefix=self.path.name + ".",
                suffix=".tmp",
                delete=False,
            )
            tmp_path = Path(handle.name)
            try:
                with handle:
                    json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, self.path)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
        except OSError as exc:
            return StateWrite(
                ok=False,
                path=self.path,
                warnings=(f"cannot write state file {self.path}: {exc}",),
            )
        return StateWrite(ok=True, path=self.path)

    def clear(self) -> StateWrite:
        """Delete the state file (best effort)."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            return StateWrite(
                ok=False, path=self.path, warnings=(f"cannot remove {self.path}: {exc}",)
            )
        return StateWrite(ok=True, path=self.path)


# ======================================================================================
# Parsing / pruning helpers
# ======================================================================================


def _parse_sticky(raw: Any, warnings: list[str]) -> dict[str, StickyEntry]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        warnings.append("state: 'sticky' is not an object; ignoring it")
        return {}
    out: dict[str, StickyEntry] = {}
    for scope, body in raw.items():
        if not isinstance(body, Mapping):
            continue
        account = body.get("account")
        at_s = body.get("at_s")
        if not isinstance(account, str) or not account:
            continue
        if not isinstance(at_s, (int, float)) or isinstance(at_s, bool):
            continue
        out[str(scope)] = StickyEntry(account=account, at_s=float(at_s), scope=str(scope))
    return out


def _parse_reservations(raw: Any, warnings: list[str]) -> tuple[Reservation, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        warnings.append("state: 'reservations' is not a list; ignoring it")
        return ()
    out: list[Reservation] = []
    for body in raw:
        if not isinstance(body, Mapping):
            continue
        account = body.get("account")
        at_s = body.get("at_s")
        cost = body.get("cost", 0.0)
        if not isinstance(account, str) or not account:
            continue
        if not isinstance(at_s, (int, float)) or isinstance(at_s, bool):
            continue
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            continue
        out.append(Reservation(account=account, at_s=float(at_s), cost=float(cost)))
    out.sort(key=lambda item: item.at_s)
    return tuple(out)


def _prune_sticky(
    sticky: Mapping[str, StickyEntry], now_s: float, ttl_s: float
) -> dict[str, StickyEntry]:
    """Drop expired scopes, then cap how many scopes may accumulate."""
    live = {
        scope: entry for scope, entry in sticky.items() if entry.is_fresh(now_s, ttl_s)
    }
    if len(live) <= _MAX_STICKY_SCOPES:
        return live
    newest = sorted(live.items(), key=lambda item: item[1].at_s, reverse=True)
    return dict(newest[:_MAX_STICKY_SCOPES])


def _prune_reservations(
    reservations: Iterable[Reservation],
    now_s: float,
    window_s: float,
    max_records: int,
) -> list[Reservation]:
    """Drop reservations that can no longer matter, then cap the list length."""
    horizon = max(float(window_s), 0.0)
    live = [item for item in reservations if item.is_active(now_s, horizon)]
    live.sort(key=lambda item: item.at_s)
    if max_records >= 0 and len(live) > max_records:
        live = live[len(live) - max_records :]
    return live
