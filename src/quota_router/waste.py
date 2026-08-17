"""The measurement this project is judged on: quota that expired unspent at a reset.

The tool exists to reduce **wasted quota** -- subscription budget that evaporates when a
window rolls over, because it does not carry forward. Until this module existed that
number had never once been computed, and every change had been justified mechanically
("the scorer compared incommensurable units") rather than by evidence that the router
wastes less than the dial it replaced.

WHAT IS RECORDED
----------------
One line in ``waste.jsonl`` per window reset, per account. The remainder is expressed
two ways on purpose:

* ``remaining_fraction`` -- what the vendor published, kept verbatim so a row can be
  checked against the raw history it came from.
* ``wasted_pse`` -- the same remainder in absolute units. 55% of a max_20x pool and 55%
  of a max_5x pool are different amounts of lost work, so fractions cannot be summed
  across a fleet and a fleet total is the whole point. The conversion is
  :mod:`quota_router.pse`'s, not a second copy of it.

``k`` (the weekly:session capacity ratio) is on every row because it is calibrated per
account and will change. A series computed under one ``k`` must never be silently
reinterpreted under a later one.

``observation_gap_s`` -- how long before the reset the last reading was taken -- is
recorded rather than thresholded. A remainder measured eight minutes out is solid; one
measured three hours out is a guess. Which of those is good enough is a question for
whoever reads the series, and picking a cutoff now would bake one reader's answer in.

THIS FILE IS NEVER ROTATED AND NEVER PRUNED
-------------------------------------------
That is the requirement, not an oversight. Roughly 400 rows a year is a few hundred KB
for the life of the project.

The scar: history pruning trusted a caller-supplied clock, and one ad-hoc probe with a
synthetic ``--now`` five months in the future put the retention cutoff five months in
the future too, so every real file fell behind it and 38 hours of the calibration data
the whole ``k`` measurement rested on was deleted by a single debugging invocation.
**A measurement series that can be silently shortened is worse than none, because it
still renders -- it just answers a smaller question than the one that was asked.**

So there is no retention policy here at all: no rotation, no pruning, no rewriting, and
the file is opened for append or for read and never for anything else. A test enforces
that structurally, because the behavioural tests only cover the paths we thought of.

A MISSED RESET IS NOT A ZERO
----------------------------
The machine sleeps, the poller stops, ``resets_at`` comes back several windows on. Those
resets happened and their remainders are unrecoverable. They are written as rows with
``observed: false`` and no remainder, so a report can say "12 resets observed, 2 missed"
instead of quietly averaging over a hole. Interpolating them would produce exactly the
silent gap this whole measurement exists to avoid.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from . import pse
from .pse import Plan, SESSION_LENGTH_S, Stocks
from .types import (
    MODEL_CLASS_FABLE,
    WINDOW_KEY_5H,
    WINDOW_KEY_7D,
    capacity_for_tier,
    normalize_tier,
)

__all__ = [
    "WASTE_VERSION",
    "ResetRecord",
    "Detection",
    "WasteWrite",
    "waste_path",
    "detect_resets",
    "read_records",
    "append_records",
    "update_from_history",
    "summarize",
]

#: Record schema version (``v`` on every line).
WASTE_VERSION: Final[int] = 1

#: A reset instant that moves by less than this is serialization jitter, not movement.
#: Live, the scoped and account-wide weekly rows of one payload disagree by microseconds
#: to about a second around what is plainly a single instant.
RESET_JITTER_S: Final[float] = 60.0

#: How far the boundary must move before it can be a rollover, as a fraction of the
#: window. A replacement window ends one full length after it starts, and it cannot start
#: before the old one ended, so a genuine reset moves the boundary by AT LEAST one window
#: length -- more if the account sat idle before the new window opened.
#:
#: The cheap threshold ("moved forward by more than jitter") is therefore wrong in a way
#: that would be silent and enormous. If any published window turns out to slide rather
#: than tile -- a boundary that advances a little on every poll -- that reading mints a
#: fresh reset on EVERY poll, at ~35,000 rows a year into a file with no retention
#: policy, each one carrying a remainder that never expired. Half a window is the
#: unambiguous line: below it no rollover can have occurred, at or above it nothing else
#: can explain the move.
MIN_RESET_JUMP_FRACTION: Final[float] = 0.5

#: Both bars are published as integer percentages, so a "drop" smaller than half a point
#: is rounding, not consumption running backwards.
USED_QUANTUM: Final[float] = 0.005

#: Enumerating unobserved resets is bounded so that one corrupt ``resets_at`` cannot
#: write an unbounded number of rows into a file that is never pruned. Hitting it is
#: reported with the count that was cut, never silently absorbed.
MAX_MISSED_ENUMERATED: Final[int] = 64

#: Decimal places kept on the two derived quantities, so a row a human opens reads
#: ``0.07`` rather than ``0.06999999999999995``. This is display precision, not a cap on
#: the measurement: the vendor publishes integer percentages, so the fraction carries two
#: real decimals and ``k`` three significant figures, and everything past the sixth place
#: is IEEE noise from ``1 - used``. Nothing downstream is sensitive at that scale.
_ROUND_PLACES: Final[int] = 6


# ======================================================================================
# Location
# ======================================================================================


def waste_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the waste-series path -- a sibling of ``history.jsonl``.

    ``$QUOTA_ROUTER_WASTE`` wins (a directory means "put ``waste.jsonl`` in here"), then
    ``$XDG_STATE_HOME/quota-router``, then ``~/.local/state/quota-router``. This is the
    third hand-written copy of one resolver (``state_path``, ``history_path``, here);
    a test asserts the three land in the same directory for the same environment,
    because the requirement is "beside history.jsonl" and drift between copies is the
    only way that stops being true.
    """
    environ = os.environ if env is None else env

    override = (environ.get("QUOTA_ROUTER_WASTE") or "").strip()
    if override:
        path = Path(_expand(override, environ))
        return path / "waste.jsonl" if path.is_dir() else path

    xdg = (environ.get("XDG_STATE_HOME") or "").strip()
    if xdg:
        base = Path(_expand(xdg, environ))
    else:
        home = environ.get("HOME") or str(Path.home())
        base = Path(home) / ".local" / "state"
    return base / "quota-router" / "waste.jsonl"


def _expand(raw: str, env: Mapping[str, str]) -> str:
    """``~`` expansion against the *given* environment (never ``os.environ``)."""
    text = raw.strip()
    if text == "~" or text.startswith("~/"):
        home = env.get("HOME") or str(Path.home())
        return home + text[1:]
    return text


def _iso(epoch_s: float) -> str:
    """RFC 3339 in UTC. UTC on purpose: the series must not shift with the operator.

    Spelled with ``strftime`` rather than ``isoformat().replace("+00:00", "Z")`` -- the
    idiom the rest of the package uses -- so that the append-only guard in
    ``tests/test_waste.py`` can ban the name ``replace`` outright. A guard with an
    exception list is a guard someone edits; this file simply contains no spelling of
    anything that shortens a file.
    """
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ======================================================================================
# The record
# ======================================================================================


@dataclass(frozen=True, slots=True)
class ResetRecord:
    """One window reset of one account, and what it cost.

    Args:
        reset_at_s: The instant the window rolled over. This -- with the account and
            window -- is the identity of the occurrence, which is what makes appending
            idempotent: re-running over the same history can never double-count.
        observed: ``True`` when a reading exists from before this reset, so the
            remainder is measured. ``False`` when the reset happened while nothing was
            looking; then every remainder field is ``None``, because there is no honest
            number and a guessed one would be indistinguishable from a measured one.
        scoped: ``True`` for a model-scoped sub-cap (Fable). Its PSE is a *slice of the
            same weekly pool* as the account-wide row, so it is reported separately and
            never added into a fleet total.
        observation_gap_s: Seconds between the last reading and the reset. Recorded, not
            thresholded -- see the module docstring.
        k: The weekly:session ratio the PSE figure was computed with, so a later
            recalibration cannot silently reinterpret an older row.
    """

    account: str
    window: str
    reset_at_s: float
    observed: bool
    scoped: bool = False
    remaining_fraction: float | None = None
    wasted_pse: float | None = None
    last_observed_at_s: float | None = None
    observation_gap_s: float | None = None
    tier: str | None = None
    tier_scale: float | None = None
    k: float | None = None
    source: str | None = None
    signals: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "signals", tuple(self.signals))

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity of the occurrence, used for idempotent appends.

        The reset instant is rendered to a whole-second ISO string rather than compared
        as a float: the vendor's timestamps arrive with sub-second jitter across
        payloads, and two floats that differ in the seventh decimal are the same reset.
        """
        return (self.account, self.window, _iso(self.reset_at_s))

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        return {
            "v": WASTE_VERSION,
            "account": self.account,
            "window": self.window,
            "scoped": self.scoped,
            "reset_at": _iso(self.reset_at_s),
            "reset_at_s": self.reset_at_s,
            "observed": self.observed,
            "remaining_fraction": self.remaining_fraction,
            "wasted_pse": self.wasted_pse,
            "last_observed_at": (
                None if self.last_observed_at_s is None else _iso(self.last_observed_at_s)
            ),
            "last_observed_at_s": self.last_observed_at_s,
            "observation_gap_s": self.observation_gap_s,
            "tier": self.tier,
            "tier_scale": self.tier_scale,
            "k": self.k,
            "source": self.source,
            "signals": list(self.signals),
            "reason": self.reason,
        }


def record_from_dict(data: Mapping[str, Any]) -> ResetRecord | None:
    """Rebuild a record from one line, or ``None`` if the line is not one.

    Unreadable rows are skipped rather than raised on, for the same reason history
    tolerates a truncated final line: a crash mid-append must not hide every good row
    written before it.
    """
    if not isinstance(data, Mapping):
        return None
    account = data.get("account")
    window = data.get("window")
    reset_at_s = data.get("reset_at_s")
    if not isinstance(account, str) or not isinstance(window, str):
        return None
    if not isinstance(reset_at_s, (int, float)) or isinstance(reset_at_s, bool):
        return None

    def _optional_float(name: str) -> float | None:
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    signals = data.get("signals")
    return ResetRecord(
        account=account,
        window=window,
        reset_at_s=float(reset_at_s),
        observed=bool(data.get("observed")),
        scoped=bool(data.get("scoped")),
        remaining_fraction=_optional_float("remaining_fraction"),
        wasted_pse=_optional_float("wasted_pse"),
        last_observed_at_s=_optional_float("last_observed_at_s"),
        observation_gap_s=_optional_float("observation_gap_s"),
        tier=data.get("tier") if isinstance(data.get("tier"), str) else None,
        tier_scale=_optional_float("tier_scale"),
        k=_optional_float("k"),
        source=data.get("source") if isinstance(data.get("source"), str) else None,
        signals=tuple(s for s in signals if isinstance(s, str))
        if isinstance(signals, Sequence) and not isinstance(signals, str)
        else (),
        reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
    )


# ======================================================================================
# Reading the history into a per-account series
# ======================================================================================


@dataclass(frozen=True, slots=True)
class _WindowReading:
    key: str
    used: float
    length_s: float
    resets_at_s: float
    applies_to: frozenset[str]

    @property
    def scoped(self) -> bool:
        return bool(self.applies_to)

    @property
    def is_stock(self) -> bool:
        """Is this window a pile that expires, or a flow that is replaced?

        The five-hour window refills every five hours, so its rollover loses nothing --
        :mod:`quota_router.pse` is explicit that valuing it as a stock is what makes a
        router chase windows it cannot fill. Recording its rollovers as waste would add
        ~1,700 rows per account per year of a quantity this project does not call waste,
        and would inflate every total by roughly that much noise.
        """
        return self.length_s > SESSION_LENGTH_S


@dataclass(frozen=True, slots=True)
class _Reading:
    observed_at_s: float
    tier: str
    tier_scale: float
    source: str
    windows: Mapping[str, _WindowReading]


def _window_reading(data: Mapping[str, Any]) -> _WindowReading | None:
    key = data.get("key")
    used = data.get("used_fraction")
    length_s = data.get("length_s")
    resets_at_s = data.get("resets_at_s")
    if not isinstance(key, str):
        return None
    for value in (used, length_s, resets_at_s):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
    applies_to = data.get("applies_to")
    return _WindowReading(
        key=key,
        used=float(used),  # type: ignore[arg-type]
        length_s=float(length_s),  # type: ignore[arg-type]
        resets_at_s=float(resets_at_s),  # type: ignore[arg-type]
        applies_to=frozenset(s for s in applies_to if isinstance(s, str))
        if isinstance(applies_to, Sequence) and not isinstance(applies_to, str)
        else frozenset(),
    )


def _series(records: Iterable[Mapping[str, Any]]) -> dict[str, list[_Reading]]:
    """Per-account readings, oldest first, keyed on **observation** time.

    THE CLOCK IS ``observed_at_s``, NOT ``t``. Every history row carries two: ``t``, when
    the router wrote the row, and ``observed_at_s``, when the vendor actually reported
    those numbers. The cache source republishes its last reading on every poll, so a bar
    last truly read hours ago is re-emitted under a fresh ``t``.

    Keyed on ``t``, a republished pre-reset reading sorts *after* the post-reset one, and
    the pair then reads as the window jumping backwards -- a contradiction the detector
    reports and refuses, so a real reset is exchanged for a spurious complaint about data
    that was fine.

    A reading with no observation time at all is DROPPED rather than falling back to
    ``t``, which is where :func:`quota_router.history._account_series` lands. The two
    modules want different things from that row: the k estimator wants every usable
    reading, while this one is pairing consecutive sightings, and the only rows without
    an observation time are failed usage reads -- windowless snapshots of an account that
    could not be read. Admitting one as a sighting inserts a reading that saw nothing
    between the two that did, and the reset between them stops being detectable at all.
    """
    by_account: dict[str, list[_Reading]] = {}
    for record in records:
        accounts = record.get("accounts")
        if not isinstance(accounts, list):
            continue
        for entry in accounts:
            if not isinstance(entry, Mapping):
                continue
            account_id = entry.get("id")
            if not isinstance(account_id, str) or not account_id:
                continue
            raw_windows = entry.get("windows")
            if not isinstance(raw_windows, list):
                continue
            windows = {}
            for item in raw_windows:
                if not isinstance(item, Mapping):
                    continue
                reading = _window_reading(item)
                if reading is not None:
                    windows[reading.key] = reading
            # A failed usage read is windowless (``available=False`` with a note naming
            # the cause). It is not a reset and not a zero -- it is an absence, and it
            # must not break the chain between the readings either side of it. There is
            # no ``t`` fallback for the same reason: a snapshot that observed nothing has
            # no observation time, and inventing one inserts a blind reading between two
            # sighted ones, which is enough to make the reset between them undetectable.
            observed_candidates = [
                float(item["observed_at_s"])
                for item in raw_windows
                if isinstance(item, Mapping)
                and isinstance(item.get("observed_at_s"), (int, float))
                and not isinstance(item.get("observed_at_s"), bool)
            ]
            if not windows or not observed_candidates:
                continue
            observed_at_s = max(observed_candidates)

            tier = normalize_tier(entry.get("tier"))
            capacity = entry.get("capacity")
            tier_scale = (
                float(capacity)
                if isinstance(capacity, (int, float)) and not isinstance(capacity, bool)
                else capacity_for_tier(tier)
            )
            by_account.setdefault(account_id, []).append(
                _Reading(
                    observed_at_s=observed_at_s,
                    tier=tier,
                    tier_scale=tier_scale,
                    source=str(entry.get("source") or "unknown"),
                    windows=windows,
                )
            )

    # Sorted, but NOT de-duplicated on the observation time -- which is where the k
    # estimator in ``history`` has to drop republished rows, so the difference is worth
    # stating. That estimator sums increments, so counting one reading twice fabricates
    # sampling density it never had. This one compares neighbours for a change of window,
    # and a reading compared against a copy of itself shows no change; the occurrence is
    # then keyed on the reset instant, so even a duplicated detection collapses to one
    # row. There is no drop here because there is nothing for it to prevent, and an
    # unexercised guard is one nobody notices going wrong.
    for readings in by_account.values():
        readings.sort(key=lambda reading: reading.observed_at_s)
    return by_account


# ======================================================================================
# Detection
# ======================================================================================


@dataclass(frozen=True, slots=True)
class Detection:
    """Resets found in a history, plus every pair that was refused and why."""

    records: tuple[ResetRecord, ...] = ()
    warnings: tuple[str, ...] = ()


def _stocks(reading: _Reading, plan: Plan) -> Stocks | None:
    """Absolute stocks for one reading, via the shared PSE conversion.

    ``None`` when the account publishes no weekly window: without it there is no
    denominator to normalize against, and any PSE figure would be invented.
    """
    weekly = reading.windows.get(WINDOW_KEY_7D)
    if weekly is None:
        return None
    session = reading.windows.get(WINDOW_KEY_5H)
    fable = next(
        (w for w in reading.windows.values() if MODEL_CLASS_FABLE in w.applies_to), None
    )
    return Stocks.from_fractions(
        session_used=session.used if session else 0.0,
        weekly_used=weekly.used,
        fable_used=fable.used if fable else 0.0,
        tier_scale=reading.tier_scale,
        session_reset_s=session.resets_at_s if session else weekly.resets_at_s,
        weekly_reset_s=weekly.resets_at_s,
        plan=plan,
    )


def _wasted_for(
    window: _WindowReading, stocks: Stocks | None, plan: Plan, tier_scale: float
) -> tuple[float | None, str | None]:
    """PSE that expired with this window, and why it could not be computed if it could not.

    At the reset instant the horizon is zero, so nothing can be absorbed and the whole
    remainder is lost. That is exactly what :func:`quota_router.pse.wasted_pse` returns
    when ``now_s`` is the reset -- the working rate cannot matter across zero seconds --
    so the objective the router optimizes and the number it is scored on are the same
    function, not two copies that can drift.
    """
    if stocks is None:
        return None, "account published no weekly window; nothing to normalize against"
    if window.key == WINDOW_KEY_7D:
        return (
            pse.wasted_pse(stocks, now_s=stocks.weekly_reset_s, rate_pse_per_hour=0.0),
            None,
        )
    if MODEL_CLASS_FABLE in window.applies_to:
        # Already the coupled minimum: Fable work spends the shared weekly pool too, so
        # the sub-cap can never deliver more than the weekly pool has left.
        #
        # When that minimum binds, ``remaining_fraction`` and ``wasted_pse`` on this row
        # stop being related by ``fraction x k x tier_scale``, and a reader months from
        # now has no way to tell that from an arithmetic bug. So the row says which of
        # the two ceilings produced its number, in the row itself.
        fable_capacity = plan.fable_fraction * plan.weekly_to_session * tier_scale
        uncoupled = max(0.0, 1.0 - window.used) * fable_capacity
        if stocks.fable_remaining < uncoupled - 1e-9:
            return stocks.fable_remaining, (
                f"weekly-coupled: the sub-cap itself had "
                f"{uncoupled:.3f} PSE left but the shared weekly pool it also draws on "
                f"had only {stocks.weekly_remaining:.3f}, so remaining_fraction and "
                f"wasted_pse do not agree on this row and the smaller ceiling is the "
                f"real one"
            )
        return stocks.fable_remaining, None
    return None, (
        f"no calibrated sub-cap fraction for window {window.key!r}; the remainder is "
        f"recorded as a fraction but cannot be converted to PSE"
    )


def detect_resets(
    records: Iterable[Mapping[str, Any]], *, plans: Mapping[str, Plan] | None = None
) -> Detection:
    """Find every window reset the history can attest to.

    A reset shows up two ways -- the used fraction dropping, and ``resets_at`` moving
    forward -- and ``resets_at`` is the one that carries the *instant*, so it is the
    trigger. The drop is recorded as a corroborating signal when it is also present.

    Args:
        records: History records (see :func:`quota_router.history.iter_records`).
        plans: Per-account calibrated denominators. Absent, the built-in ``k`` is used
            and stamped on the row, so the row still says which value produced it.
    """
    plans = plans or {}
    found: list[ResetRecord] = []
    warnings: list[str] = []

    for account_id, readings in sorted(_series(records).items()):
        plan = plans.get(account_id) or Plan()
        for before, after in zip(readings, readings[1:]):
            stocks = _stocks(before, plan)
            for key, window in before.windows.items():
                if not window.is_stock:
                    continue
                later = after.windows.get(key)
                if later is None:
                    continue

                jump = later.resets_at_s - window.resets_at_s
                if jump < -RESET_JITTER_S:
                    warnings.append(
                        f"{account_id}/{key}: reset time moved backwards "
                        f"{_iso(window.resets_at_s)} -> {_iso(later.resets_at_s)}; "
                        f"refusing to record a reset from data that disagrees with itself"
                    )
                    continue
                if jump < MIN_RESET_JUMP_FRACTION * window.length_s:
                    if later.used < window.used - USED_QUANTUM:
                        warnings.append(
                            f"{account_id}/{key}: incoherent pair -- used fell "
                            f"{window.used:.0%} -> {later.used:.0%} but the reset time "
                            f"moved only {jump / window.length_s:.2f} of a window; a "
                            f"window cannot hold less than it did without rolling over, "
                            f"and a rollover moves the boundary by a full length"
                        )
                    elif jump > RESET_JITTER_S:
                        warnings.append(
                            f"{account_id}/{key}: reset time moved forward "
                            f"{jump / window.length_s:.2f} of a window without rolling "
                            f"over ({_iso(window.resets_at_s)} -> "
                            f"{_iso(later.resets_at_s)}); recorded as no reset, but a "
                            f"window whose boundary drifts is not the fixed window this "
                            f"measurement assumes"
                        )
                    continue

                signals = ["resets_at_advanced"]
                if later.used < window.used - USED_QUANTUM:
                    signals.append("used_fraction_dropped")

                wasted, reason = _wasted_for(window, stocks, plan, before.tier_scale)
                found.append(
                    ResetRecord(
                        account=account_id,
                        window=key,
                        reset_at_s=window.resets_at_s,
                        observed=True,
                        scoped=window.scoped,
                        remaining_fraction=round(
                            max(0.0, 1.0 - window.used), _ROUND_PLACES
                        ),
                        wasted_pse=(
                            None if wasted is None else round(wasted, _ROUND_PLACES)
                        ),
                        last_observed_at_s=before.observed_at_s,
                        observation_gap_s=max(
                            0.0, window.resets_at_s - before.observed_at_s
                        ),
                        tier=before.tier,
                        tier_scale=before.tier_scale,
                        k=plan.weekly_to_session,
                        source=before.source,
                        signals=tuple(signals),
                        reason=reason,
                    )
                )

                # How many resets fit in the jump. Anchored to ``later`` rather than to
                # ``before``, because ``later.resets_at_s`` is a value the vendor
                # actually published while any instant derived forward from ``before``
                # is arithmetic. Under continuously tiled windows this is exact; if the
                # vendor instead starts a window on first use after an idle stretch it
                # OVER-counts, which is the right direction to be wrong in -- a report
                # that claims more holes than there were never hides one.
                missed = max(0, int(round(jump / window.length_s)) - 1)
                emitted = min(missed, MAX_MISSED_ENUMERATED)
                if missed > emitted:
                    warnings.append(
                        f"{account_id}/{key}: {missed} unobserved resets implied by a "
                        f"jump of {jump / window.length_s:.1f} window lengths; recorded "
                        f"the {emitted} most recent and cut {missed - emitted}"
                    )
                for index in range(emitted, 0, -1):
                    found.append(
                        ResetRecord(
                            account=account_id,
                            window=key,
                            reset_at_s=later.resets_at_s - index * window.length_s,
                            observed=False,
                            scoped=window.scoped,
                            tier=before.tier,
                            tier_scale=before.tier_scale,
                            k=plan.weekly_to_session,
                            signals=("resets_at_advanced",),
                            reason=(
                                f"unobserved: the reset time jumped "
                                f"{jump / window.length_s:.1f} window lengths between "
                                f"{_iso(before.observed_at_s)} and "
                                f"{_iso(after.observed_at_s)}, so this reset happened "
                                f"with nothing watching and its remainder is "
                                f"unrecoverable"
                            ),
                        )
                    )

    found.sort(key=lambda row: (row.reset_at_s, row.account, row.window))
    return Detection(records=tuple(found), warnings=tuple(warnings))


# ======================================================================================
# The file
# ======================================================================================


@dataclass(frozen=True, slots=True)
class WasteWrite:
    """Outcome of an append. Best-effort: failures are warnings, never exceptions.

    The waste series is telemetry about routing, not an input to it, so nothing here may
    raise into a ``status`` run any more than history may raise into a pick.
    """

    ok: bool = False
    path: Path | None = None
    appended: int = 0
    skipped: int = 0
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", tuple(self.warnings))


def _iter_lines(path: Path) -> Iterator[str]:
    try:
        handle = open(path, encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError:
        return
    with handle:
        yield from handle


def read_records(
    path: str | os.PathLike[str] | None = None, *, env: Mapping[str, str] | None = None
) -> tuple[tuple[ResetRecord, ...], tuple[str, ...]]:
    """Every record in the series, in file order, plus a count of unreadable lines."""
    target = Path(path) if path is not None else waste_path(env)
    rows: list[ResetRecord] = []
    bad = 0
    for line in _iter_lines(target):
        text = line.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            bad += 1
            continue
        record = record_from_dict(data)
        if record is None:
            bad += 1
            continue
        rows.append(record)
    warnings = (
        (f"{bad} unreadable line(s) in {target}; the readable rows are unaffected",)
        if bad
        else ()
    )
    return tuple(rows), warnings


def append_records(
    records: Iterable[ResetRecord],
    *,
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> WasteWrite:
    """Append rows the file does not already have, and nothing else.

    Idempotent on ``(account, window, reset_at)``: the reset instant uniquely identifies
    the occurrence, so re-running the detector over the same history -- or over a longer
    history that contains it -- can never double-count.

    There is deliberately no ``max_bytes``, no ``keep_days``, and no way to pass a clock
    that could shorten the series. See the module docstring for the incident that made
    that a hard requirement rather than a default.
    """
    target = Path(path) if path is not None else waste_path(env)
    existing, warnings = read_records(target)
    seen = {record.key for record in existing}

    fresh: list[ResetRecord] = []
    skipped = 0
    for record in records:
        if record.key in seen:
            skipped += 1
            continue
        seen.add(record.key)
        fresh.append(record)

    if not fresh:
        return WasteWrite(ok=True, path=target, appended=0, skipped=skipped,
                          warnings=warnings)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return WasteWrite(
            ok=False,
            path=target,
            skipped=skipped,
            warnings=warnings + (f"cannot create waste directory: {exc}",),
        )

    try:
        with open(target, "a", encoding="utf-8") as handle:
            for record in fresh:
                handle.write(json.dumps(record.to_dict(), separators=(",", ":")) + "\n")
    except OSError as exc:
        return WasteWrite(
            ok=False,
            path=target,
            skipped=skipped,
            warnings=warnings + (f"cannot append to waste file {target}: {exc}",),
        )

    return WasteWrite(
        ok=True, path=target, appended=len(fresh), skipped=skipped, warnings=warnings
    )


def update_from_history(
    records: Iterable[Mapping[str, Any]],
    *,
    plans: Mapping[str, Plan] | None = None,
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    now_s: float | None = None,
) -> WasteWrite:
    """Detect resets in a history and append the ones not already recorded.

    ``now_s`` is accepted and unused on purpose. Every field written here comes out of
    the readings -- observation times, reset instants, published fractions -- so a
    synthetic or wrong clock cannot change what the series says. The last time a
    file-maintenance path trusted a caller-supplied clock it deleted 38 hours of data.
    """
    detection = detect_resets(records, plans=plans)
    write = append_records(detection.records, path=path, env=env)
    return WasteWrite(
        ok=write.ok,
        path=write.path,
        appended=write.appended,
        skipped=write.skipped,
        warnings=write.warnings + detection.warnings,
    )


# ======================================================================================
# Reporting
# ======================================================================================


def _blank_bucket() -> dict[str, Any]:
    return {
        "resets": 0,
        "observed": 0,
        "missed": 0,
        "wasted_pse": 0.0,
        "max_observation_gap_s": None,
    }


def _absorb(bucket: dict[str, Any], record: ResetRecord) -> None:
    bucket["resets"] += 1
    if record.observed:
        bucket["observed"] += 1
        if record.wasted_pse is not None:
            bucket["wasted_pse"] += record.wasted_pse
        if record.observation_gap_s is not None:
            current = bucket["max_observation_gap_s"]
            bucket["max_observation_gap_s"] = (
                record.observation_gap_s
                if current is None
                else max(current, record.observation_gap_s)
            )
    else:
        bucket["missed"] += 1


def summarize(records: Iterable[ResetRecord]) -> dict[str, Any]:
    """Per account per window, what expired unused, with totals in PSE.

    Scoped sub-caps are totalled **separately**. A Fable PSE is a weekly PSE seen a
    second time through a narrower window -- adding the two would double-count the same
    lost work, which is the same coupling mistake :mod:`quota_router.pse` exists to
    prevent on the routing side.
    """
    accounts: dict[str, dict[str, dict[str, Any]]] = {}
    totals = _blank_bucket()
    scoped_totals = _blank_bucket()
    span: list[float] = []

    for record in records:
        bucket = accounts.setdefault(record.account, {}).setdefault(
            record.window, {**_blank_bucket(), "scoped": record.scoped}
        )
        _absorb(bucket, record)
        _absorb(scoped_totals if record.scoped else totals, record)
        span.append(record.reset_at_s)

    return {
        "accounts": accounts,
        "totals": totals,
        "scoped_totals": scoped_totals,
        "first_reset_at_s": min(span) if span else None,
        "last_reset_at_s": max(span) if span else None,
    }
