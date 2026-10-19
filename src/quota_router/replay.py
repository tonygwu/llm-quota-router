"""Replay: route the fleet's real demand through each policy and measure the waste.

This is the gate for making ``[engine] objective = "joint"`` the default. It answers "on
the last N days, would the joint objective have wasted less than today's?" with the
production decision code, not a re-implementation of it.

Reconstruction (from history, read-only):

* Each account's consumption: the rises between consecutive readings
  (:func:`quota_router.forecast.account_rises`, the same reading the forecast uses),
  spread evenly over the time between readings and binned.
* Each account's reset schedule: every weekly boundary the readings published. Resets
  are facts about the vendor, the same under every policy.
* Each account's state when counting starts: the last reading before it.

Simulation, per policy: the group's binned demand is cut into jobs. Each job goes to the
pool the policy picks, through the real :func:`quota_router.select.select` for
``own_waste``, ``own_waste_measured_rate`` and ``joint``. A pool's leftover when its
scheduled reset passes is waste. A job no pool can take is unmet. The forecast a policy
sees at time *t* is rebuilt from the trace before *t* only.

The metric for a policy is ``M = W + Tail``:

* ``W``: leftover at every scheduled reset and plan end inside the counted window.
* ``Tail``: from the state at the end, the same neutral drain for every policy
  (earliest deadline first at the trailing mean demand, :func:`quota_router.joint.project`)
  over 7 days. With the same demand served, ``W`` plus the stock left at the end is
  constant, so a policy could look good by hoarding stock past the end; the tail
  charges for it.

``actual`` debits each account with its own observed consumption. Its ``W`` must match
the waste series (``waste.jsonl``) for the same window, or the reconstruction is wrong
and no comparison counts: that is the fidelity gate.

Validity limits, printed with every result: all demand in a group is treated as
reroutable, though some of it was pinned (``cl`` records no picks); one-point
quantization of the 7-day bar is 0.0625 PSE at 20x; PSE compares only within a group.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Final

from . import forecast as forecast_mod
from .demand import group_for
from . import joint
from . import waste
from .pse import Plan, SESSION_LENGTH_S
from .select import SelectionState, select
from .types import (
    MODEL_CLASS_FABLE,
    SOURCE_LIVE,
    WINDOW_KEY_5H,
    WINDOW_KEY_7D,
    AccountSnapshot,
    Window,
)

POLICIES: Final[tuple[str, ...]] = (
    "actual",
    "own_waste",
    "own_waste_measured_rate",
    "joint",
    "edf",
)

_WEEK_S: Final[float] = 7 * 86400.0
_EPS: Final[float] = 1e-9


@dataclass(frozen=True, slots=True)
class ResetEvent:
    """One weekly rollover: when it happened and what the ending window had promised.

    ``visible_s`` is the boundary the account published before the reset, which is all
    a policy could know; ``roll_s`` is when the stock actually refilled. They differ only
    for an early reset, whose leftover is forfeited rather than wasted.
    """

    roll_s: float
    visible_s: float
    early: bool
    observed: bool


@dataclass(frozen=True, slots=True)
class PoolSpec:
    """Everything about one account that does not depend on the policy."""

    id: str
    provider: str
    tier: str
    tier_scale: float
    plan: Plan
    events: tuple[ResetEvent, ...]
    plan_end_s: float | None
    start_used: float
    start_fable_used: float | None
    has_fable: bool

    @property
    def weekly_cap(self) -> float:
        return self.plan.weekly_to_session * self.tier_scale

    @property
    def fable_cap(self) -> float:
        return self.plan.fable_fraction * self.weekly_cap


@dataclass(frozen=True, slots=True)
class Trace:
    """Demand per bin: the group total and each account's own share."""

    start_s: float
    bin_s: float
    group_pse: tuple[float, ...]
    group_fable_pse: tuple[float, ...]
    by_account: Mapping[str, tuple[float, ...]]
    by_account_fable: Mapping[str, tuple[float, ...]]

    def bin_start(self, index: int) -> float:
        return self.start_s + index * self.bin_s


@dataclass(slots=True)
class _Pool:
    spec: PoolSpec
    weekly_rem: float
    fable_rem: float | None
    event_index: int
    session_rem: float | None = None
    session_reset_s: float | None = None
    retired: bool = False

    def _event(self) -> ResetEvent:
        events = self.spec.events
        if self.event_index < len(events):
            return events[self.event_index]
        if not events:
            # No boundary was ever published: no reset is known, so none is invented.
            return ResetEvent(roll_s=math.inf, visible_s=math.inf, early=False, observed=False)
        boundary = events[-1].roll_s + _WEEK_S * (self.event_index - len(events) + 1)
        return ResetEvent(roll_s=boundary, visible_s=boundary, early=False, observed=False)

    @property
    def deadline_s(self) -> float:
        """The deadline a policy sees: the published boundary, or the plan end."""
        visible = self._event().visible_s
        if self.spec.plan_end_s is not None:
            return min(visible, self.spec.plan_end_s)
        return visible

    @property
    def roll_s(self) -> float:
        roll = self._event().roll_s
        if self.spec.plan_end_s is not None:
            return min(roll, self.spec.plan_end_s)
        return roll


@dataclass(frozen=True, slots=True)
class PolicyResult:
    policy: str
    waste_pse: float
    observed_waste_pse: float
    forfeited_pse: float
    tail_pse: float
    unmet_pse: float
    served_pse: float
    switches: int
    jobs: int
    clamped: int
    choices: tuple[tuple[float, str], ...] = field(default=(), repr=False)

    @property
    def metric_pse(self) -> float:
        return self.waste_pse + self.tail_pse

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "metric_pse": self.metric_pse,
            "waste_pse": self.waste_pse,
            "tail_pse": self.tail_pse,
            "forfeited_pse": self.forfeited_pse,
            "observed_waste_pse": self.observed_waste_pse,
            "unmet_pse": self.unmet_pse,
            "served_pse": self.served_pse,
            "switches": self.switches,
            "jobs": self.jobs,
            "clamped": self.clamped,
        }


# ======================================================================================
# Reconstruction
# ======================================================================================


def _bin_into(
    bins: list[float], rise_start: float, rise_end: float, amount: float, start_s: float,
    bin_s: float,
) -> None:
    if amount <= 0 or rise_end <= rise_start:
        return
    end_s = start_s + bin_s * len(bins)
    lo, hi = max(rise_start, start_s), min(rise_end, end_s)
    if hi <= lo:
        return
    rate = amount / (rise_end - rise_start)
    first = int((lo - start_s) // bin_s)
    last = min(len(bins) - 1, int(math.ceil((hi - start_s) / bin_s)) - 1)
    for index in range(first, last + 1):
        b0 = start_s + index * bin_s
        overlap = min(hi, b0 + bin_s) - max(lo, b0)
        if overlap > 0:
            bins[index] += rate * overlap


def _events(
    account_id: str,
    weekly_series: Sequence[Any],
    detections: Iterable[waste.ResetRecord],
    start_s: float,
) -> tuple[ResetEvent, ...]:
    """Weekly rollovers after ``start_s``, from the waste series' own detection.

    The detector already tells a scheduled reset from an early one and a measured one
    from a missed one; reading it here keeps the replay and ``waste.jsonl`` in step. The
    boundary the latest reading publishes closes the list.
    """
    out = [
        ResetEvent(
            roll_s=row.reset_at_s,
            visible_s=row.scheduled_reset_at_s or row.reset_at_s,
            early=row.kind == waste.KIND_EARLY,
            observed=row.observed,
        )
        for row in detections
        if row.account == account_id
        and row.window == WINDOW_KEY_7D
        and not row.scoped
        and row.reset_at_s > start_s
    ]
    out.sort(key=lambda event: event.roll_s)
    pending = weekly_series[-1].windows[WINDOW_KEY_7D].resets_at_s if weekly_series else None
    if pending is not None and (not out or pending > out[-1].roll_s + 3600.0) and pending > start_s:
        out.append(ResetEvent(roll_s=pending, visible_s=pending, early=False, observed=False))
    return tuple(out)


def reconstruct(
    records: Iterable[Mapping[str, Any]],
    *,
    group: str,
    plans: Mapping[str, Plan],
    plan_ends: Mapping[str, float],
    start_s: float,
    end_s: float,
    bin_s: float,
    demand_groups: Mapping[str, str] | None = None,
) -> tuple[list[PoolSpec], Trace]:
    """Pools and demand for one group over ``[start_s, end_s)``.

    ``start_s`` is when counting starts; history before it is read for the starting
    state and for the forecast's look-back, which the caller includes in ``records``.
    """
    n_bins = max(1, int(math.ceil((end_s - start_s) / bin_s)))
    materialized = list(records)
    readings = waste.readings_by_account(materialized)
    detections = waste.detect_resets(materialized, plans=plans).records
    specs: list[PoolSpec] = []
    by_account: dict[str, tuple[float, ...]] = {}
    by_account_fable: dict[str, tuple[float, ...]] = {}
    lookback_s = start_s - _WEEK_S
    look_bins = int(math.ceil(_WEEK_S / bin_s))
    for account_id, series in sorted(readings.items()):
        series = forecast_mod.dedupe_readings(series)
        provider = next((r.provider for r in reversed(series) if r.provider), None)
        if group_for(account_id, provider or "", demand_groups) != group:
            continue
        weekly_series = [r for r in series if WINDOW_KEY_7D in r.windows]
        before = [r for r in weekly_series if r.observed_at_s <= start_s]
        if not before:
            continue
        first = before[-1]
        plan = plans.get(account_id) or Plan()
        events = _events(account_id, weekly_series, detections, start_s)
        fable_window = next(
            (w for w in first.windows.values() if MODEL_CLASS_FABLE in w.applies_to), None
        )
        specs.append(
            PoolSpec(
                id=account_id,
                provider=provider or group,
                tier=first.tier,
                tier_scale=first.tier_scale,
                plan=plan,
                events=events,
                plan_end_s=plan_ends.get(account_id),
                start_used=first.windows[WINDOW_KEY_7D].used,
                start_fable_used=fable_window.used if fable_window is not None else None,
                has_fable=fable_window is not None,
            )
        )
        bins = [0.0] * (n_bins + look_bins)
        fbins = [0.0] * (n_bins + look_bins)
        for rise in forecast_mod.account_rises(weekly_series, plan):
            _bin_into(bins, rise.start_s, rise.end_s, rise.weekly_pse, lookback_s, bin_s)
            _bin_into(fbins, rise.start_s, rise.end_s, rise.fable_pse, lookback_s, bin_s)
        by_account[account_id] = tuple(bins)
        by_account_fable[account_id] = tuple(fbins)
    total = [0.0] * (n_bins + look_bins)
    ftotal = [0.0] * (n_bins + look_bins)
    for account_id in by_account:
        for index, value in enumerate(by_account[account_id]):
            total[index] += value
            ftotal[index] += by_account_fable[account_id][index]
    trace = Trace(
        start_s=lookback_s,
        bin_s=bin_s,
        group_pse=tuple(total),
        group_fable_pse=tuple(ftotal),
        by_account=by_account,
        by_account_fable=by_account_fable,
    )
    return specs, trace


# ======================================================================================
# Simulation
# ======================================================================================


def _start(spec: PoolSpec, at_s: float) -> _Pool:
    index = 0
    while index < len(spec.events) and spec.events[index].roll_s <= at_s:
        index += 1
    fable_rem = None
    if spec.has_fable:
        used = spec.start_fable_used if spec.start_fable_used is not None else 0.0
        fable_rem = max(0.0, 1.0 - used) * spec.fable_cap
    weekly_rem = max(0.0, 1.0 - spec.start_used) * spec.weekly_cap
    if fable_rem is not None:
        fable_rem = min(fable_rem, weekly_rem)
    return _Pool(spec=spec, weekly_rem=weekly_rem, fable_rem=fable_rem, event_index=index)


def _settle(pool: _Pool, t: float, count_from_s: float, sink: dict[str, float]) -> None:
    while not pool.retired and pool.roll_s <= t:
        roll = pool.roll_s
        event = pool._event()
        spec = pool.spec
        ended = spec.plan_end_s is not None and roll >= spec.plan_end_s
        leftover = max(0.0, pool.weekly_rem)
        if roll >= count_from_s:
            if event.early and not ended:
                sink["forfeited"] += leftover
            else:
                sink["waste"] += leftover
                if event.observed or ended:
                    sink["observed"] += leftover
        if ended:
            pool.retired = True
            pool.weekly_rem = 0.0
            pool.fable_rem = 0.0 if pool.fable_rem is not None else None
            return
        pool.event_index += 1
        pool.weekly_rem = spec.weekly_cap
        pool.fable_rem = spec.fable_cap if spec.has_fable else None
    if pool.session_reset_s is not None and pool.session_reset_s <= t:
        pool.session_reset_s = None
        pool.session_rem = None


def _room(pool: _Pool, fable: bool) -> float:
    if pool.retired:
        return 0.0
    room = pool.weekly_rem
    cap = pool.spec.tier_scale
    room = min(room, pool.session_rem if pool.session_rem is not None else cap)
    if fable and pool.fable_rem is not None:
        room = min(room, pool.fable_rem)
    return max(0.0, room)


def _take(pool: _Pool, amount: float, fable: bool, t: float) -> None:
    pool.weekly_rem -= amount
    if pool.session_rem is None:
        pool.session_rem = pool.spec.tier_scale
        pool.session_reset_s = t + SESSION_LENGTH_S
    pool.session_rem -= amount
    if fable and pool.fable_rem is not None:
        pool.fable_rem -= amount
    if pool.fable_rem is not None:
        pool.fable_rem = min(pool.fable_rem, pool.weekly_rem)


#: Stand-in for "no known deadline" where a window needs a finite reset time.
_FAR_S: Final[float] = 365 * 86400.0


def _deadline(pool: _Pool, t: float) -> float:
    return min(pool.deadline_s, t + _FAR_S)


def _snapshot(pool: _Pool, t: float) -> AccountSnapshot:
    spec = pool.spec
    windows = [
        Window(
            key=WINDOW_KEY_7D,
            used_fraction=min(1.0, max(0.0, 1.0 - pool.weekly_rem / spec.weekly_cap)),
            length_s=_WEEK_S,
            resets_at_s=_deadline(pool, t),
            observed_at_s=t,
        )
    ]
    if pool.session_rem is not None and pool.session_reset_s is not None:
        windows.append(
            Window(
                key=WINDOW_KEY_5H,
                used_fraction=min(1.0, max(0.0, 1.0 - pool.session_rem / spec.tier_scale)),
                length_s=SESSION_LENGTH_S,
                resets_at_s=pool.session_reset_s,
                observed_at_s=t,
            )
        )
    if pool.fable_rem is not None:
        windows.append(
            Window(
                key="fable",
                used_fraction=min(1.0, max(0.0, 1.0 - pool.fable_rem / spec.fable_cap)),
                length_s=_WEEK_S,
                resets_at_s=_deadline(pool, t),
                observed_at_s=t,
                applies_to=frozenset({MODEL_CLASS_FABLE}),
            )
        )
    return AccountSnapshot(
        id=spec.id,
        provider=spec.provider,
        windows=tuple(windows),
        tier=spec.tier,
        # The size history recorded for this pool, so the scorer inside ``select`` sees
        # the same PSE the stock and the metric use. Without it a pool recorded at a
        # configured capacity would be scored at its tier's ratio.
        capacity_override=spec.tier_scale,
        source=SOURCE_LIVE,
        confidence=1.0,
        available=not pool.retired,
    )


def _stock(pool: _Pool, t: float, group: str | None = None) -> joint.Stock:
    spec = pool.spec
    return joint.Stock(
        id=spec.id,
        group=group or group_for(spec.id, spec.provider) or spec.provider,
        kind=joint.KIND_SUBSCRIPTION,
        weekly_rem=0.0 if pool.retired else pool.weekly_rem,
        weekly_cap=spec.weekly_cap,
        deadline_s=max(_deadline(pool, t), t + 1.0),
        refills=spec.plan_end_s is None or spec.plan_end_s > pool.deadline_s + 60.0,
        period_s=_WEEK_S,
        session_cap=spec.tier_scale,
        session_rem=pool.session_rem,
        session_reset_s=pool.session_reset_s,
        fable_rem=pool.fable_rem,
        fable_cap=spec.fable_cap if spec.has_fable else None,
        available_from_s=t,
    )


def _trailing_rate(trace: Trace, index: int, window_bins: int, scale: float) -> tuple[float, float]:
    lo = max(0, index - window_bins)
    total = sum(trace.group_pse[lo:index])
    fable = sum(trace.group_fable_pse[lo:index])
    hours = max(1e-9, (index - lo) * trace.bin_s / 3600.0)
    return scale * total / hours, (fable / total if total > 0 else 0.0)


def _cfg(specs: Sequence[PoolSpec], rate: float | None, group: str | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "accounts": {s.id: {"weekly_to_session": s.plan.weekly_to_session} for s in specs},
        "min_remaining_floor": 0.02,
        "demand_group_by_account": {
            s.id: group or group_for(s.id, s.provider) or s.provider for s in specs
        },
    }
    if rate is not None:
        cfg["rate_pse_per_hour"] = rate
    return cfg


Chooser = Callable[[list[_Pool], float, bool, int, float, float], tuple[str | None, bool]]


def simulate(
    specs: Sequence[PoolSpec],
    trace: Trace,
    *,
    policy: str,
    count_from_s: float,
    end_s: float,
    job_pse: float,
    forecast_scale: float = 1.0,
    group: str = "claude",
    chooser: Chooser | None = None,
) -> PolicyResult:
    """Run one policy over ``[count_from_s, end_s)`` and measure it."""
    pools = [_start(spec, count_from_s) for spec in specs]
    by_id = {pool.spec.id: pool for pool in pools}
    sink = {"waste": 0.0, "observed": 0.0, "forfeited": 0.0}
    unmet = served = 0.0
    jobs = switches = clamped = 0
    choices: list[tuple[float, str]] = []
    state = SelectionState()
    last: str | None = None
    window_bins = int(round(_WEEK_S / trace.bin_s))
    first_bin = int(round((count_from_s - trace.start_s) / trace.bin_s))
    n_bins = int(math.ceil((end_s - count_from_s) / trace.bin_s))

    def choose(t: float, fable: bool, index: int, size: float) -> str | None:
        nonlocal clamped
        live = [p for p in pools if _room(p, fable) > _EPS and p.weekly_rem / p.spec.weekly_cap > 0.02]
        if not live:
            return None
        if chooser is not None:
            pick, was_clamped = chooser(live, t, fable, index, size, forecast_scale)
            clamped += was_clamped
            return pick
        if policy == "edf":
            return min(live, key=lambda p: (_deadline(p, t), p.spec.id)).spec.id
        snaps = [_snapshot(p, t) for p in live]
        model_class = MODEL_CLASS_FABLE if fable else None
        if policy == "own_waste":
            decision = select(snaps, t, _cfg(specs, None, group), model_class, state)
            return decision.chosen
        rate, fable_share = _trailing_rate(trace, index, window_bins, forecast_scale)
        if policy == "own_waste_measured_rate":
            decision = select(snaps, t, _cfg(specs, rate, group), model_class, state)
            return decision.chosen
        if policy == "joint":
            context = joint.JointContext(
                stocks=[_stock(p, t, group) for p in pools],
                streams=[
                    joint.Stream(group, joint.STREAM_OTHER, rate, fable_share),
                    joint.Stream(group, joint.STREAM_RUN, 0.0, 0.0),
                ],
                now_s=t,
                horizon_s=_WEEK_S,
                step_s=3600.0,
                job_pse=size,
                fable_ids=frozenset(p.spec.id for p in pools) if fable else frozenset(),
            )
            decision = select(snaps, t, _cfg(specs, None, group), model_class, state, joint=context)
            if decision.chosen is not None:
                result = context.score(decision.chosen)
                clamped += bool(result is not None and result.clamped)
            return decision.chosen
        raise ValueError(f"unknown policy {policy!r}")

    for offset in range(n_bins):
        index = first_bin + offset
        t = trace.bin_start(index)
        for pool in pools:
            _settle(pool, t, count_from_s, sink)
        if index >= len(trace.group_pse):
            break
        if policy == "actual":
            for account_id, bins in trace.by_account.items():
                pool = by_id.get(account_id)
                need = bins[index]
                fable_need = trace.by_account_fable[account_id][index]
                if pool is None or need <= 0:
                    continue
                take = min(need, max(0.0, pool.weekly_rem))
                pool.weekly_rem -= take
                if pool.fable_rem is not None:
                    pool.fable_rem = min(max(0.0, pool.fable_rem - fable_need), pool.weekly_rem)
                served += take
                unmet += need - take
            continue
        demand = trace.group_pse[index]
        fable_demand = min(demand, trace.group_fable_pse[index])
        for fable, amount in ((True, fable_demand), (False, demand - fable_demand)):
            left = amount
            tries = 0
            while left > _EPS and tries < 1000:
                tries += 1
                size = min(job_pse, left)
                chosen = choose(t, fable, index, size)
                if chosen is None:
                    unmet += left
                    break
                jobs += 1
                if last is not None and chosen != last:
                    switches += 1
                last = chosen
                choices.append((t, chosen))
                pool = by_id[chosen]
                take = min(size, _room(pool, fable))
                if take <= _EPS:
                    unmet += left
                    break
                _take(pool, take, fable, t)
                served += take
                left -= take

    end_t = trace.bin_start(first_bin + n_bins)
    for pool in pools:
        _settle(pool, end_t, count_from_s, sink)
    tail = _tail(pools, trace, first_bin + n_bins, window_bins, end_t, group)
    return PolicyResult(
        policy=policy,
        waste_pse=sink["waste"],
        observed_waste_pse=sink["observed"],
        forfeited_pse=sink["forfeited"],
        tail_pse=tail,
        unmet_pse=unmet,
        served_pse=served,
        switches=switches,
        jobs=jobs,
        clamped=clamped,
        choices=tuple(choices),
    )


def _tail(
    pools: Sequence[_Pool], trace: Trace, index: int, window_bins: int, t: float, group: str
) -> float:
    """The same neutral drain for every policy: earliest deadline at trailing demand."""
    rate, fable_share = _trailing_rate(trace, min(index, len(trace.group_pse)), window_bins, 1.0)
    projection = joint.project(
        [_stock(p, t, group) for p in pools],
        [joint.Stream(group, joint.STREAM_OTHER, rate, fable_share)],
        now_s=t,
        horizon_s=_WEEK_S,
        step_s=3600.0,
    )
    return projection.v_pse


# ======================================================================================
# The comparison
# ======================================================================================


@dataclass(frozen=True, slots=True)
class ReplayResult:
    group: str
    count_from_s: float
    end_s: float
    job_pse: float
    forecast_scale: float
    accounts: tuple[str, ...]
    recorded_waste_pse: float | None
    fidelity_ok: bool | None
    policies: tuple[PolicyResult, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "count_from_s": self.count_from_s,
            "end_s": self.end_s,
            "job_pse": self.job_pse,
            "forecast_scale": self.forecast_scale,
            "accounts": list(self.accounts),
            "recorded_waste_pse": self.recorded_waste_pse,
            "fidelity_ok": self.fidelity_ok,
            "policies": [p.to_dict() for p in self.policies],
            "notes": list(self.notes),
        }


def recorded_waste(
    rows: Iterable[waste.ResetRecord], *, accounts: Iterable[str], start_s: float, end_s: float
) -> float:
    """Waste the series measured for these accounts' scheduled weekly resets in the window."""
    wanted = set(accounts)
    return sum(
        row.wasted_pse or 0.0
        for row in waste.current_records(list(rows))
        if row.account in wanted
        and row.window == WINDOW_KEY_7D
        and not row.scoped
        and row.observed
        and (row.kind or waste.KIND_SCHEDULED) == waste.KIND_SCHEDULED
        and start_s <= (row.scheduled_reset_at_s or row.reset_at_s) < end_s
    )


def fidelity(simulated: float, recorded: float) -> bool:
    return abs(simulated - recorded) <= max(0.3, 0.1 * abs(recorded))


def run_replay(
    records: Iterable[Mapping[str, Any]],
    *,
    waste_rows: Iterable[waste.ResetRecord] | None,
    plans: Mapping[str, Plan],
    plan_ends: Mapping[str, float],
    count_from_s: float,
    end_s: float,
    policies: Sequence[str] = POLICIES,
    group: str = "claude",
    job_pse: float = 0.01,
    forecast_scale: float = 1.0,
    bin_s: float = 900.0,
    without: Sequence[str] = (),
    demand_groups: Mapping[str, str] | None = None,
) -> ReplayResult:
    """Replay ``policies`` over ``[count_from_s, end_s)``.

    ``without`` drops those accounts' pools but keeps their demand: "what if this
    account had not been there". The fidelity gate does not apply then, since
    ``actual`` can no longer debit the dropped accounts.
    """
    for name in policies:
        if name not in POLICIES:
            raise ValueError(f"unknown policy {name!r} (known: {', '.join(POLICIES)})")
    specs, trace = reconstruct(
        records,
        group=group,
        plans=plans,
        plan_ends=plan_ends,
        start_s=count_from_s,
        end_s=end_s,
        bin_s=bin_s,
        demand_groups=demand_groups,
    )
    dropped = set(without)
    unknown = dropped - {spec.id for spec in specs}
    if unknown:
        raise ValueError(f"cannot drop {', '.join(sorted(unknown))}: not in the {group} group")
    specs = [spec for spec in specs if spec.id not in dropped]
    results = tuple(
        simulate(
            specs,
            trace,
            policy=name,
            count_from_s=count_from_s,
            end_s=end_s,
            job_pse=job_pse,
            forecast_scale=forecast_scale,
            group=group,
        )
        for name in policies
    )
    recorded = (
        recorded_waste(waste_rows, accounts=[s.id for s in specs], start_s=count_from_s,
                       end_s=end_s)
        if waste_rows is not None
        else None
    )
    actual = next((r for r in results if r.policy == "actual"), None)
    ok = (
        fidelity(actual.observed_waste_pse, recorded)
        if actual is not None and recorded is not None and not dropped
        else None
    )
    notes = (
        "all demand in the group is treated as reroutable; some was pinned (cl records no picks)",
        "one point of a 20x weekly bar is 0.0625 PSE, so small differences are noise",
        "PSE compares only within one demand group",
    ) + (
        (f"what-if: pools dropped, demand kept: {', '.join(sorted(dropped))}",)
        if dropped
        else ()
    )
    return ReplayResult(
        group=group,
        count_from_s=count_from_s,
        end_s=end_s,
        job_pse=job_pse,
        forecast_scale=forecast_scale,
        accounts=tuple(s.id for s in specs),
        recorded_waste_pse=recorded,
        fidelity_ok=ok,
        policies=results,
        notes=notes,
    )
