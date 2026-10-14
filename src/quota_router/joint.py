"""The joint projection: how much fleet waste one job avoids.

Today's objective (:func:`quota_router.scoring.pse_objective`) values each account alone,
as if the whole working rate flowed into it. A small pool -- a $200 API credit pool is
about 0.66 PSE -- therefore projects no waste and loses every pick until it expires, and
two pools that share one stream of work both claim it. This module projects the **fleet**
instead:

``V(S)``
    Spread forecast demand over every stock, hour by hour, earliest deadline first, under
    each subscription's five-hour cap. A stock's leftover when its deadline passes is
    waste. ``V`` is the sum over deadlines inside the horizon.

``avoided(i) = V(S) - V(S with the job taken from i now)``
    The waste this job avoids if it runs on stock *i*. This is the score; earliest
    deadline only breaks ties, and that happens at the decision layer.

Earliest-deadline-first is the **stand-in rule for future demand** inside the projection,
not the decision rule. Re-running the objective for every future hour would be the full
scheduling problem.

Stocks and streams:

* A subscription's weekly pool is a stock. At its deadline it refills (a weekly reset) or
  retires (a plan end). Its Fable sub-cap is a constraint on Fable work, never a second
  pile: Fable leftover is part of weekly leftover.
* The five-hour window is a flow cap, not a stock. A new window opens at first use.
* An API credit pool is a stock with no five-hour cap that retires at its expiry.
* Demand comes in two streams per provider group. ``other`` work (``pick``, ``cl``,
  ``cdx``, the library) reaches subscriptions only; ``run`` work (``quotapick run``) may
  reach credit pools too. Groups never trade demand.

Unmet demand is computed, because the projection must stop pools at zero, and reported.
It is never scored (operator decision, 2026-10-14).

Pure: standard library and package types only, no I/O, no clock.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from .pse import SESSION_LENGTH_S, Plan
from .scoring import stocks_from_snapshot
from .types import AccountSnapshot, WINDOW_KEY_5H

KIND_SUBSCRIPTION: Final[str] = "subscription"
KIND_CREDIT: Final[str] = "credit"

STREAM_OTHER: Final[str] = "other"
STREAM_RUN: Final[str] = "run"

#: Which stock kinds may serve each stream.
SERVES: Final[Mapping[str, frozenset[str]]] = {
    STREAM_OTHER: frozenset({KIND_SUBSCRIPTION}),
    STREAM_RUN: frozenset({KIND_SUBSCRIPTION, KIND_CREDIT}),
}

#: Provider -> demand group. A provider not listed carries no measurable demand.
GROUP_FOR_PROVIDER: Final[Mapping[str, str]] = {
    "claude": "claude",
    "codex": "codex",
    "antigravity": "antigravity",
    "anthropic_api": "claude",
}

#: A non-refilling deadline past the default horizon (a credit pool expiring in three
#: weeks) extends the horizon so its leftover is counted, up to this far.
MAX_HORIZON_S: Final[float] = 35 * 86400.0

_EPS: Final[float] = 1e-12


@dataclass(frozen=True, slots=True)
class Stock:
    """One perishable pool, in PSE.

    Args:
        weekly_rem: What is left now.
        weekly_cap: What a refill restores.
        deadline_s: When what is left now perishes.
        refills: True for a weekly reset; False for a plan end or a credit expiry.
        period_s: Time between refills.
        session_cap: PSE per five-hour window, or None for no five-hour cap.
        session_rem: PSE left in the open window, or None when no window is open.
        session_reset_s: When the open window closes, or None when none is open.
        fable_rem: Fable sub-cap left, or None when Fable work is limited only by the
            weekly pool.
        fable_cap: What a refill restores to the Fable sub-cap.
        available_from_s: A cooldown: the stock serves nothing before this.
    """

    id: str
    group: str
    kind: str
    weekly_rem: float
    weekly_cap: float
    deadline_s: float
    refills: bool
    period_s: float
    session_cap: float | None
    session_rem: float | None
    session_reset_s: float | None
    fable_rem: float | None
    fable_cap: float | None
    available_from_s: float


@dataclass(frozen=True, slots=True)
class Stream:
    group: str
    name: str
    pse_per_h: float
    fable_share: float


@dataclass(frozen=True, slots=True)
class Settled:
    """A stock's leftover when one of its deadlines passed inside the horizon."""

    id: str
    deadline_s: float
    leftover: float
    kind: str


@dataclass(frozen=True, slots=True)
class Projection:
    v_pse: float
    settled: tuple[Settled, ...]
    unmet: Mapping[tuple[str, str], float]
    served: Mapping[str, float]
    refilled: Mapping[str, float]
    terminal: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class Marginal:
    """What one job avoids on one stock.

    Args:
        avoided: ``V`` before minus ``V`` after, clamped to ``[0, job_pse]``.
        job_pse: The part of the job this stock can serve now.
        raw: The unclamped difference. It can leave the range when starting a five-hour
            window early shifts the windows after it; ``clamped`` says so.
    """

    stock_id: str
    avoided: float
    job_pse: float
    raw: float
    clamped: bool


def subscription_stock(
    snap: AccountSnapshot,
    *,
    now_s: float,
    capacity: float,
    plan: Plan | None,
    refills: bool,
    available_from_s: float | None = None,
) -> Stock | None:
    """A subscription's stock from one snapshot, or None without a weekly window.

    Holds and reservations count as used, through
    :func:`quota_router.scoring.stocks_from_snapshot`, exactly as today's objective
    counts them. ``refills`` is False for an account whose plan ends before its next
    reset (``plan_ends``), whose deadline the caller has already moved.
    """
    stocks = stocks_from_snapshot(snap, now_s, capacity, plan)
    if stocks is None:
        return None
    p = plan or Plan()
    weekly_cap = p.weekly_to_session * capacity
    session = next((w for w in snap.windows if w.key == WINDOW_KEY_5H), None)
    active = session is not None and session.resets_at_s > now_s
    fable_scoped = any(w.applies_to and "fable" in w.applies_to for w in snap.windows)
    return Stock(
        id=snap.id,
        group=GROUP_FOR_PROVIDER.get(snap.provider, snap.provider),
        kind=KIND_SUBSCRIPTION,
        weekly_rem=stocks.weekly_remaining,
        weekly_cap=weekly_cap,
        deadline_s=stocks.weekly_reset_s,
        refills=refills,
        period_s=7 * 86400.0,
        session_cap=stocks.session_capacity,
        session_rem=stocks.session_remaining if active else None,
        session_reset_s=stocks.session_reset_s if active else None,
        fable_rem=stocks.fable_remaining if fable_scoped else None,
        fable_cap=p.fable_fraction * weekly_cap if fable_scoped else None,
        available_from_s=available_from_s if available_from_s is not None else now_s,
    )


# ======================================================================================
# The projection
# ======================================================================================


@dataclass(slots=True)
class _Live:
    stock: Stock
    weekly_rem: float
    deadline_s: float
    session_rem: float | None
    session_reset_s: float | None
    fable_rem: float | None
    retired: bool = False


def _horizon_end(stocks: Sequence[Stock], now_s: float, horizon_s: float) -> float:
    end = now_s + horizon_s
    for stock in stocks:
        if not stock.refills and stock.deadline_s > end:
            end = max(end, min(stock.deadline_s, now_s + MAX_HORIZON_S))
    return end


def project(
    stocks: Sequence[Stock],
    streams: Sequence[Stream],
    *,
    now_s: float,
    horizon_s: float,
    step_s: float,
) -> Projection:
    """Spread forecast demand over the stocks and total the leftover at each deadline."""
    if step_s <= 0 or horizon_s <= 0:
        raise ValueError("horizon_s and step_s must be positive")
    end_s = _horizon_end(stocks, now_s, horizon_s)
    live = [
        _Live(
            stock=s,
            weekly_rem=max(0.0, s.weekly_rem),
            deadline_s=s.deadline_s,
            session_rem=s.session_rem,
            session_reset_s=s.session_reset_s,
            fable_rem=s.fable_rem,
        )
        for s in stocks
    ]
    settled: list[Settled] = []
    unmet: dict[tuple[str, str], float] = {}
    served: dict[str, float] = {s.id: 0.0 for s in stocks}
    refilled: dict[str, float] = {s.id: 0.0 for s in stocks}

    # Most constrained first, so run work cannot take subscription room that other work
    # needed while a credit pool had room for it.
    portions: list[tuple[Stream, bool, float]] = []
    for name in (STREAM_OTHER, STREAM_RUN):
        for stream in streams:
            if stream.name != name or stream.pse_per_h <= 0:
                continue
            share = min(1.0, max(0.0, stream.fable_share))
            portions.append((stream, True, share))
            portions.append((stream, False, 1.0 - share))

    t = now_s
    while t < end_s - _EPS:
        step = min(step_s, end_s - t)
        _settle(live, t, settled, refilled)
        for item in live:
            if item.session_reset_s is not None and item.session_reset_s <= t:
                item.session_reset_s = None
                item.session_rem = None
        for stream, fable, share in portions:
            need = stream.pse_per_h * (step / 3600.0) * share
            if need <= _EPS:
                continue
            left = need
            kinds = SERVES[stream.name]
            for item in sorted(
                (x for x in live if not x.retired),
                key=lambda x: (x.deadline_s, x.stock.id),
            ):
                if left <= _EPS:
                    break
                stock = item.stock
                if stock.group != stream.group or stock.kind not in kinds:
                    continue
                if stock.available_from_s > t or item.weekly_rem <= _EPS:
                    continue
                time_share = min(1.0, max(0.0, (item.deadline_s - t) / step))
                limit = min(left, need * time_share, item.weekly_rem)
                if stock.session_cap is not None:
                    limit = min(
                        limit,
                        item.session_rem if item.session_rem is not None else stock.session_cap,
                    )
                if fable and item.fable_rem is not None:
                    limit = min(limit, item.fable_rem)
                if limit <= _EPS:
                    continue
                _take(item, limit, fable, t)
                served[stock.id] += limit
                left -= limit
            if left > _EPS:
                key = (stream.group, stream.name)
                unmet[key] = unmet.get(key, 0.0) + left
        t += step
    _settle(live, t + _EPS if t >= end_s else t, settled, refilled, only_before=end_s)

    terminal = {item.stock.id: (0.0 if item.retired else item.weekly_rem) for item in live}
    return Projection(
        v_pse=sum(row.leftover for row in settled),
        settled=tuple(settled),
        unmet=unmet,
        served=served,
        refilled=refilled,
        terminal=terminal,
    )


def _take(item: _Live, amount: float, fable: bool, now_s: float) -> None:
    item.weekly_rem -= amount
    if item.stock.session_cap is not None:
        if item.session_rem is None:
            item.session_rem = item.stock.session_cap
            item.session_reset_s = now_s + SESSION_LENGTH_S
        item.session_rem -= amount
    if fable and item.fable_rem is not None:
        item.fable_rem -= amount
    if item.fable_rem is not None:
        item.fable_rem = min(item.fable_rem, item.weekly_rem)


def _settle(
    live: list[_Live],
    t: float,
    settled: list[Settled],
    refilled: dict[str, float],
    *,
    only_before: float | None = None,
) -> None:
    for item in live:
        while not item.retired and item.deadline_s <= t:
            if only_before is not None and item.deadline_s > only_before:
                break
            settled.append(
                Settled(
                    id=item.stock.id,
                    deadline_s=item.deadline_s,
                    leftover=max(0.0, item.weekly_rem),
                    kind=item.stock.kind,
                )
            )
            if item.stock.refills and item.stock.period_s > 0:
                item.weekly_rem = item.stock.weekly_cap
                refilled[item.stock.id] += item.stock.weekly_cap
                item.fable_rem = item.stock.fable_cap
                item.deadline_s += item.stock.period_s
            else:
                item.weekly_rem = 0.0
                item.retired = True


def _serves_now(stock: Stock, job_pse: float, fable: bool) -> float:
    limit = min(job_pse, max(0.0, stock.weekly_rem))
    if stock.session_cap is not None:
        limit = min(
            limit, stock.session_rem if stock.session_rem is not None else stock.session_cap
        )
    if fable and stock.fable_rem is not None:
        limit = min(limit, stock.fable_rem)
    return max(0.0, limit)


def marginal(
    stocks: Sequence[Stock],
    streams: Sequence[Stream],
    *,
    stock_id: str,
    job_pse: float,
    fable: bool,
    stream: str,
    now_s: float,
    horizon_s: float,
    step_s: float,
    baseline: Projection | None = None,
) -> Marginal:
    """Waste avoided by running a job of ``job_pse`` on ``stock_id`` now.

    Only the stock's own group is projected: groups never trade demand, so the other
    groups' terms are equal on both sides and cancel. Pass ``baseline`` (the group's
    projection without the job) to avoid recomputing it for every candidate.
    """
    target = next((s for s in stocks if s.id == stock_id), None)
    if target is None:
        raise ValueError(f"unknown stock {stock_id!r}")
    if target.kind not in SERVES[stream]:
        raise ValueError(f"{stock_id} is a {target.kind} stock and cannot serve {stream} work")
    group = [s for s in stocks if s.group == target.group]
    group_streams = [s for s in streams if s.group == target.group]
    if baseline is None:
        baseline = project(group, group_streams, now_s=now_s, horizon_s=horizon_s, step_s=step_s)
    x = _serves_now(target, job_pse, fable) if target.available_from_s <= now_s else 0.0
    if x <= _EPS:
        return Marginal(stock_id=stock_id, avoided=0.0, job_pse=0.0, raw=0.0, clamped=False)
    debited = _debit(target, x, fable, now_s)
    after = project(
        [debited if s.id == stock_id else s for s in group],
        group_streams,
        now_s=now_s,
        horizon_s=horizon_s,
        step_s=step_s,
    )
    raw = baseline.v_pse - after.v_pse
    avoided = min(x, max(0.0, raw))
    clamped = not math.isclose(avoided, raw, abs_tol=1e-9)
    return Marginal(stock_id=stock_id, avoided=avoided, job_pse=x, raw=raw, clamped=clamped)


def _debit(stock: Stock, amount: float, fable: bool, now_s: float) -> Stock:
    session_rem, session_reset_s = stock.session_rem, stock.session_reset_s
    if stock.session_cap is not None:
        if session_rem is None:
            session_rem, session_reset_s = stock.session_cap, now_s + SESSION_LENGTH_S
        session_rem -= amount
    weekly = stock.weekly_rem - amount
    fable_rem = stock.fable_rem
    if fable_rem is not None:
        fable_rem = min(fable_rem - amount if fable else fable_rem, weekly)
    return replace(
        stock,
        weekly_rem=weekly,
        session_rem=session_rem,
        session_reset_s=session_reset_s,
        fable_rem=fable_rem,
    )
