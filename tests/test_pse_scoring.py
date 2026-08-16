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


# ======================================================================================
# Full-path integration: the shape that real accounts actually have.
#
# WHY THIS SECTION EXISTS
# -----------------------
# Everything above tests ``pse.py`` in isolation. That left a hole: nothing ran the
# *whole* scoring path -- account_score / rank -- against a snapshot carrying a
# model-scoped Fable window, because no fixture in the suite had one.
#
# A missing ``MODEL_CLASS_FABLE`` import in scoring.py slipped through 390 passing
# tests because of exactly that. The name is only evaluated when a snapshot has a
# window with a truthy ``applies_to``, so a suite full of 5h/7d-only fixtures never
# reaches it. It would have shipped and fired on the first live account.
# ======================================================================================

from quota_router.scoring import account_score, rank, stocks_from_snapshot  # noqa: E402
from quota_router.types import (  # noqa: E402
    SOURCE_LIVE,
    TIER_MAX_5X,
    TIER_MAX_20X,
    AccountSnapshot,
    Window,
)

FIVE_HOUR_S = 5 * 3600.0
SEVEN_DAY_S = 7 * 24 * 3600.0


def _w(key: str, used: float, *, length_s: float, ttr_s: float, applies_to=None) -> Window:
    return Window(
        key=key,
        used_fraction=used,
        length_s=length_s,
        resets_at_s=NOW + ttr_s,
        observed_at_s=NOW,
        applies_to=applies_to,
    )


def realistic(
    account_id: str,
    *,
    session_used: float,
    weekly_used: float,
    fable_used: float,
    weekly_ttr_h: float,
    session_ttr_h: float = 2.0,
    tier: str = TIER_MAX_20X,
) -> AccountSnapshot:
    """A snapshot shaped like what the live usage endpoint actually returns.

    Three windows, and critically the third is **model-scoped** -- the shape the
    rest of the suite was missing.
    """
    return AccountSnapshot(
        id=account_id,
        windows=(
            _w("5h", session_used, length_s=FIVE_HOUR_S, ttr_s=session_ttr_h * HOUR),
            _w("7d", weekly_used, length_s=SEVEN_DAY_S, ttr_s=weekly_ttr_h * HOUR),
            _w(
                "fable",
                fable_used,
                length_s=SEVEN_DAY_S,
                ttr_s=weekly_ttr_h * HOUR,
                applies_to=frozenset({"fable"}),
            ),
        ),
        tier=tier,
        source=SOURCE_LIVE,
    )


def test_the_full_path_scores_a_snapshot_carrying_a_model_scoped_window() -> None:
    """The regression the suite was missing: score a snapshot WITH a Fable window.

    If ``MODEL_CLASS_FABLE`` is ever unimported from scoring.py again, this raises
    NameError rather than passing quietly.
    """
    snap = realistic("claude", session_used=0.11, weekly_used=0.77, fable_used=0.96, weekly_ttr_h=42.0)

    stocks = stocks_from_snapshot(snap, NOW, 1.0)
    assert stocks is not None, "a snapshot with a 7d window must normalize"

    for model_class in (None, "opus", "fable"):
        row = account_score(snap, NOW, None, model_class)
        assert row.eligible, f"{model_class}: {row.reason}"


def test_the_full_path_uses_pse_and_does_not_silently_fall_back() -> None:
    """Pin that the normalized objective is what actually ran.

    The scorer keeps a fallback for snapshots with no weekly window. A silent
    fallback would still produce a plausible ranking, so the reason string is
    asserted rather than merely the ordering.
    """
    snap = realistic("claude", session_used=0.11, weekly_used=0.77, fable_used=0.96, weekly_ttr_h=42.0)
    row = account_score(snap, NOW, None, "opus")
    assert "PSE" in row.reason, row.reason
    assert "cannot normalize" not in row.reason, row.reason


def test_the_fable_window_constrains_a_fable_request_but_not_an_opus_one() -> None:
    """The model-scoped window must gate Fable work and be invisible to everything else."""
    snap = realistic("claude", session_used=0.0, weekly_used=0.20, fable_used=0.99, weekly_ttr_h=42.0)
    stocks = stocks_from_snapshot(snap, NOW, 1.0)
    assert stocks is not None

    fable_avail = available_pse(
        stocks, now_s=NOW, horizon_s=5 * HOUR, rate_pse_per_hour=0.2, model_class="fable"
    )
    opus_avail = available_pse(
        stocks, now_s=NOW, horizon_s=5 * HOUR, rate_pse_per_hour=0.2, model_class="opus"
    )
    assert fable_avail < opus_avail, (
        "a 99%-consumed Fable sub-cap must restrict Fable work while leaving "
        f"opus work alone; got fable={fable_avail} opus={opus_avail}"
    )


def test_ranking_across_realistic_accounts_prefers_the_soonest_deadline_with_quota_at_risk() -> None:
    """End-to-end over three accounts shaped like the operator's real fleet."""
    fleet = [
        realistic("claude", session_used=0.11, weekly_used=0.77, fable_used=0.96, weekly_ttr_h=42.0),
        realistic("claude_b", session_used=0.00, weekly_used=0.74, fable_used=0.95, weekly_ttr_h=75.0),
        realistic(
            "claude_c", session_used=0.00, weekly_used=0.43, fable_used=0.78,
            weekly_ttr_h=140.0, tier=TIER_MAX_5X,
        ),
    ]
    ranked = rank(fleet, NOW, None, "opus")
    assert [r.account_id for r in ranked][0] == "claude", (
        "the account whose weekly pool expires soonest with quota still on it should "
        f"win; got {[(r.account_id, round(r.score, 4)) for r in ranked]}"
    )


def test_a_five_x_account_cannot_serve_more_than_a_quarter_window_of_work() -> None:
    """Throughput, not quota: a max_5x window holds 0.25 PSE however full the weekly is.

    This is why a fixed job-size gate would exclude the 5x account from every heavy
    interactive launch -- worth pinning, because it looks like a bug when it happens.
    """
    rich_but_small = realistic(
        "claude_c", session_used=0.0, weekly_used=0.10, fable_used=0.10,
        weekly_ttr_h=140.0, session_ttr_h=5.0, tier=TIER_MAX_5X,
    )
    stocks = stocks_from_snapshot(rich_but_small, NOW, 0.25)
    assert stocks is not None
    # Horizon ends exactly when the window does, so no refill is counted. Over a
    # LONGER horizon the answer is legitimately larger -- the window is a flow, not
    # a stock -- which is why the horizon and the window boundary must line up to
    # measure "one window's worth".
    got = available_pse(
        stocks, now_s=NOW, horizon_s=5 * HOUR, rate_pse_per_hour=1.0, model_class=None
    )
    assert got == pytest.approx(0.25), (
        f"a 5x window holds 0.25 PSE however full the weekly is, got {got}"
    )

    # And confirm the flow behaviour explicitly, so the bound above is not mistaken
    # for a hard ceiling on what the account can ever supply.
    over_two_windows = available_pse(
        stocks, now_s=NOW, horizon_s=10 * HOUR, rate_pse_per_hour=1.0, model_class=None
    )
    assert over_two_windows == pytest.approx(0.50), over_two_windows


# ======================================================================================
# Degraded sources must never DELETE a constraint.
# ======================================================================================


def test_a_source_that_cannot_see_the_fable_window_must_not_look_unconstrained() -> None:
    """The failure a batch caller hit: an account looked 57% free for Fable at 20%.

    Observed live. claude_c's usage-endpoint read was rate-limited and its cached
    payload had aged past the stale bound, so the statusline cache served instead --
    and the statusline carries only the 5h and 7d windows, never the model-scoped
    one. The merged snapshot therefore had no Fable window, ``min_remaining_fraction
    ("fable")`` fell back to the account-wide 57%, and the account sailed past a
    ``--min-remaining 0.50`` gate it should have failed.

    A missing window is not an absent constraint. When any source knows about a
    window, the merged snapshot must keep it, because dropping one can only ever
    flatter the account -- the more dangerous direction.
    """
    from quota_router.providers import collect_snapshots
    from quota_router.types import SOURCE_CACHE

    complete = AccountSnapshot(
        id="claude_c",
        windows=(
            _w("5h", 0.06, length_s=FIVE_HOUR_S, ttr_s=2 * HOUR),
            _w("7d", 0.43, length_s=SEVEN_DAY_S, ttr_s=140 * HOUR),
            _w("fable", 0.80, length_s=SEVEN_DAY_S, ttr_s=140 * HOUR,
               applies_to=frozenset({"fable"})),
        ),
        tier=TIER_MAX_5X,
        source=SOURCE_CACHE,
        confidence=0.6,
    )
    # The statusline: fresher and better-ranked, but structurally blind to Fable.
    blind = AccountSnapshot(
        id="claude_c",
        windows=(
            _w("5h", 0.06, length_s=FIVE_HOUR_S, ttr_s=2 * HOUR),
            _w("7d", 0.43, length_s=SEVEN_DAY_S, ttr_s=140 * HOUR),
        ),
        tier=TIER_MAX_5X,
        source=SOURCE_LIVE,
        confidence=1.0,
    )

    class _A:
        name = "a"
        def __init__(self, snaps): self._s = snaps
        def snapshot(self, now_s): return list(self._s)

    merged, _warnings = collect_snapshots([_A([blind]), _A([complete])], NOW)
    got = next(s for s in merged if s.id == "claude_c")

    assert "fable" in {w.key for w in got.windows}, (
        "the merged snapshot dropped the Fable window that one source knew about; "
        f"kept only {[w.key for w in got.windows]}"
    )
    assert got.min_remaining_fraction("fable") == pytest.approx(0.20, abs=0.01), (
        "with the Fable window preserved the account must report its real Fable "
        f"headroom, got {got.min_remaining_fraction('fable')}"
    )


def test_an_account_blind_to_a_scoped_limit_is_excluded_not_treated_as_free() -> None:
    """When no source can see an account's Fable window, it is UNMEASURED, not free.

    The restore-from-another-source path only helps when some adapter reported the
    window. In the live failure nothing could: the usage endpoint was rate-limited
    and returned an empty snapshot, and the statusline is structurally blind to
    model-scoped limits. So the account genuinely had no Fable window from any
    source -- and scoring it as unconstrained is the one reading that is certainly
    wrong.

    The signal that the class is real is the rest of the fleet: if other accounts
    report a Fable window, Fable is a scoped limit on this plan, and an account
    missing one cannot be spoken for. If NO account reports one, absence is normal
    and nothing should be excluded.
    """
    from quota_router.cli import exclude_accounts_blind_to_model_class

    seeing = AccountSnapshot(
        id="claude",
        windows=(
            _w("7d", 0.78, length_s=SEVEN_DAY_S, ttr_s=40 * HOUR),
            _w("fable", 0.96, length_s=SEVEN_DAY_S, ttr_s=40 * HOUR,
               applies_to=frozenset({"fable"})),
        ),
        tier=TIER_MAX_20X,
        source=SOURCE_LIVE,
    )
    blind = AccountSnapshot(
        id="claude_c",
        windows=(_w("7d", 0.43, length_s=SEVEN_DAY_S, ttr_s=140 * HOUR),),
        tier=TIER_MAX_5X,
        source=SOURCE_LIVE,
    )

    kept, dropped = exclude_accounts_blind_to_model_class([seeing, blind], "fable")
    assert [s.id for s in kept] == ["claude"]
    assert [d["account"] for d in dropped] == ["claude_c"]
    assert "fable" in dropped[0]["reason"], dropped[0]["reason"]

    # Non-Fable work is unaffected: no account is scoped for opus, so absence is normal.
    kept, dropped = exclude_accounts_blind_to_model_class([seeing, blind], "opus")
    assert [s.id for s in kept] == ["claude", "claude_c"]
    assert dropped == []

    # And when nobody reports a Fable window, absence is normal for everyone.
    kept, dropped = exclude_accounts_blind_to_model_class([blind], "fable")
    assert [s.id for s in kept] == ["claude_c"]
    assert dropped == []
