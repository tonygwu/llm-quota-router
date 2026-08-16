"""Convert every quota window to one absolute unit, then compare.

THE UNIT
--------
A **PSE** is one Max-20x five-hour budget. Everything is expressed in it:

    session capacity = 1.0                x tier_scale
    weekly capacity  = weekly_to_session  x tier_scale
    fable capacity   = fable_fraction     x weekly capacity

WHY THIS MODULE EXISTS
----------------------
The scorer this replaces compared window *fractions* against each other --
``min(0.89, 0.23)`` -- which is not a comparison at all: 0.89 is 89% of a
five-hour budget, 0.23 is 23% of a weekly budget, and the two have different
denominators. Live, that produced an account whose score was computed from its
five-hour window while the tool reported its seven-day window as binding.

Percentages are the only thing the vendor publishes (``limit_dollars`` and
friends are always null), so the denominators have to be recovered by
calibration. That is what ``Plan`` holds.

THE TWO CONSTANTS, AND HOW MUCH TO TRUST THEM
---------------------------------------------
``weekly_to_session`` (k) -- **6.25**, from a single clean full-window
measurement: an account created fresh, both bars starting at zero, driven to
100% of its five-hour window with no reset in between, reading 100pp session
against 16pp weekly. Cross-checks: the increment-summing estimator read 6.5 on
the same account (slightly high -- summing loses a little burn to the gap before
each sample), and a second 20x account read 6.1.

An earlier value of 11.9 was wrong and is worth recording as such. It came from
four intervals where the weekly bar moved only 1-3pp, so the 1pp quantum
dominated, and it was taken before a source-oscillation bug was fixed. It was
reported with +/-2 error bars that were far too confident. The correction is
roughly a factor of two, which had been making every weekly pool look twice as
large as it is.

Still a calibrated input, never a literal: Anthropic changed session caps in May
without resizing the weekly bucket, and ``quotapick calibrate`` re-derives this
per account from the history log.

``fable_fraction`` -- Anthropic documents the Fable cap as 50% of the weekly
allowance. Two independent checks against live data are *consistent* with 0.5
without pinning it: a static consistency bound gives f <= 0.551, and a
delta-based estimate gives <= 0.625 (inflated, because the sampled work mixed
Fable and non-Fable and so overstates the numerator). Consistent is not proven;
0.5 is used because it is documented, not because the arithmetic established it.

FABLE IS A CONSTRAINT, NOT A PILE
---------------------------------
Fable work draws down its own sub-cap *and* the shared weekly pool. So the Fable
quota actually available is ``min(fable sub-cap remaining, weekly remaining)``.
Modelling it as an independent bucket overstates capacity on any account whose
weekly pool is nearly dry, and is the likeliest bug in a naive implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .types import MODEL_CLASS_FABLE, normalize_model_class

__all__ = [
    "DEFAULT_WEEKLY_TO_SESSION",
    "DEFAULT_FABLE_FRACTION",
    "SESSION_LENGTH_S",
    "Plan",
    "Stocks",
    "available_pse",
    "wasted_pse",
    "rank_key",
]

#: Measured, not assumed. See the module docstring for provenance and error bars.
DEFAULT_WEEKLY_TO_SESSION: Final[float] = 6.25

#: Documented by Anthropic as 50% of the weekly allowance for Max plans.
DEFAULT_FABLE_FRACTION: Final[float] = 0.5

SESSION_LENGTH_S: Final[float] = 5 * 3600.0


@dataclass(frozen=True, slots=True)
class Plan:
    """The calibrated denominators that turn percentages into absolute units."""

    weekly_to_session: float = DEFAULT_WEEKLY_TO_SESSION
    fable_fraction: float = DEFAULT_FABLE_FRACTION


@dataclass(frozen=True, slots=True)
class Stocks:
    """One account's remaining quota, in PSE, plus the deadlines that govern it."""

    session_remaining: float
    weekly_remaining: float
    fable_remaining: float
    session_capacity: float
    session_reset_s: float
    weekly_reset_s: float

    @classmethod
    def from_fractions(
        cls,
        *,
        session_used: float,
        weekly_used: float,
        fable_used: float,
        tier_scale: float,
        session_reset_s: float,
        weekly_reset_s: float,
        plan: Plan | None = None,
    ) -> "Stocks":
        p = plan or Plan()
        session_cap = 1.0 * tier_scale
        weekly_cap = p.weekly_to_session * tier_scale
        fable_cap = p.fable_fraction * weekly_cap

        weekly_rem = max(0.0, 1.0 - weekly_used) * weekly_cap
        # The coupling: Fable spends the shared weekly pool too, so it can never
        # deliver more than the weekly pool has left, whatever its own bar says.
        fable_rem = min(max(0.0, 1.0 - fable_used) * fable_cap, weekly_rem)

        return cls(
            session_remaining=max(0.0, 1.0 - session_used) * session_cap,
            weekly_remaining=weekly_rem,
            fable_remaining=fable_rem,
            session_capacity=session_cap,
            session_reset_s=session_reset_s,
            weekly_reset_s=weekly_reset_s,
        )


def _session_supply(stocks: Stocks, *, now_s: float, horizon_s: float) -> float:
    """PSE the five-hour constraint can deliver over ``horizon_s``.

    The session window is a *flow*, not a stock: it refills every five hours. So
    over a long horizon it supplies the current window's remainder plus a fresh
    full budget for each subsequent window. Treating it as a one-off stock is
    what makes a router over-value a nearly-full window that is about to reset --
    the quota is not lost, it is replaced.
    """
    to_reset = max(0.0, stocks.session_reset_s - now_s)
    supply = stocks.session_remaining if horizon_s > 0 else 0.0
    if horizon_s > to_reset:
        refills = (horizon_s - to_reset) / SESSION_LENGTH_S
        supply += refills * stocks.session_capacity
    return supply


def available_pse(
    stocks: Stocks,
    *,
    now_s: float,
    horizon_s: float,
    rate_pse_per_hour: float,
    model_class: str | None,
) -> float:
    """Most PSE this account can actually deliver over ``horizon_s``.

    Four ceilings, and the smallest wins:

    1. what the five-hour constraint supplies over the horizon,
    2. what the weekly pool has left,
    3. for Fable work, the Fable sub-cap (already coupled to the weekly pool),
    4. **what you can physically consume at your working rate** -- the ceiling
       that stops a window expiring in 17 minutes from being valued as though
       you could drain it.
    """
    ceilings = [
        _session_supply(stocks, now_s=now_s, horizon_s=horizon_s),
        stocks.weekly_remaining,
        rate_pse_per_hour * (max(0.0, horizon_s) / 3600.0),
    ]
    if normalize_model_class(model_class) == MODEL_CLASS_FABLE:
        ceilings.append(stocks.fable_remaining)
    return max(0.0, min(ceilings))


def wasted_pse(stocks: Stocks, *, now_s: float, rate_pse_per_hour: float) -> float:
    """PSE that will expire unused at the weekly reset if we do not spend here.

    This is the objective. Quota is perishable: whatever is left when the weekly
    window rolls over is gone. An account wastes quota only to the extent its
    remainder exceeds what you could consume before the deadline -- bounded by
    both your working rate and the five-hour throttle, since neither alone
    determines throughput.
    """
    horizon_s = max(0.0, stocks.weekly_reset_s - now_s)
    absorbable = min(
        rate_pse_per_hour * (horizon_s / 3600.0),
        _session_supply(stocks, now_s=now_s, horizon_s=horizon_s),
    )
    return max(0.0, stocks.weekly_remaining - absorbable)


def rank_key(
    stocks: Stocks,
    *,
    now_s: float,
    rate_pse_per_hour: float,
    model_class: str | None,
) -> tuple[float, float, float]:
    """Sort key for choosing an account; larger is better.

    Three terms, lexicographic:

    1. **Waste.** Spend first from the pool that would otherwise expire unused.
       This is use-it-or-lose-it, and it dominates whenever any account is
       carrying more quota than it can possibly burn before its deadline.
    2. **Earliest deadline.** When nothing will be wasted -- you will consume
       everything either way -- the ordering that keeps the most options open is
       to drain the pool that resets soonest.
    3. **Absolute availability.** A final tie-break so two accounts with matching
       deadlines resolve toward the one that can actually serve more work.

    No regime switch. The previous scorer needed one because its slack term went
    negative, and multiplying a negative by a tier capacity ratio inverts the
    ordering -- making a smaller account look better under scarcity. Every term
    here is non-negative by construction, so that failure mode cannot arise.
    """
    horizon_s = max(0.0, stocks.weekly_reset_s - now_s)
    waste = wasted_pse(stocks, now_s=now_s, rate_pse_per_hour=rate_pse_per_hour)
    avail = available_pse(
        stocks,
        now_s=now_s,
        horizon_s=horizon_s,
        rate_pse_per_hour=rate_pse_per_hour,
        model_class=model_class,
    )
    return (waste, -horizon_s, avail)
