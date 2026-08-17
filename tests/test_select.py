"""Tests for :mod:`quota_router.select` -- eligibility, hysteresis, shrinkage.

The first test in this file is the one that matters most and was written first: a
1000-step random walk that would make a naive ``argmax`` flip hundreds of times, with a
hard assertion that the router switches at most ``steps / min_dwell_calls`` times. Every
other guarantee in this module (a switch margin on the *unscaled* ``min_slack``, a dwell
counted in CALLS rather than seconds, sticky state keyed by the candidate set) exists to
serve that bound, so it is the regression that must never go red.
"""

from __future__ import annotations

import ast
import json
import random
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from quota_router.select import (
    DEFAULT_MAX_DWELL_S,
    DEFAULT_MIN_DWELL_CALLS,
    DEFAULT_SWITCH_MARGIN_ABS,
    DEFAULT_SWITCH_MARGIN_RATIO,
    SelectionState,
    StickyEntry,
    select,
    sticky_key,
)
from quota_router.types import (
    ACCOUNT_CLAUDE,
    ACCOUNT_CLAUDE_B,
    ACCOUNT_CLAUDE_C,
    MODEL_CLASS_FABLE,
    REGIME_A,
    REGIME_B,
    SOURCE_CACHE,
    SOURCE_LIVE,
    TIER_MAX_5X,
    TIER_MAX_20X,
    AccountSnapshot,
    Window,
)

# --------------------------------------------------------------------------------------
# Shared scaffolding
# --------------------------------------------------------------------------------------

FIVE_HOUR_S = 5 * 60 * 60.0
SEVEN_DAY_S = 7 * 24 * 60 * 60.0
DAY_S = 24 * 60 * 60.0

#: Arbitrary but fixed epoch. Nothing in the library reads a clock, so the absolute value
#: is irrelevant -- only the differences matter.
NOW = 1_700_000_000.0


def win(
    key: str,
    used: float,
    *,
    expected_used: float | None = None,
    length_s: float = SEVEN_DAY_S,
    ttr_s: float = DAY_S,
    now_s: float = NOW,
    applies_to: frozenset[str] | None = None,
) -> Window:
    """Build a :class:`Window` that resets ``ttr_s`` seconds after ``now_s``."""
    return Window(
        key=key,
        used_fraction=used,
        length_s=length_s,
        resets_at_s=now_s + ttr_s,
        observed_at_s=now_s,
        applies_to=applies_to,
        expected_used_fraction=expected_used,
    )


def acct(
    account_id: str,
    *windows: Window,
    tier: str = TIER_MAX_20X,
    confidence: float = 1.0,
    available: bool = True,
    source: str = SOURCE_LIVE,
) -> AccountSnapshot:
    """Build an :class:`AccountSnapshot` with sane test defaults."""
    return AccountSnapshot(
        id=account_id,
        windows=windows,
        tier=tier,
        source=source,
        confidence=confidence,
        available=available,
    )


def pair(used_a: float, used_b: float, now_s: float, *, expected_used: float = 0.9):
    """Two same-tier accounts whose only difference is how much they have burned.

    Same tier and same provider means ``capacity == 1.0`` and ``provider_weight == 1.0``
    for both, so ``score == min_slack == expected_used - used``: the walk drives the
    score directly and the margin arithmetic in the assertions is exact.
    """
    return [
        acct(ACCOUNT_CLAUDE, win("seven_day", used_a, expected_used=expected_used, now_s=now_s)),
        acct(ACCOUNT_CLAUDE_B, win("seven_day", used_b, expected_used=expected_used, now_s=now_s)),
    ]


def wobble(rng: random.Random, value: float, lo: float, hi: float, step: float) -> float:
    """One reflected random-walk step of ``value`` inside ``[lo, hi]``."""
    return max(lo, min(hi, value + rng.uniform(-step, step)))


# --------------------------------------------------------------------------------------
# (1) ANTI-PING-PONG -- written first, most important
# --------------------------------------------------------------------------------------


def test_anti_ping_pong_random_walk_inside_the_margin() -> None:
    """1000 steps of two accounts wobbling *inside* the switch margin -> no thrashing.

    Both accounts hold ``used`` in ``[0.58, 0.62]`` against a fixed pacing baseline of
    0.90, so every ``min_slack`` lands in ``[0.28, 0.32]`` and the largest possible gap
    between the two is 0.04. The switch margin at the bottom of that band is
    ``0.28 * 1.15 + 0.02 = 0.342``, comfortably above the top of it, so the margin alone
    forbids every switch -- and the dwell floor would cap them anyway.

    Time advances only 0.5s per step (500s total, under ``max_dwell_s``) so the bound
    being asserted is the *call-count* dwell, not the wall-clock escape hatch.
    """
    steps = 1000
    rng = random.Random(20260814)
    state = SelectionState()

    now = NOW
    used_a = used_b = 0.60
    chosen_prev: str | None = None
    naive_prev: str | None = None
    switches = 0
    naive_flips = 0
    sticky_steps = 0

    for _ in range(steps):
        used_a = wobble(rng, used_a, 0.58, 0.62, 0.01)
        used_b = wobble(rng, used_b, 0.58, 0.62, 0.01)

        decision = select(pair(used_a, used_b, now), now, state=state)
        assert decision.chosen is not None
        assert decision.regime == REGIME_A

        if chosen_prev is not None and decision.chosen != chosen_prev:
            switches += 1
        chosen_prev = decision.chosen
        if decision.sticky_applied:
            sticky_steps += 1

        # What a memoryless argmax would have done with the same numbers.
        naive = ACCOUNT_CLAUDE if used_a <= used_b else ACCOUNT_CLAUDE_B
        if naive_prev is not None and naive != naive_prev:
            naive_flips += 1
        naive_prev = naive

        now += 0.5

    # The premise of the test: a naive router really would have thrashed.
    assert naive_flips > 50, f"walk did not exercise the margin (naive flips={naive_flips})"

    # The guarantee.
    assert switches <= steps // DEFAULT_MIN_DWELL_CALLS
    # Stronger, and specific to this band: the margin forbids every switch outright.
    assert switches == 0
    assert sticky_steps > 0, "hysteresis never engaged; the walk is not testing anything"


def test_adversarial_jitter_is_bounded_by_the_call_count_dwell() -> None:
    """The dwell, not the margin, is what holds when the oracle is pure noise.

    Every step resamples both accounts independently across the whole ``[0.1, 0.9]`` slack
    range, so on roughly a third of the steps the challenger clears the switch margin
    outright -- measured at ~450 switches per 1000 calls with the dwell removed, well past
    the ``steps / min_dwell_calls`` ceiling. The dwell is the only thing that can hold the
    line here, which makes this the test that actually pins ``min_dwell_calls``.
    """
    steps = 1000
    rng = random.Random(4242)
    state = SelectionState()

    now = NOW
    chosen_prev: str | None = None
    naive_prev: str | None = None
    switches = 0
    naive_flips = 0

    for _ in range(steps):
        used_a = rng.uniform(0.0, 0.8)
        used_b = rng.uniform(0.0, 0.8)

        decision = select(pair(used_a, used_b, now), now, state=state)
        assert decision.chosen is not None
        if chosen_prev is not None and decision.chosen != chosen_prev:
            switches += 1
        chosen_prev = decision.chosen

        naive = ACCOUNT_CLAUDE if used_a <= used_b else ACCOUNT_CLAUDE_B
        if naive_prev is not None and naive != naive_prev:
            naive_flips += 1
        naive_prev = naive

        now += 0.5

    assert naive_flips > 400, f"jitter did not thrash a naive router (flips={naive_flips})"
    assert switches > 0, "nothing ever switched; the bound would be vacuous"
    assert switches <= steps // DEFAULT_MIN_DWELL_CALLS


def test_dwell_is_counted_in_calls_not_seconds() -> None:
    """Three calls at the *same* instant still retire the dwell; time never moves."""
    state = SelectionState()
    incumbent = pair(0.30, 0.90, NOW)  # claude far ahead -> claude wins first
    assert select(incumbent, NOW, state=state).chosen == ACCOUNT_CLAUDE

    # claude_b now dominates by far more than the margin, but the dwell holds.
    swapped = pair(0.90, 0.05, NOW)
    second = select(swapped, NOW, state=state)
    assert second.chosen == ACCOUNT_CLAUDE
    assert second.sticky_applied is True

    third = select(swapped, NOW, state=state)
    assert third.chosen == ACCOUNT_CLAUDE
    assert third.sticky_applied is True

    # Fourth call: the incumbent has served min_dwell_calls == 3, the margin decides.
    fourth = select(swapped, NOW, state=state)
    assert fourth.chosen == ACCOUNT_CLAUDE_B
    assert fourth.sticky_applied is False


def test_max_dwell_s_releases_the_call_count_dwell() -> None:
    """A low-frequency caller is not pinned forever by an unmet call-count dwell."""
    state = SelectionState()
    assert select(pair(0.30, 0.90, NOW), NOW, state=state).chosen == ACCOUNT_CLAUDE

    swapped_at = NOW + 1.0
    held = select(pair(0.90, 0.05, swapped_at), swapped_at, state=state)
    assert held.chosen == ACCOUNT_CLAUDE and held.sticky_applied is True

    late = NOW + DEFAULT_MAX_DWELL_S + 1.0
    released = select(pair(0.90, 0.05, late), late, state=state)
    assert released.chosen == ACCOUNT_CLAUDE_B
    assert released.sticky_applied is False


def test_switch_margin_uses_unscaled_min_slack_not_the_scaled_score() -> None:
    """The 4x-stricter trap: an additive epsilon must not be applied to a scaled score.

    Incumbent ``claude_c`` is ``max_5x`` (capacity 0.25) with ``min_slack = 0.20``, so it
    scores 0.05. Challenger ``claude`` is ``max_20x`` with ``min_slack = 0.21`` and scores
    0.21 -- it ranks first. On the *unscaled* slacks the margin is not met
    (``0.21 <= 0.20 * 1.15 + 0.02 = 0.25``) so the incumbent must be kept. Had the margin
    been applied to the scores it would have been met four times over
    (``0.21 > 0.05 * 1.15 + 0.02 = 0.0775``) and the router would have switched.
    """
    incumbent = acct(
        ACCOUNT_CLAUDE_C, win("seven_day", 0.70, expected_used=0.90), tier=TIER_MAX_5X
    )
    challenger = acct(
        ACCOUNT_CLAUDE, win("seven_day", 0.69, expected_used=0.90), tier=TIER_MAX_20X
    )

    state = SelectionState()
    state.record(sticky_key([ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_C], None), ACCOUNT_CLAUDE_C, NOW)
    state.entries[sticky_key([ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_C], None)].calls = 99  # dwell retired

    decision = select([incumbent, challenger], NOW, state=state)

    by_id = {row.account_id: row for row in decision.ranked}
    assert by_id[ACCOUNT_CLAUDE_C].min_slack == pytest.approx(0.20)
    assert by_id[ACCOUNT_CLAUDE].min_slack == pytest.approx(0.21)
    assert by_id[ACCOUNT_CLAUDE].score > by_id[ACCOUNT_CLAUDE_C].score  # challenger ranks first

    margin_on_slack = 0.20 * DEFAULT_SWITCH_MARGIN_RATIO + DEFAULT_SWITCH_MARGIN_ABS
    margin_on_score = 0.05 * DEFAULT_SWITCH_MARGIN_RATIO + DEFAULT_SWITCH_MARGIN_ABS
    assert 0.21 <= margin_on_slack  # not met on the unscaled quantity
    assert 0.21 > margin_on_score  # would have been met on the scaled one

    assert decision.chosen == ACCOUNT_CLAUDE_C
    assert decision.sticky_applied is True
    assert decision.ranked[0].account_id == ACCOUNT_CLAUDE_C  # chosen is always first


def test_sticky_state_is_keyed_by_candidate_set_and_model_class() -> None:
    """Changing the eligible set (or the model class) starts a fresh dwell."""
    assert sticky_key(["b", "a"], None) == sticky_key(["a", "b"], None)
    assert sticky_key(["a", "b"], None) != sticky_key(["a", "b"], MODEL_CLASS_FABLE)
    assert sticky_key(["a", "b"], None) != sticky_key(["a", "b", "c"], None)
    assert sticky_key(["a"], "Fable") == sticky_key(["a"], "fable")

    state = SelectionState()
    assert select(pair(0.30, 0.90, NOW), NOW, state=state).chosen == ACCOUNT_CLAUDE

    # A third candidate appears -> different key -> the dwell no longer protects claude.
    third = acct(ACCOUNT_CLAUDE_C, win("seven_day", 0.95, expected_used=0.90))
    trio = [*pair(0.90, 0.05, NOW), third]
    decision = select(trio, NOW, state=state)
    assert decision.chosen == ACCOUNT_CLAUDE_B
    assert decision.sticky_applied is False


# --------------------------------------------------------------------------------------
# (3) Eligibility
# --------------------------------------------------------------------------------------


def test_exhausted_until_in_the_future_is_never_ranked_first() -> None:
    """A cooling-off account is ineligible *regardless of score*."""
    hot = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.05, expected_used=0.90))  # slack 0.85
    ok = acct(ACCOUNT_CLAUDE, win("seven_day", 0.80, expected_used=0.90))  # slack 0.10

    state = SelectionState()
    state.mark_exhausted(ACCOUNT_CLAUDE_B, NOW + 300.0)
    decision = select([hot, ok], NOW, state=state)

    assert decision.chosen == ACCOUNT_CLAUDE
    assert [row.account_id for row in decision.ranked] == [ACCOUNT_CLAUDE]
    excluded = {row.account_id: row for row in decision.excluded}
    assert "exhaust" in excluded[ACCOUNT_CLAUDE_B].reason
    # It genuinely had the better number -- exclusion is not a scoring artefact.
    assert excluded[ACCOUNT_CLAUDE_B].score > decision.ranked[0].score


def test_exhausted_until_accepts_a_per_call_mapping_and_expires() -> None:
    hot = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.05, expected_used=0.90))
    ok = acct(ACCOUNT_CLAUDE, win("seven_day", 0.80, expected_used=0.90))

    blocked = select([hot, ok], NOW, exhausted_until={ACCOUNT_CLAUDE_B: NOW + 60.0})
    assert blocked.chosen == ACCOUNT_CLAUDE

    # Same deadline, later clock: the cooldown has expired.
    freed = select([hot, ok], NOW + 61.0, exhausted_until={ACCOUNT_CLAUDE_B: NOW + 60.0})
    assert freed.chosen == ACCOUNT_CLAUDE_B


def test_unavailable_accounts_are_excluded() -> None:
    down = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.05, expected_used=0.90), available=False)
    ok = acct(ACCOUNT_CLAUDE, win("seven_day", 0.80, expected_used=0.90))
    decision = select([down, ok], NOW)
    assert decision.chosen == ACCOUNT_CLAUDE
    assert {row.account_id for row in decision.excluded} == {ACCOUNT_CLAUDE_B}
    assert "unavailable" in decision.excluded[0].reason


def test_min_remaining_floor_excludes_and_is_configurable() -> None:
    thin = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.97, expected_used=0.10))  # slack -0.87
    ok = acct(ACCOUNT_CLAUDE, win("seven_day", 0.50, expected_used=0.10))

    default_run = select([thin, ok], NOW)
    assert {row.account_id for row in default_run.ranked} == {ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B}

    floored = select([thin, ok], NOW, cfg={"min_remaining_floor": 0.10})
    assert [row.account_id for row in floored.ranked] == [ACCOUNT_CLAUDE]
    assert "floor" in floored.excluded[0].reason


def test_a_fully_used_window_cannot_serve_the_call() -> None:
    """A 100%-used window is ineligible under the default (exclusive) zero floor."""
    dead = acct(ACCOUNT_CLAUDE_B, win("seven_day", 1.0, expected_used=0.957))
    ok = acct(ACCOUNT_CLAUDE, win("seven_day", 0.60, expected_used=0.546))
    decision = select([dead, ok], NOW)
    assert decision.chosen == ACCOUNT_CLAUDE
    assert {row.account_id for row in decision.excluded} == {ACCOUNT_CLAUDE_B}
    assert decision.excluded[0].fits is False


def test_no_eligible_candidate_returns_a_choiceless_decision() -> None:
    dead = acct(ACCOUNT_CLAUDE, win("seven_day", 1.0, expected_used=0.90))
    down = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.10, expected_used=0.90), available=False)
    decision = select([dead, down], NOW)
    assert decision.chosen is None
    assert decision.has_choice is False
    assert decision.ranked == ()
    assert len(decision.excluded) == 2
    assert decision.degraded is True
    assert decision.regime is None


def test_select_never_raises_on_ordinary_degenerate_inputs() -> None:
    assert select([], NOW).chosen is None
    assert select([acct(ACCOUNT_CLAUDE)], NOW).chosen is None  # no windows at all
    windowless = select([acct(ACCOUNT_CLAUDE)], NOW)
    assert windowless.degraded is True
    assert "window" in windowless.excluded[0].reason

    # A window that applies to a different model class leaves nothing to score.
    scoped_only = acct(
        ACCOUNT_CLAUDE,
        win("scoped:fable", 0.10, expected_used=0.90, applies_to=frozenset({MODEL_CLASS_FABLE})),
    )
    assert select([scoped_only], NOW, model_class="opus").chosen is None


# --------------------------------------------------------------------------------------
# (4) Model-class gating, end to end
# --------------------------------------------------------------------------------------


def test_model_class_gating_changes_the_winner() -> None:
    """``claude`` has the better account-wide slack; ``claude_b`` the better Fable slack."""
    a = acct(
        ACCOUNT_CLAUDE,
        win("seven_day", 0.10, expected_used=0.90),  # slack 0.80
        win("scoped:fable", 0.85, expected_used=0.90, applies_to=frozenset({MODEL_CLASS_FABLE})),
    )
    b = acct(
        ACCOUNT_CLAUDE_B,
        win("seven_day", 0.30, expected_used=0.90),  # slack 0.60
        win("scoped:fable", 0.40, expected_used=0.90, applies_to=frozenset({MODEL_CLASS_FABLE})),
    )

    opus = select([a, b], NOW, model_class="opus")
    assert opus.chosen == ACCOUNT_CLAUDE
    assert opus.chosen_breakdown is not None
    assert opus.chosen_breakdown.binding_window == "seven_day"

    fable = select([a, b], NOW, model_class=MODEL_CLASS_FABLE)
    assert fable.chosen == ACCOUNT_CLAUDE_B
    assert fable.chosen_breakdown is not None
    assert fable.chosen_breakdown.binding_window == "scoped:fable"


# --------------------------------------------------------------------------------------
# Confidence shrinkage
# --------------------------------------------------------------------------------------


def test_confidence_shrinks_toward_the_mean_rather_than_multiplying() -> None:
    """Low confidence pulls a score toward the field, it does not scale it to zero."""
    strong = acct(ACCOUNT_CLAUDE, win("seven_day", 0.0, expected_used=1.0), confidence=0.3)  # 1.00
    steady = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.0, expected_used=0.6))  # 0.60

    decision = select([strong, steady], NOW)
    scores = {row.account_id: row.score for row in decision.ranked}

    mean = (1.0 + 0.6) / 2
    assert scores[ACCOUNT_CLAUDE] == pytest.approx(0.3 * 1.0 + 0.7 * mean)
    assert scores[ACCOUNT_CLAUDE_B] == pytest.approx(0.6)
    # Multiplying by confidence (0.3 * 1.0 = 0.30) would have handed this to claude_b.
    assert 0.3 * 1.0 < 0.6
    assert decision.chosen == ACCOUNT_CLAUDE
    assert decision.degraded is True
    assert any("confidence" in warning for warning in decision.warnings)


def test_zero_confidence_collapses_a_score_onto_the_mean() -> None:
    blind = acct(ACCOUNT_CLAUDE, win("seven_day", 0.0, expected_used=1.0), confidence=0.0)  # 1.00
    known = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.0, expected_used=0.9))  # 0.90
    weak = acct(ACCOUNT_CLAUDE_C, win("seven_day", 0.0, expected_used=0.2))  # 0.20

    decision = select([blind, known, weak], NOW)
    assert decision.chosen == ACCOUNT_CLAUDE_B
    shrunk = {row.account_id: row.score for row in decision.ranked}
    assert shrunk[ACCOUNT_CLAUDE] == pytest.approx((1.0 + 0.9 + 0.2) / 3)
    # Hysteresis compares raw slack, which shrinkage must never touch.
    raw = {row.account_id: row.min_slack for row in decision.ranked}
    assert raw[ACCOUNT_CLAUDE] == pytest.approx(1.0)


# --------------------------------------------------------------------------------------
# (5) Determinism / (6) the operator's real case
# --------------------------------------------------------------------------------------


def test_identical_inputs_produce_identical_decisions() -> None:
    snaps = [
        acct(ACCOUNT_CLAUDE, win("seven_day", 0.30, expected_used=0.98)),
        acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.35, expected_used=0.14)),
        acct(ACCOUNT_CLAUDE_C, win("seven_day", 0.35, expected_used=0.14), tier=TIER_MAX_5X),
    ]
    first = select(snaps, NOW, state=SelectionState())
    second = select(snaps, NOW, state=SelectionState())
    assert first.to_dict() == second.to_dict()
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_operator_real_case_expiring_weekly_quota_wins() -> None:
    """claude: 30% used, 3h to the weekly reset. claude_b: 35% used, 6 days to go.

    claude is sitting on 70% of a budget that evaporates in three hours; claude_b is
    ahead of pace with most of a week to spend it. Regime A must spend claude's.
    """
    claude = acct(ACCOUNT_CLAUDE, win("seven_day", 0.30, ttr_s=3 * 60 * 60.0))
    claude_b = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.35, ttr_s=6 * DAY_S))

    decision = select([claude, claude_b], NOW)
    assert decision.chosen == ACCOUNT_CLAUDE
    assert decision.regime == REGIME_A
    assert decision.ranked[0].min_slack > 0.0
    assert decision.ranked[1].min_slack < 0.0


# --------------------------------------------------------------------------------------
# Real captured oracle payload
# --------------------------------------------------------------------------------------

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cswap_real.json"


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _snapshot_from_fixture(account: dict, account_id: str, tier: str = TIER_MAX_20X):
    """Build windows from the captured payload -- local so these tests own no other file.

    A provider that publishes a pacing baseline (the statusline reader) lands exactly
    here; the live usage endpoint publishes none and lets ``Window`` derive one. Both
    reach ``select`` as plain windows, which is the only thing this file is testing.
    """
    observed = _epoch(account["usageFetchedAt"])
    usage = account["usage"]
    windows = [
        Window(
            key="five_hour",
            used_fraction=usage["fiveHour"]["pct"] / 100.0,
            length_s=FIVE_HOUR_S,
            resets_at_s=_epoch(usage["fiveHour"]["resetsAt"]),
            observed_at_s=observed,
        ),
        Window(
            key="seven_day",
            used_fraction=usage["sevenDay"]["pct"] / 100.0,
            length_s=SEVEN_DAY_S,
            resets_at_s=_epoch(usage["sevenDay"]["resetsAt"]),
            observed_at_s=observed,
            expected_used_fraction=usage["sevenDay"]["expectedPct"] / 100.0,
        ),
    ]
    for scoped in usage.get("scoped", ()):
        name = scoped["name"].casefold()
        windows.append(
            Window(
                key=f"scoped:{name}",
                used_fraction=scoped["pct"] / 100.0,
                length_s=SEVEN_DAY_S,
                resets_at_s=_epoch(scoped["resetsAt"]),
                observed_at_s=observed,
                applies_to=frozenset({name}),
                expected_used_fraction=scoped["expectedPct"] / 100.0,
            )
        )
    return AccountSnapshot(
        id=account_id, windows=tuple(windows), tier=tier, source=SOURCE_LIVE, confidence=1.0
    )


def test_real_capture_routes_fable_to_the_account_that_can_serve_it() -> None:
    """All three real accounts are behind pace on Fable -> regime B picks the roomiest.

    acct1 fable: 80% used, slack ``0.546 - 0.80 = -0.254``, 20% left.
    acct2 fable: 88% used, slack ``0.344 - 0.88 = -0.536``, 12% left.
    acct3 fable: 100% used -> cannot serve the call at all, excluded before scoring.

    The naive "scale the negative slack" bug would rank acct1's ``-0.254`` behind nothing
    here, but the point stands: regime B ranks on *remaining*, so acct1 (0.20) beats
    acct2 (0.12) even though both are underwater.
    """
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ids = (ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B, ACCOUNT_CLAUDE_C)
    snaps = [
        _snapshot_from_fixture(account, account_id)
        for account, account_id in zip(payload["accounts"], ids, strict=True)
    ]
    now = _epoch("2026-08-14T18:48:00+00:00")

    decision = select(snaps, now, model_class=MODEL_CLASS_FABLE)

    assert decision.regime == REGIME_B
    assert decision.chosen == ACCOUNT_CLAUDE
    assert [row.account_id for row in decision.ranked] == [ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B]
    assert decision.ranked[0].min_slack == pytest.approx(-0.254)
    assert decision.ranked[0].min_remaining == pytest.approx(0.20)
    assert decision.ranked[1].min_slack == pytest.approx(-0.536)
    assert {row.account_id for row in decision.excluded} == {ACCOUNT_CLAUDE_C}


def test_real_capture_ignores_the_upstream_ahead_of_pace_boolean() -> None:
    """acct1 reports ``aheadOfPace: false`` for ``pct 60.0 > expectedPct 54.6``."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    seven_day = payload["accounts"][0]["usage"]["sevenDay"]
    assert seven_day["aheadOfPace"] is False and seven_day["pct"] > seven_day["expectedPct"]

    snap = _snapshot_from_fixture(payload["accounts"][0], ACCOUNT_CLAUDE)
    now = _epoch("2026-08-14T18:48:00+00:00")
    window = snap.window("seven_day")
    assert window is not None
    from quota_router.scoring import window_slack

    assert window_slack(window, now) == pytest.approx(-0.054)


# --------------------------------------------------------------------------------------
# Config plumbing, state persistence, purity
# --------------------------------------------------------------------------------------


def test_config_accepts_mappings_and_attribute_objects() -> None:
    snaps = pair(0.30, 0.40, NOW)  # claude ahead on slack
    weights = {"provider_weights": {"claude": 0.0}}
    assert select(snaps, NOW, cfg=weights).ranked[0].score == pytest.approx(0.0)

    tuned = SimpleNamespace(min_dwell_calls=1)
    state = SelectionState()
    assert select(pair(0.30, 0.90, NOW), NOW, cfg=tuned, state=state).chosen == ACCOUNT_CLAUDE
    switched = select(pair(0.90, 0.05, NOW), NOW, cfg=tuned, state=state)
    assert switched.chosen == ACCOUNT_CLAUDE_B  # dwell of 1 call is already satisfied


def test_direct_keyword_overrides_beat_the_config_object() -> None:
    """The CLI keeps policy in a config layer this module may not import, so it may
    hand the individual values straight in as keywords."""
    thin = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.97, expected_used=0.10))
    ok = acct(ACCOUNT_CLAUDE, win("seven_day", 0.50, expected_used=0.10))

    floored = select([thin, ok], NOW, min_remaining=0.10)
    assert [row.account_id for row in floored.ranked] == [ACCOUNT_CLAUDE]

    weighted = select(pair(0.30, 0.40, NOW), NOW, provider_weights={"claude": 0.25})
    assert weighted.ranked[0].provider_weight == pytest.approx(0.25)
    assert weighted.ranked[0].score == pytest.approx(0.25 * 0.60)

    # An unusable override is reported, not raised, and the default stands.
    sloppy = select(pair(0.30, 0.40, NOW), NOW, min_remaining="lots")
    assert sloppy.chosen == ACCOUNT_CLAUDE
    assert any("min_remaining" in warning for warning in sloppy.warnings)


def test_a_bare_sticky_hint_is_governed_by_the_margin_alone() -> None:
    """A caller with its own (TTL-based) stickiness can pass just the incumbent id."""
    # Incumbent slack 0.00, challenger 0.01: short of the 0.02 additive margin.
    inside = select(pair(0.90, 0.89, NOW), NOW, sticky=ACCOUNT_CLAUDE)
    assert inside.chosen == ACCOUNT_CLAUDE
    assert inside.sticky_applied is True

    outside = select(pair(0.90, 0.05, NOW), NOW, sticky=ACCOUNT_CLAUDE)
    assert outside.chosen == ACCOUNT_CLAUDE_B  # 0.85 clears it comfortably
    assert outside.sticky_applied is False

    # An override of the additive margin term is honoured, unscaled.
    widened = select(pair(0.90, 0.05, NOW), NOW, sticky=ACCOUNT_CLAUDE, switch_margin=0.90)
    assert widened.chosen == ACCOUNT_CLAUDE
    assert widened.sticky_applied is True

    # A hint naming an account that is not in the field is simply ignored.
    assert select(pair(0.90, 0.05, NOW), NOW, sticky="nobody").chosen == ACCOUNT_CLAUDE_B
    assert select(pair(0.90, 0.05, NOW), NOW, sticky=StickyEntry(ACCOUNT_CLAUDE)).chosen == (
        ACCOUNT_CLAUDE_B
    )


def test_unparsable_config_knob_degrades_instead_of_raising() -> None:
    decision = select(pair(0.30, 0.40, NOW), NOW, cfg={"min_dwell_calls": "three"})
    assert decision.chosen is not None
    assert decision.degraded is True
    assert any("min_dwell_calls" in warning for warning in decision.warnings)


def test_selection_state_round_trips_through_plain_json() -> None:
    state = SelectionState()
    key = sticky_key([ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B], MODEL_CLASS_FABLE)
    state.record(key, ACCOUNT_CLAUDE, NOW)
    state.record(key, ACCOUNT_CLAUDE, NOW + 1.0)
    state.mark_exhausted(ACCOUNT_CLAUDE_B, NOW + 300.0)

    restored = SelectionState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert restored.to_dict() == state.to_dict()
    incumbent = restored.incumbent(key)
    assert isinstance(incumbent, StickyEntry)
    assert incumbent.account_id == ACCOUNT_CLAUDE and incumbent.calls == 2
    assert restored.is_exhausted(ACCOUNT_CLAUDE_B, NOW) is True
    assert restored.is_exhausted(ACCOUNT_CLAUDE_B, NOW + 301.0) is False


def test_a_plain_mutable_mapping_works_as_state_and_is_written_back() -> None:
    """So a caller can persist the state as JSON without importing our dataclasses."""
    state: dict = {}
    assert select(pair(0.30, 0.90, NOW), NOW, state=state).chosen == ACCOUNT_CLAUDE
    assert state["entries"], state
    entry = next(iter(state["entries"].values()))
    assert entry["account_id"] == ACCOUNT_CLAUDE and entry["calls"] == 1

    # The dwell survives the round trip through the mapping.
    held = select(pair(0.90, 0.05, NOW), NOW, state=state)
    assert held.chosen == ACCOUNT_CLAUDE and held.sticky_applied is True
    assert next(iter(state["entries"].values()))["calls"] == 2


def test_hysteresis_also_holds_under_scarcity() -> None:
    """Regime B jitter is dwell-protected too, where the margin is at its weakest."""
    state = SelectionState()
    # Both underwater; claude has more left, so it takes the seat first.
    first = select(
        [
            acct(ACCOUNT_CLAUDE, win("seven_day", 0.55, expected_used=0.35)),
            acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.75, expected_used=0.35)),
        ],
        NOW,
        state=state,
    )
    assert first.regime == REGIME_B and first.chosen == ACCOUNT_CLAUDE

    flipped = [
        acct(ACCOUNT_CLAUDE, win("seven_day", 0.75, expected_used=0.35)),
        acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.55, expected_used=0.35)),
    ]
    second = select(flipped, NOW, state=state)
    assert second.chosen == ACCOUNT_CLAUDE  # dwell, not margin, is holding the line
    assert second.sticky_applied is True
    assert second.regime == REGIME_B


def test_record_false_leaves_the_state_untouched() -> None:
    state = SelectionState()
    select(pair(0.30, 0.90, NOW), NOW, state=state, record=False)
    assert state.entries == {}


def test_stale_snapshot_is_reported_as_degraded() -> None:
    stale = acct(
        ACCOUNT_CLAUDE,
        win("seven_day", 0.30, expected_used=0.90, now_s=NOW - 10_000.0),
        source=SOURCE_CACHE,
    )
    decision = select([stale], NOW)
    assert decision.chosen == ACCOUNT_CLAUDE
    assert decision.degraded is True
    assert any("stale" in warning or "source" in warning for warning in decision.warnings)


# --------------------------------------------------------------------------------------
# Unreadable accounts: a failed read is not an empty measurement
#
# The CLI learned this in 62caa4b; the pure layer did not, and it is a separate entry
# point -- ``select()`` is public, takes no Config, and is reachable without the CLI's
# policy pass ever running. On that route an unread account was still misdiagnosed.
# --------------------------------------------------------------------------------------


def dark(account_id: str, note: str | None) -> AccountSnapshot:
    """An account whose usage read failed, exactly as the OAuth provider reports it.

    No windows, ``available=False``, ``confidence=0.0``, and a ``note`` naming the cause.
    ``source`` stays ``live`` because the provider labels the attempt, not the outcome.
    """
    return AccountSnapshot(
        id=account_id,
        windows=(),
        tier=TIER_MAX_20X,
        source=SOURCE_LIVE,
        confidence=0.0,
        available=False,
        note=note,
    )


def test_an_unreadable_account_is_diagnosed_not_merely_called_unavailable() -> None:
    """"unavailable" is what the flag says; it is not what happened.

    ``account unavailable (acct4@example.com: access token expired)`` reads as an aside
    -- the parenthetical could be a rate-limit, a logout, a disabled account. The router
    knows something stronger and has to say it: no reading was obtained, so this
    account's headroom is UNKNOWN rather than zero, and the fix is a login, not a wait.
    """
    down = dark(ACCOUNT_CLAUDE_B, "acct4@example.com: access token expired")
    ok = acct(ACCOUNT_CLAUDE, win("seven_day", 0.20, expected_used=0.90))

    decision = select([down, ok], NOW)

    row = {r.account_id: r for r in decision.excluded}[ACCOUNT_CLAUDE_B]
    assert "unreadable" in row.reason, row.reason
    assert "unknown" in row.reason, row.reason
    assert "access token expired" in row.reason, row.reason
    assert row.min_remaining is None, (
        "an unread account's remaining quota is unknown; 0.0 would read as spent"
    )


def test_an_unreadable_account_with_no_recorded_cause_still_reports_the_failure() -> None:
    """The verdict cannot depend on the source having bothered to write a note."""
    decision = select(
        [dark(ACCOUNT_CLAUDE_B, None), acct(ACCOUNT_CLAUDE, win("seven_day", 0.20))], NOW
    )

    row = {r.account_id: r for r in decision.excluded}[ACCOUNT_CLAUDE_B]
    assert "unreadable" in row.reason, row.reason
    assert "unknown" in row.reason, row.reason


def test_an_unreadable_account_does_not_warn_about_the_shape_of_its_data() -> None:
    """``snapshot carries no usage windows`` is a data-shape complaint, not the truth.

    That warning is what the live incident left behind, and it sends the reader hunting
    for a parsing bug when the cause was an expired token. It must be replaced by the
    real diagnosis -- not merely deleted, because the decision still has to come back
    degraded when part of the fleet is dark.
    """
    down = dark(ACCOUNT_CLAUDE_B, "acct4@example.com: access token expired")
    ok = acct(ACCOUNT_CLAUDE, win("seven_day", 0.20, expected_used=0.90))

    decision = select([down, ok], NOW)

    about_the_dark_one = [w for w in decision.warnings if ACCOUNT_CLAUDE_B in w]
    assert about_the_dark_one, f"the dark account went unmentioned: {decision.warnings}"
    assert not any("carries no usage windows" in w for w in about_the_dark_one), (
        f"misdiagnosed a failed read as a data-shape problem: {about_the_dark_one}"
    )
    assert any("unreadable" in w for w in about_the_dark_one), about_the_dark_one
    assert decision.degraded is True


def test_an_unread_account_reads_differently_from_a_spent_one() -> None:
    """They demand opposite responses: one needs a login, the other needs a wait."""
    spent = acct(ACCOUNT_CLAUDE_C, win("seven_day", 1.0, expected_used=0.90))
    down = dark(ACCOUNT_CLAUDE_B, "acct4@example.com: access token expired")
    ok = acct(ACCOUNT_CLAUDE, win("seven_day", 0.20, expected_used=0.90))

    decision = select([spent, down, ok], NOW)

    rows = {r.account_id: r for r in decision.excluded}
    assert "floor" in rows[ACCOUNT_CLAUDE_C].reason, rows[ACCOUNT_CLAUDE_C].reason
    assert "floor" not in rows[ACCOUNT_CLAUDE_B].reason, rows[ACCOUNT_CLAUDE_B].reason
    assert "unreadable" in rows[ACCOUNT_CLAUDE_B].reason, rows[ACCOUNT_CLAUDE_B].reason
    assert "unreadable" not in rows[ACCOUNT_CLAUDE_C].reason
    assert rows[ACCOUNT_CLAUDE_C].min_remaining == 0.0
    assert rows[ACCOUNT_CLAUDE_B].min_remaining is None


def test_a_pool_that_publishes_no_windows_by_design_is_still_called_window_less() -> None:
    """Narrowing the window-shape warning must not switch it off for its own case.

    The Antigravity pools report no windows at ``confidence=0.0`` while remaining
    available and routable, so "no windows" cannot be the test for a failed read --
    ``available`` is. A pool that publishes nothing is genuinely unmeasured, and the
    warning that says so has to keep firing.
    """
    pool = acct("antigravity_gemini", confidence=0.0)  # available, and no windows at all
    ok = acct(ACCOUNT_CLAUDE, win("seven_day", 0.20, expected_used=0.90))

    decision = select([pool, ok], NOW)

    about_the_pool = [w for w in decision.warnings if "antigravity_gemini" in w]
    assert any("carries no usage windows" in w for w in about_the_pool), about_the_pool
    assert not any("unreadable" in w for w in about_the_pool), about_the_pool
    row = {r.account_id: r for r in decision.excluded}["antigravity_gemini"]
    assert "unreadable" not in row.reason, row.reason


def test_select_module_is_pure() -> None:
    """select.py may import stdlib, ``types`` and its pure sibling ``scoring`` -- nothing else."""
    import quota_router.select as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add(("." * node.level) + (node.module or "").split(".")[0])

    assert roots <= {
        "__future__",
        "math",
        "collections",
        "dataclasses",
        "typing",
        ".types",
        ".scoring",
    }, roots
    for forbidden in ("os", "sys", "subprocess", "pathlib", "time", "datetime", "socket"):
        assert forbidden not in roots
    assert "time.time" not in source
    assert "datetime.now" not in source
