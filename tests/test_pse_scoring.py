"""Acceptance cases for PSE-normalized scoring: the obvious calls must be obvious.

WHY THIS EXISTS
---------------
The previous scorer compared window *fractions* directly -- ``min(0.89, 0.23)`` --
which is dimensionally meaningless: 0.89 is 89% of a five-hour budget and 0.23 is
23% of a weekly budget, and those have different denominators. It produced a live
incoherence where an account's score was built from its 5-hour window while the
tool reported that its 7-day window was binding.

The fix is to convert every window to a common absolute unit before comparing
anything. The unit is the **PSE**: one Max-20x five-hour budget.

    session capacity = 1.0            x tier_scale
    weekly capacity  = k              x tier_scale      (k ~ 12, measured)
    fable capacity   = fable_fraction x weekly capacity (0.5, documented)

Two constants are estimates and must stay configurable:

* ``k`` was measured at ~11.9 from this operator's own logged deltas
  (sum d5h / sum d7d = 95/8 over four intervals). The 7-day bar is integer-
  quantized so the error bars are wide (+/-2). Anthropic has changed these limits
  repeatedly, so it is calibrated, never hardcoded as truth.
* ``fable_fraction`` is documented by Anthropic as 50% of the weekly allowance.
  Two independent checks against live data are *consistent* with 0.5 but do not
  pin it: a static bound gives f <= 0.551, and a delta-based estimate gives
  <= 0.625 (inflated because the sampled work was not Fable-only). Consistent is
  not the same as proven, and the code should not pretend otherwise.
"""

from __future__ import annotations

import pytest

from quota_router.pse import (
    DEFAULT_FABLE_FRACTION,
    DEFAULT_WEEKLY_TO_SESSION,
    Plan,
    Stocks,
    available_pse,
    rank_key,
    wasted_pse,
)

HOUR = 3600.0
DAY = 24 * HOUR
NOW = 1_800_000_000.0

PLAN = Plan(weekly_to_session=DEFAULT_WEEKLY_TO_SESSION, fable_fraction=DEFAULT_FABLE_FRACTION)


def stocks(
    *,
    session_used: float,
    weekly_used: float,
    fable_used: float = 0.0,
    tier_scale: float = 1.0,
    session_reset_in: float = 2 * HOUR,
    weekly_reset_in: float = 3 * DAY,
    plan: Plan = PLAN,
) -> Stocks:
    return Stocks.from_fractions(
        session_used=session_used,
        weekly_used=weekly_used,
        fable_used=fable_used,
        tier_scale=tier_scale,
        session_reset_s=NOW + session_reset_in,
        weekly_reset_s=NOW + weekly_reset_in,
        plan=plan,
    )


def better(a: Stocks, b: Stocks, *, rate: float = 2.0, model_class: str | None = None) -> str:
    """Which of two accounts the router should prefer. Returns 'a' or 'b'."""
    ka = rank_key(a, now_s=NOW, rate_pse_per_hour=rate, model_class=model_class)
    kb = rank_key(b, now_s=NOW, rate_pse_per_hour=rate, model_class=model_class)
    return "a" if ka > kb else "b"


# ======================================================================================
# 1. The headline case: quota about to expire should be spent first.
# ======================================================================================


def test_a_pool_expiring_sooner_wins_when_both_are_otherwise_identical() -> None:
    """Same remaining quota, different deadlines -> spend the one about to vanish.

    This is the whole premise of the tool. If it fails, nothing else matters.
    """
    soon = stocks(session_used=0.2, weekly_used=0.5, weekly_reset_in=3 * HOUR)
    later = stocks(session_used=0.2, weekly_used=0.5, weekly_reset_in=6 * DAY)
    assert better(soon, later) == "a"


def test_more_remaining_wins_when_deadlines_match() -> None:
    rich = stocks(session_used=0.2, weekly_used=0.30, weekly_reset_in=2 * DAY)
    poor = stocks(session_used=0.2, weekly_used=0.85, weekly_reset_in=2 * DAY)
    assert better(rich, poor) == "a"


# ======================================================================================
# 2. The mirage: a nearly-full session window expiring in minutes is NOT free money.
# ======================================================================================


def test_a_session_window_expiring_in_minutes_is_worth_only_what_you_can_absorb() -> None:
    """The trap that split two of three council models.

    An account whose 5-hour window is 98% unused but resets in 17 minutes looks
    like it is about to waste 0.98 PSE. At a working rate of 2 PSE/hour you can
    physically absorb 2 * (17/60) = 0.57 PSE before it resets -- and the window
    refills afterwards anyway, so nothing is actually lost. Valuing it at 0.98
    makes the router chase windows it cannot fill.
    """
    mirage = stocks(session_used=0.02, weekly_used=0.5, session_reset_in=17 * 60)
    absorbable = available_pse(
        mirage, now_s=NOW, horizon_s=17 * 60, rate_pse_per_hour=2.0, model_class=None
    )
    assert absorbable == pytest.approx(2.0 * (17 / 60), rel=0.01), (
        "session availability must be capped by what you can actually consume "
        f"before the window resets, got {absorbable}"
    )


def test_an_expiring_session_does_not_outrank_a_genuinely_at_risk_weekly_pool() -> None:
    # a: fat session about to reset, but its weekly is comfortable and far away.
    a = stocks(session_used=0.02, weekly_used=0.30, session_reset_in=15 * 60, weekly_reset_in=6 * DAY)
    # b: session irrelevant, but a big weekly pool expires in 3 hours.
    b = stocks(session_used=0.50, weekly_used=0.40, session_reset_in=4 * HOUR, weekly_reset_in=3 * HOUR)
    assert better(a, b) == "b"


# ======================================================================================
# 3. Tier normalization: percentages lie across tiers.
# ======================================================================================


def test_a_quarter_of_a_20x_beats_half_of_a_5x() -> None:
    """25% of a 20x is 3 PSE; 50% of a 5x is 1.5 PSE. The bigger bar loses."""
    big = stocks(session_used=0.0, weekly_used=0.75, tier_scale=1.00, weekly_reset_in=2 * DAY)
    small = stocks(session_used=0.0, weekly_used=0.50, tier_scale=0.25, weekly_reset_in=2 * DAY)
    assert better(big, small) == "a"


def test_tier_scale_is_applied_to_every_window_not_just_the_weekly() -> None:
    s = stocks(session_used=0.0, weekly_used=0.0, tier_scale=0.25)
    assert s.session_remaining == pytest.approx(0.25)
    assert s.weekly_remaining == pytest.approx(0.25 * DEFAULT_WEEKLY_TO_SESSION)


# ======================================================================================
# 4. Fable is a CONSTRAINT on the shared weekly pool, not a separate pile of tokens.
# ======================================================================================


def test_fable_availability_is_capped_by_the_shared_weekly_pool() -> None:
    """The single most likely bug in a first implementation.

    Fable draws down BOTH its own sub-cap and the general weekly pool. An account
    showing plenty of Fable headroom but a nearly-dry weekly pool cannot actually
    serve Fable work -- the weekly runs out first.
    """
    s = stocks(session_used=0.0, weekly_used=0.98, fable_used=0.0)
    assert s.weekly_remaining == pytest.approx(0.02 * DEFAULT_WEEKLY_TO_SESSION)
    assert s.fable_remaining == pytest.approx(s.weekly_remaining), (
        "Fable availability must be min(fable sub-cap remaining, weekly remaining); "
        "treating the Fable bar as an independent pile overstates it"
    )


def test_a_non_fable_request_ignores_the_fable_bar() -> None:
    exhausted_fable = stocks(session_used=0.0, weekly_used=0.20, fable_used=0.99)
    healthy = stocks(session_used=0.0, weekly_used=0.60, fable_used=0.00)
    # For opus work the Fable bar is irrelevant; the account with more weekly wins.
    assert better(exhausted_fable, healthy, model_class="opus") == "a"


def test_a_fable_request_will_not_pick_an_account_with_no_fable_headroom() -> None:
    no_fable = stocks(session_used=0.0, weekly_used=0.20, fable_used=1.0)
    some_fable = stocks(session_used=0.0, weekly_used=0.70, fable_used=0.50)
    assert better(no_fable, some_fable, model_class="fable") == "b"


# ======================================================================================
# 5. Feasibility: never hand back an account that cannot serve the job.
# ======================================================================================


def test_an_exhausted_account_offers_nothing() -> None:
    dead = stocks(session_used=1.0, weekly_used=1.0)
    assert available_pse(dead, now_s=NOW, horizon_s=HOUR, rate_pse_per_hour=2.0, model_class=None) == 0.0


def test_availability_is_the_minimum_across_the_binding_constraints() -> None:
    # Weekly is the scarce one here; session and rate could both supply more.
    s = stocks(session_used=0.0, weekly_used=0.99, session_reset_in=5 * HOUR)
    got = available_pse(s, now_s=NOW, horizon_s=5 * HOUR, rate_pse_per_hour=10.0, model_class=None)
    assert got == pytest.approx(0.01 * DEFAULT_WEEKLY_TO_SESSION)


# ======================================================================================
# 6. Waste: the objective the whole tool exists to minimize.
# ======================================================================================


def test_waste_is_bounded_by_the_session_throttle_not_just_your_typing_speed() -> None:
    """Over a short horizon the five-hour window, not your rate, is the throttle.

    6 PSE of weekly sits behind a deadline 2 hours out. At 2 PSE/hour you would
    happily absorb 4 -- but a five-hour window holds only 1 PSE and does not
    refill inside a 2-hour horizon, so 1 PSE is all that can physically flow.
    5 PSE expires no matter what you do.

    This is why "which window binds" is horizon-dependent: for any short working
    session the five-hour constraint governs, and the weekly pool only becomes
    the real limit once the horizon is long enough for several refills.
    """
    s = stocks(session_used=0.0, weekly_used=0.5, weekly_reset_in=2 * HOUR, session_reset_in=5 * HOUR)
    assert wasted_pse(s, now_s=NOW, rate_pse_per_hour=2.0) == pytest.approx(6.0 - 1.0, abs=0.01)


def test_waste_is_bounded_by_your_rate_when_the_horizon_is_long_enough_to_refill() -> None:
    """Same pool, long horizon: now your own throughput is the binding constraint.

    6 PSE, 48 hours to the deadline, session refilling throughout (supplying
    ~10.4 PSE), but working at only 0.1 PSE/hour you can absorb 4.8 -- so 1.2
    expires.
    """
    s = stocks(session_used=0.0, weekly_used=0.5, weekly_reset_in=48 * HOUR, session_reset_in=1 * HOUR)
    assert wasted_pse(s, now_s=NOW, rate_pse_per_hour=0.1) == pytest.approx(6.0 - 4.8, abs=0.05)


def test_nothing_is_wasted_when_the_deadline_is_far_enough_away() -> None:
    s = stocks(session_used=0.0, weekly_used=0.5, weekly_reset_in=6 * DAY)
    assert wasted_pse(s, now_s=NOW, rate_pse_per_hour=2.0) == pytest.approx(0.0)
