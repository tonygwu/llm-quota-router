"""Tests for :mod:`quota_router.scoring` -- the pure slack/regime arithmetic.

The load-bearing cases here are the two-regime split (a negative slack must never be
scaled by a capacity ratio), the ``applies_to`` gate that makes a model class change
which windows bind, and the monotonicity properties the ranking depends on.
"""

from __future__ import annotations

import ast
import json
import math
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from quota_router.scoring import (
    account_score,
    applicable_windows,
    decide_regime,
    expected_demand,
    min_remaining,
    min_slack,
    provider_weight,
    rank,
    remaining_fraction,
    time_to_reset_s,
    window_slack,
)
from quota_router.types import (
    ACCOUNT_CLAUDE,
    ACCOUNT_CLAUDE_B,
    ACCOUNT_CLAUDE_C,
    MODEL_CLASS_FABLE,
    REGIME_A,
    REGIME_B,
    SOURCE_LIVE,
    TIER_MAX_5X,
    TIER_MAX_20X,
    AccountSnapshot,
    Window,
)

FIVE_HOUR_S = 5 * 60 * 60.0
SEVEN_DAY_S = 7 * 24 * 60 * 60.0
DAY_S = 24 * 60 * 60.0
NOW = 1_700_000_000.0

FABLE = frozenset({MODEL_CLASS_FABLE})


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
    return Window(
        key=key,
        used_fraction=used,
        length_s=length_s,
        resets_at_s=now_s + ttr_s,
        observed_at_s=now_s,
        applies_to=applies_to,
        expected_used_fraction=expected_used,
    )


def acct(account_id: str, *windows: Window, tier: str = TIER_MAX_20X) -> AccountSnapshot:
    return AccountSnapshot(id=account_id, windows=windows, tier=tier, source=SOURCE_LIVE)


# --------------------------------------------------------------------------------------
# Window primitives
# --------------------------------------------------------------------------------------


def test_remaining_fraction_is_one_minus_used() -> None:
    assert remaining_fraction(win("w", 0.0)) == pytest.approx(1.0)
    assert remaining_fraction(win("w", 0.6)) == pytest.approx(0.4)
    assert remaining_fraction(win("w", 1.0)) == pytest.approx(0.0)


def test_time_to_reset_is_clamped_at_zero() -> None:
    window = win("w", 0.5, ttr_s=3600.0)
    assert time_to_reset_s(window, NOW) == pytest.approx(3600.0)
    assert time_to_reset_s(window, NOW + 3600.0) == pytest.approx(0.0)
    assert time_to_reset_s(window, NOW + 10_000.0) == 0.0


def test_expected_demand_uses_the_upstream_baseline_verbatim() -> None:
    """With ``expectedPct`` present the identity ``slack == expected_used - used`` holds.

    These are the real numbers from account 1 of the captured payload: 60% used against a
    54.6% pacing baseline, three days out from the weekly reset. The elapsed-time prior
    would say something else entirely (the window is ~57% elapsed); the oracle's own
    baseline wins.
    """
    window = win("seven_day", 0.60, expected_used=0.546, ttr_s=3 * DAY_S)
    assert expected_demand(window, NOW) == pytest.approx(1.0 - 0.546)
    assert window_slack(window, NOW) == pytest.approx(0.546 - 0.60)
    assert window_slack(window, NOW) == pytest.approx(-0.054)


def test_expected_demand_falls_back_to_the_uniform_prior() -> None:
    """The 5-hour window ships no ``expectedPct``; ``burn_rate = 1 / length_s``."""
    window = win("five_hour", 0.12, length_s=FIVE_HOUR_S, ttr_s=720.0)
    assert expected_demand(window, NOW) == pytest.approx(720.0 / FIVE_HOUR_S)
    assert expected_demand(window, NOW) == pytest.approx(0.04)
    assert window_slack(window, NOW) == pytest.approx(0.88 - 0.04)


def test_expected_demand_is_zero_once_the_window_has_reset() -> None:
    window = win("five_hour", 0.30, length_s=FIVE_HOUR_S, ttr_s=0.0)
    assert expected_demand(window, NOW) == pytest.approx(0.0)
    assert window_slack(window, NOW) == pytest.approx(0.70)


def test_expected_demand_accepts_an_explicit_burn_rate() -> None:
    """An explicitly measured burn rate overrides both the prior and the baseline."""
    window = win("seven_day", 0.50, expected_used=0.20, ttr_s=DAY_S)
    doubled = 2.0 / SEVEN_DAY_S
    assert expected_demand(window, NOW, doubled) == pytest.approx(2.0 * DAY_S / SEVEN_DAY_S)
    assert window_slack(window, NOW, doubled) == pytest.approx(0.5 - 2.0 * DAY_S / SEVEN_DAY_S)
    assert expected_demand(window, NOW, 0.0) == pytest.approx(0.0)


def test_expected_demand_is_clamped_into_the_unit_interval() -> None:
    window = win("seven_day", 0.10, ttr_s=DAY_S)
    assert expected_demand(window, NOW, 1.0) == 1.0  # 1/s for a day would be 86400


def test_expected_demand_rejects_an_impossible_burn_rate() -> None:
    window = win("seven_day", 0.10)
    with pytest.raises(ValueError):
        expected_demand(window, NOW, -1e-6)
    with pytest.raises(ValueError):
        expected_demand(window, NOW, math.nan)
    with pytest.raises(ValueError):
        expected_demand(window, NOW, math.inf)


def test_slack_sign_is_computed_not_taken_from_ahead_of_pace() -> None:
    """The upstream boolean is self-inconsistent; the sign must come from the numbers."""
    payload = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "cswap_real.json").read_text(
            encoding="utf-8"
        )
    )
    usage = payload["accounts"][0]["usage"]
    seven_day, scoped = usage["sevenDay"], usage["scoped"][0]

    # Same baseline, both over it, contradictory booleans upstream.
    assert seven_day["aheadOfPace"] is False and seven_day["pct"] > seven_day["expectedPct"]
    assert scoped["aheadOfPace"] is True and scoped["pct"] > scoped["expectedPct"]

    now = datetime.fromisoformat("2026-08-14T18:48:00+00:00").timestamp()
    for entry in (seven_day, scoped):
        window = Window(
            key="w",
            used_fraction=entry["pct"] / 100.0,
            length_s=SEVEN_DAY_S,
            resets_at_s=datetime.fromisoformat(entry["resetsAt"]).timestamp(),
            observed_at_s=now,
            expected_used_fraction=entry["expectedPct"] / 100.0,
        )
        assert window_slack(window, now) < 0.0


# --------------------------------------------------------------------------------------
# (4) applies_to gating
# --------------------------------------------------------------------------------------


def test_applies_to_gates_which_windows_bind() -> None:
    wide = win("seven_day", 0.10, expected_used=0.90)  # slack 0.80
    fable = win("scoped:fable", 0.85, expected_used=0.90, applies_to=FABLE)  # slack 0.05
    snap = acct(ACCOUNT_CLAUDE, wide, fable)

    assert applicable_windows(snap, MODEL_CLASS_FABLE) == (wide, fable)
    assert applicable_windows(snap, "opus") == (wide,)
    assert applicable_windows(snap, "Fable") == (wide, fable)  # normalized
    # No model class named -> no constraint may be dropped.
    assert applicable_windows(snap, None) == (wide, fable)

    assert min_slack(snap, NOW, MODEL_CLASS_FABLE) == pytest.approx(0.05)
    assert min_slack(snap, NOW, "opus") == pytest.approx(0.80)
    assert min_remaining(snap, MODEL_CLASS_FABLE) == pytest.approx(0.15)
    assert min_remaining(snap, "opus") == pytest.approx(0.90)

    fable_row = account_score(snap, NOW, None, MODEL_CLASS_FABLE, REGIME_A)
    opus_row = account_score(snap, NOW, None, "opus", REGIME_A)
    assert fable_row.binding_window == "scoped:fable"
    assert opus_row.binding_window == "seven_day"
    assert opus_row.score > fable_row.score

    # The skipped window is still reported, with a reason, so a human can see why.
    skipped = [row for row in opus_row.per_window if not row.applicable]
    assert [row.key for row in skipped] == ["scoped:fable"]
    assert skipped[0].reason.startswith("skipped")
    assert "opus" in skipped[0].reason  # the reason names the request, not the window
    assert skipped[0].binding is False
    assert all(row.applicable for row in fable_row.per_window)


def test_an_account_with_no_applicable_window_is_ineligible() -> None:
    snap = acct(ACCOUNT_CLAUDE, win("scoped:fable", 0.10, expected_used=0.90, applies_to=FABLE))
    row = account_score(snap, NOW, None, "opus", REGIME_A)
    assert row.eligible is False
    assert row.fits is False
    assert row.min_remaining is None
    assert "model class" in row.reason
    assert min_slack(snap, NOW, "opus") is None


def test_ineligible_rows_sort_below_every_eligible_row() -> None:
    gated = acct(ACCOUNT_CLAUDE_B, win("scoped:fable", 0.0, expected_used=1.0, applies_to=FABLE))
    poor = acct(ACCOUNT_CLAUDE, win("seven_day", 0.99, expected_used=0.10))  # slack -0.89
    rows = rank([gated, poor], NOW, None, "opus")
    assert [row.account_id for row in rows] == [ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B]
    assert rows[0].eligible is True and rows[1].eligible is False


# --------------------------------------------------------------------------------------
# Regimes
# --------------------------------------------------------------------------------------


def test_regime_a_scales_slack_by_capacity_and_provider_weight() -> None:
    big = acct(ACCOUNT_CLAUDE, win("seven_day", 0.50, expected_used=0.90), tier=TIER_MAX_20X)
    small = acct(ACCOUNT_CLAUDE_C, win("seven_day", 0.50, expected_used=0.90), tier=TIER_MAX_5X)

    big_row = account_score(big, NOW, None, None, REGIME_A)
    small_row = account_score(small, NOW, None, None, REGIME_A)

    assert big_row.min_slack == pytest.approx(0.40) == small_row.min_slack
    assert big_row.score == pytest.approx(0.40)
    assert small_row.score == pytest.approx(0.10)  # 0.25 capacity
    assert small_row.capacity == pytest.approx(0.25)
    assert big_row.regime == REGIME_A


def test_regime_b_scores_min_remaining() -> None:
    snap = acct(ACCOUNT_CLAUDE, win("seven_day", 0.55, expected_used=0.35))
    row = account_score(snap, NOW, None, None, REGIME_B)
    assert row.min_slack == pytest.approx(-0.20)
    assert row.min_remaining == pytest.approx(0.45)
    assert row.score == pytest.approx(0.45)
    assert row.regime == REGIME_B


def test_regime_is_decided_globally_before_anyone_is_scored() -> None:
    surplus = acct(ACCOUNT_CLAUDE, win("seven_day", 0.10, expected_used=0.90))  # +0.80
    deficit = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.90, expected_used=0.10))  # -0.80

    assert decide_regime([surplus, deficit], NOW) == REGIME_A
    assert decide_regime([deficit], NOW) == REGIME_B

    rows = rank([surplus, deficit], NOW, None, None)
    assert {row.regime for row in rows} == {REGIME_A}  # one regime for the whole field
    assert rows[0].account_id == ACCOUNT_CLAUDE


# --------------------------------------------------------------------------------------
# (2) REGIME B ORDERING -- the reason two regimes exist at all
# --------------------------------------------------------------------------------------


def test_regime_b_never_rewards_a_small_account_for_a_shallow_deficit() -> None:
    """``claude_c`` (-0.50 slack, 0.25 capacity) must NOT beat ``claude`` (-0.20, 1.0).

    Scaling the negative slack is the bug: ``-0.50 * 0.25 = -0.125`` sorts *above*
    ``-0.20 * 1.0 = -0.20``, so the smaller, more-depleted pool would win exactly when
    the call needs a pool that can actually serve it. Regime B ranks on
    ``capacity * min_remaining`` instead.
    """
    claude = acct(ACCOUNT_CLAUDE, win("seven_day", 0.55, expected_used=0.35), tier=TIER_MAX_20X)
    claude_c = acct(ACCOUNT_CLAUDE_C, win("seven_day", 0.60, expected_used=0.10), tier=TIER_MAX_5X)

    assert min_slack(claude, NOW) == pytest.approx(-0.20)
    assert min_slack(claude_c, NOW) == pytest.approx(-0.50)

    rows = rank([claude_c, claude], NOW, None, None)
    assert {row.regime for row in rows} == {REGIME_B}
    assert rows[0].account_id == ACCOUNT_CLAUDE, "regime B must prefer the pool that can serve"
    assert rows[0].score == pytest.approx(0.45)  # 1.00 capacity * 0.45 remaining
    assert rows[1].score == pytest.approx(0.10)  # 0.25 capacity * 0.40 remaining

    # The exact inversion the two-regime split exists to prevent.
    scaled_slack = {row.account_id: row.capacity * row.min_slack for row in rows}
    assert scaled_slack[ACCOUNT_CLAUDE_C] == pytest.approx(-0.125)
    assert scaled_slack[ACCOUNT_CLAUDE] == pytest.approx(-0.20)
    assert scaled_slack[ACCOUNT_CLAUDE_C] > scaled_slack[ACCOUNT_CLAUDE]


def test_regime_b_ordering_holds_for_the_whole_field() -> None:
    """Three underwater accounts sort strictly by ``capacity * min_remaining``."""
    a = acct(ACCOUNT_CLAUDE, win("seven_day", 0.55, expected_used=0.35))  # 0.45
    b = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.70, expected_used=0.35))  # 0.30
    # 0.90 remaining x 0.25 capacity = 0.225
    c = acct(
        ACCOUNT_CLAUDE_C, win("seven_day", 0.10, expected_used=0.05), tier=TIER_MAX_5X
    )

    rows = rank([b, c, a], NOW, None, None)
    assert [row.account_id for row in rows] == [ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B, ACCOUNT_CLAUDE_C]
    assert [row.score for row in rows] == pytest.approx([0.45, 0.30, 0.225])
    # c has by far the shallowest deficit and would win any slack-based ordering.
    assert min_slack(c, NOW) > min_slack(a, NOW) > min_slack(b, NOW)


# --------------------------------------------------------------------------------------
# (5) Monotonicity and determinism
# --------------------------------------------------------------------------------------


def test_score_decreases_as_used_fraction_rises_in_regime_a() -> None:
    scores = [
        account_score(
            acct(ACCOUNT_CLAUDE, win("seven_day", used / 100.0, expected_used=0.90)),
            NOW,
            None,
            None,
            REGIME_A,
        ).score
        for used in range(0, 85, 5)
    ]
    assert scores == sorted(scores, reverse=True)
    assert all(later < earlier for earlier, later in zip(scores, scores[1:], strict=False))


def test_score_decreases_as_used_fraction_rises_in_regime_b() -> None:
    scores = [
        account_score(
            acct(ACCOUNT_CLAUDE, win("seven_day", used / 100.0, expected_used=0.10)),
            NOW,
            None,
            None,
            REGIME_B,
        ).score
        for used in range(0, 100, 5)
    ]
    assert all(later < earlier for earlier, later in zip(scores, scores[1:], strict=False))


def test_ranked_score_decreases_as_the_account_burns_within_one_regime() -> None:
    """Same property through the public entry point, with the regime pinned by a peer."""
    peer = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.0, expected_used=0.95))  # keeps regime A
    scores = []
    for used in range(0, 90, 5):
        subject = acct(ACCOUNT_CLAUDE, win("seven_day", used / 100.0, expected_used=0.90))
        rows = {row.account_id: row for row in rank([subject, peer], NOW, None, None)}
        assert rows[ACCOUNT_CLAUDE].regime == REGIME_A
        scores.append(rows[ACCOUNT_CLAUDE].score)
    assert all(later < earlier for earlier, later in zip(scores, scores[1:], strict=False))


def test_adding_a_window_never_raises_a_score() -> None:
    base_window = win("seven_day", 0.40, expected_used=0.90)  # slack 0.50, remaining 0.60
    tighter = win("five_hour", 0.70, expected_used=0.90, length_s=FIVE_HOUR_S)  # 0.20 / 0.30
    looser = win("five_hour", 0.05, expected_used=0.90, length_s=FIVE_HOUR_S)  # 0.85 / 0.95
    gated = win("scoped:fable", 0.99, expected_used=0.10, applies_to=FABLE)  # brutal, but gated

    for regime in (REGIME_A, REGIME_B):
        base = account_score(acct(ACCOUNT_CLAUDE, base_window), NOW, None, "opus", regime)
        with_tighter = account_score(
            acct(ACCOUNT_CLAUDE, base_window, tighter), NOW, None, "opus", regime
        )
        with_looser = account_score(
            acct(ACCOUNT_CLAUDE, base_window, looser), NOW, None, "opus", regime
        )
        with_gated = account_score(
            acct(ACCOUNT_CLAUDE, base_window, gated), NOW, None, "opus", regime
        )

        assert with_tighter.score < base.score
        assert with_looser.score <= base.score
        assert with_looser.score == pytest.approx(base.score)
        # A window that does not apply to the request must not change anything.
        assert with_gated.score == pytest.approx(base.score)
        assert with_gated.binding_window == base.binding_window


def test_identical_inputs_produce_identical_rankings() -> None:
    snaps = [
        acct(ACCOUNT_CLAUDE, win("seven_day", 0.30, expected_used=0.98)),
        acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.35, expected_used=0.14)),
        acct(ACCOUNT_CLAUDE_C, win("seven_day", 0.35, expected_used=0.14), tier=TIER_MAX_5X),
    ]
    first = [row.to_dict() for row in rank(snaps, NOW, None, None)]
    second = [row.to_dict() for row in rank(list(reversed(snaps)), NOW, None, None)]
    assert first == second


def test_ties_break_stably_on_account_id() -> None:
    same = [
        acct(ACCOUNT_CLAUDE_C, win("seven_day", 0.50, expected_used=0.90)),
        acct(ACCOUNT_CLAUDE, win("seven_day", 0.50, expected_used=0.90)),
        acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.50, expected_used=0.90)),
    ]
    ordered = [row.account_id for row in rank(same, NOW, None, None)]
    assert ordered == [ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B, ACCOUNT_CLAUDE_C]


# --------------------------------------------------------------------------------------
# (6) The operator's real case
# --------------------------------------------------------------------------------------


def test_operator_real_case_three_hours_of_weekly_quota_beats_six_days() -> None:
    """claude: 30% used with 3h to the weekly reset. claude_b: 35% used with 6 days left.

    claude_b looks "healthier" on raw usage and loses anyway: its remaining 65% has six
    days of runway, while claude's remaining 70% evaporates in three hours. Slack, not
    usage, is the objective.
    """
    claude = acct(ACCOUNT_CLAUDE, win("seven_day", 0.30, ttr_s=3 * 60 * 60.0))
    claude_b = acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.35, ttr_s=6 * DAY_S))

    assert min_slack(claude, NOW) == pytest.approx(0.70 - 10_800.0 / SEVEN_DAY_S)
    assert min_slack(claude_b, NOW) == pytest.approx(0.65 - 518_400.0 / SEVEN_DAY_S)

    rows = rank([claude_b, claude], NOW, None, None)
    assert rows[0].account_id == ACCOUNT_CLAUDE
    assert rows[0].regime == REGIME_A
    assert rows[0].min_slack > 0.0 > rows[1].min_slack
    assert rows[0].binding_window == "seven_day"


# --------------------------------------------------------------------------------------
# Provider weights / config plumbing
# --------------------------------------------------------------------------------------


def test_provider_weight_defaults_to_one_and_reads_any_config_shape() -> None:
    assert provider_weight(None, "claude") == pytest.approx(1.0)
    assert provider_weight({}, "claude") == pytest.approx(1.0)
    assert provider_weight({"provider_weights": {"codex": 2.0}}, "claude") == pytest.approx(1.0)
    assert provider_weight({"provider_weights": {"claude": 0.5}}, "claude") == pytest.approx(0.5)
    assert provider_weight(
        SimpleNamespace(provider_weights={"claude": 3.0}), "claude"
    ) == pytest.approx(3.0)
    assert provider_weight(SimpleNamespace(provider_weights=None), "claude") == pytest.approx(1.0)


def test_provider_weight_scales_the_score() -> None:
    snap = acct(ACCOUNT_CLAUDE, win("seven_day", 0.50, expected_used=0.90))
    cfg = {"provider_weights": {"claude": 0.5}}
    assert account_score(snap, NOW, cfg, None, REGIME_A).score == pytest.approx(0.20)
    assert account_score(snap, NOW, cfg, None, REGIME_A).provider_weight == pytest.approx(0.5)


def test_a_malformed_provider_weight_fails_loud() -> None:
    snap = acct(ACCOUNT_CLAUDE, win("seven_day", 0.50, expected_used=0.90))
    with pytest.raises(ValueError):
        account_score(snap, NOW, {"provider_weights": {"claude": -1.0}}, None, REGIME_A)
    with pytest.raises(ValueError):
        provider_weight({"provider_weights": {"claude": math.nan}}, "claude")
    with pytest.raises(TypeError):
        provider_weight({"provider_weights": [("claude", 1.0)]}, "claude")


def test_rank_accepts_any_iterable_and_returns_a_list() -> None:
    snaps = (
        acct(ACCOUNT_CLAUDE, win("seven_day", 0.50, expected_used=0.90)),
        acct(ACCOUNT_CLAUDE_B, win("seven_day", 0.60, expected_used=0.90)),
    )
    rows = rank(iter(snaps), NOW, None, None)
    assert isinstance(rows, list)
    assert [row.account_id for row in rows] == [ACCOUNT_CLAUDE, ACCOUNT_CLAUDE_B]
    assert rank([], NOW, None, None) == []


def test_per_window_rows_explain_the_binding_choice() -> None:
    snap = acct(
        ACCOUNT_CLAUDE,
        win("five_hour", 0.20, length_s=FIVE_HOUR_S, ttr_s=1800.0),
        win("seven_day", 0.80, expected_used=0.50),
    )
    row = account_score(snap, NOW, None, None, REGIME_A)
    assert [entry.key for entry in row.per_window] == ["five_hour", "seven_day"]
    assert row.binding is not None
    assert row.binding.key == "seven_day"
    assert row.binding.slack == pytest.approx(-0.30)
    assert row.per_window[0].expected_used_fraction == pytest.approx(1.0 - 1800.0 / FIVE_HOUR_S)
    assert all(entry.applicable for entry in row.per_window)


def test_scoring_module_is_pure() -> None:
    """scoring.py imports the stdlib and ``types`` -- nothing else, and no clock."""
    import quota_router.scoring as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add(("." * node.level) + (node.module or "").split(".")[0])

    assert roots <= {"__future__", "math", "collections", "typing", "dataclasses", ".types"}, roots
    forbidden_roots = ("os", "sys", "subprocess", "pathlib", "time", "datetime", "socket")
    for forbidden in forbidden_roots:
        assert forbidden not in roots
    assert "time.time" not in source
    assert "datetime.now" not in source
