"""The joint projection: fleet waste, and how much of it one job avoids.

``V`` is the PSE left in each stock at its deadline when forecast demand is spread over
the fleet earliest-deadline-first, under each subscription's five-hour cap. A job taken
from stock *i* now avoids ``V - V(job on i)`` of it. Properties that must hold:

* A job on a pool that the projection drains anyway avoids nothing, even when another
  pool wastes: the demand it displaces has nowhere useful to go.
* A five-hour cap that binds makes an expiring pool's waste unavoidable.
* Fable work is limited by the Fable sub-cap, and Fable leftover is never counted on top
  of weekly leftover (``pse.py``: Fable is a constraint, not a pile).
* A plan end retires a stock; a weekly reset refills it.
* Credit pools serve only ``run`` work. With no run demand a credit pool keeps its whole
  balance to expiry, so a run job on it avoids exactly its size.
* Unmet demand is reported and never changes ``V``.
* Conservation, and ``0 <= avoided <= job`` for any field.
"""

from __future__ import annotations

import random
import time
from dataclasses import replace

import pytest

from quota_router import joint
from quota_router.types import AccountSnapshot, Window

H = 3600.0


def sub(id_, rem, deadline_h, *, group="claude", cap=None, active=False, session_rem=None,
        refills=False, weekly_cap=None, fable_rem=None, fable_cap=None, available_h=0.0):
    """A subscription stock. ``cap`` is PSE per five-hour window (None: uncapped)."""
    return joint.Stock(
        id=id_,
        group=group,
        kind=joint.KIND_SUBSCRIPTION,
        weekly_rem=rem,
        weekly_cap=weekly_cap if weekly_cap is not None else rem,
        deadline_s=deadline_h * H,
        refills=refills,
        period_s=7 * 24 * H,
        session_cap=cap,
        session_rem=(session_rem if session_rem is not None else cap) if active else None,
        session_reset_s=(5 * H) if active else None,
        fable_rem=fable_rem,
        fable_cap=fable_cap,
        available_from_s=available_h * H,
    )


def credit(id_, rem, deadline_h, *, group="claude"):
    return joint.Stock(
        id=id_,
        group=group,
        kind=joint.KIND_CREDIT,
        weekly_rem=rem,
        weekly_cap=rem,
        deadline_s=deadline_h * H,
        refills=False,
        period_s=0.0,
        session_cap=None,
        session_rem=None,
        session_reset_s=None,
        fable_rem=None,
        fable_cap=None,
        available_from_s=0.0,
    )


def other(rate, *, group="claude", fable=0.0):
    return joint.Stream(group=group, name=joint.STREAM_OTHER, pse_per_h=rate, fable_share=fable)


def runs(rate, *, group="claude", fable=0.0):
    return joint.Stream(group=group, name=joint.STREAM_RUN, pse_per_h=rate, fable_share=fable)


def proj(stocks, streams, horizon_h=40.0):
    return joint.project(stocks, streams, now_s=0.0, horizon_s=horizon_h * H, step_s=H)


def avoided(stocks, streams, stock_id, x=0.1, *, fable=False, stream=joint.STREAM_OTHER,
            horizon_h=40.0):
    return joint.marginal(
        stocks,
        streams,
        stock_id=stock_id,
        job_pse=x,
        fable=fable,
        stream=stream,
        now_s=0.0,
        horizon_s=horizon_h * H,
        step_s=H,
    )


def leftover(projection, stock_id):
    return sum(row.leftover for row in projection.settled if row.id == stock_id)


def test_a_field_that_wastes_nothing_has_nothing_to_avoid() -> None:
    stocks = [sub("a", 1.0, 10, cap=1.0), sub("b", 2.0, 48, cap=1.0)]
    result = proj(stocks, [other(0.2)], horizon_h=60)
    assert result.v_pse == pytest.approx(0.0)
    for stock in stocks:
        assert avoided(stocks, [other(0.2)], stock.id, horizon_h=60).avoided == pytest.approx(0.0)


def test_a_job_on_a_pool_drained_anyway_avoids_nothing() -> None:
    # c expires at 5h holding more than 5h of demand; a is drained before its deadline.
    stocks = [sub("c", 2.0, 5), sub("a", 1.0, 20)]
    streams = [other(0.1)]
    base = proj(stocks, streams)
    assert leftover(base, "c") == pytest.approx(1.5)
    assert leftover(base, "a") == pytest.approx(0.0)
    assert avoided(stocks, streams, "c").avoided == pytest.approx(0.1)
    assert avoided(stocks, streams, "a").avoided == pytest.approx(0.0)


def test_displaced_demand_spills_into_a_later_pool_with_leftover() -> None:
    # a is drained by its deadline; b wastes. Taking from a pushes demand onto b, so both
    # avoid the full job: a tie, broken by deadline at the decision layer.
    stocks = [sub("a", 1.0, 10), sub("b", 5.0, 20)]
    streams = [other(0.1)]
    assert leftover(proj(stocks, streams), "b") == pytest.approx(4.0)
    assert avoided(stocks, streams, "a").avoided == pytest.approx(0.1)
    assert avoided(stocks, streams, "b").avoided == pytest.approx(0.1)


def test_a_binding_five_hour_cap_makes_waste_unavoidable() -> None:
    stocks = [sub("a", 6.0, 10, cap=1.0, active=True), sub("b", 50.0, 100)]
    streams = [other(1.0)]
    base = proj(stocks, streams, horizon_h=20)
    # Two five-hour windows of 1 PSE each fit before the 10h deadline.
    assert leftover(base, "a") == pytest.approx(4.0)
    assert avoided(stocks, streams, "a", horizon_h=20).avoided == pytest.approx(0.0)


def test_fable_work_is_held_to_the_sub_cap_and_not_counted_twice() -> None:
    stocks = [sub("a", 3.0, 10, fable_rem=0.5, fable_cap=1.5)]
    result = proj(stocks, [other(1.0, fable=1.0)], horizon_h=20)
    assert result.served["a"] == pytest.approx(0.5)
    assert leftover(result, "a") == pytest.approx(2.5)
    assert result.unmet[("claude", joint.STREAM_OTHER)] == pytest.approx(20.0 - 0.5)


def test_a_plan_end_retires_the_stock() -> None:
    stocks = [sub("a", 1.0, 5, refills=False)]
    result = proj(stocks, [other(0.1)], horizon_h=20)
    assert leftover(result, "a") == pytest.approx(0.5)
    assert result.unmet[("claude", joint.STREAM_OTHER)] == pytest.approx(1.5)


def test_a_weekly_reset_refills_the_stock() -> None:
    stocks = [sub("a", 1.0, 5, refills=True, weekly_cap=6.25)]
    result = proj(stocks, [other(0.1)], horizon_h=20)
    assert leftover(result, "a") == pytest.approx(0.5)
    assert result.refilled["a"] == pytest.approx(6.25)
    assert result.unmet.get(("claude", joint.STREAM_OTHER), 0.0) == pytest.approx(0.0)


def test_other_work_never_drains_a_credit_pool() -> None:
    stocks = [sub("s", 1.0, 10), credit("api", 0.66, 120)]
    streams = [other(0.2), runs(0.0)]
    base = proj(stocks, streams, horizon_h=130)
    assert leftover(base, "api") == pytest.approx(0.66)
    assert base.served.get("api", 0.0) == 0.0
    # The subscription is drained either way, so only the credit pool avoids waste.
    assert avoided(stocks, streams, "api", 0.01, stream=joint.STREAM_RUN,
                   horizon_h=130).avoided == pytest.approx(0.01)
    assert avoided(stocks, streams, "s", 0.01, stream=joint.STREAM_RUN,
                   horizon_h=130).avoided == pytest.approx(0.0)


def test_run_work_drains_a_credit_pool() -> None:
    stocks = [credit("api", 0.66, 120)]
    base = proj(stocks, [runs(0.01)], horizon_h=130)
    assert leftover(base, "api") == pytest.approx(0.0)


def test_groups_never_trade_demand() -> None:
    stocks = [sub("cdx", 5.0, 10, group="codex"), sub("cl", 1.0, 10)]
    result = proj(stocks, [other(0.2), other(0.0, group="codex")], horizon_h=20)
    assert result.served.get("cdx", 0.0) == 0.0
    assert leftover(result, "cdx") == pytest.approx(5.0)


def test_a_non_refilling_deadline_past_the_horizon_extends_it() -> None:
    stocks = [credit("api", 1.0, 30)]
    result = proj(stocks, [runs(0.01)], horizon_h=20)
    assert [row.deadline_s for row in result.settled] == [30 * H]
    assert leftover(result, "api") == pytest.approx(0.7)


def test_unmet_demand_is_reported_and_never_scored() -> None:
    # Refilling, so the 20h horizon is not stretched to reach a non-refilling deadline.
    stocks = [sub("a", 1.0, 30, refills=True)]
    result = proj(stocks, [other(0.5)], horizon_h=20)
    assert result.unmet[("claude", joint.STREAM_OTHER)] == pytest.approx(9.0)
    assert result.v_pse == pytest.approx(0.0)


def test_a_pool_in_cooldown_takes_nothing_until_it_is_available() -> None:
    stocks = [
        sub("a", 1.0, 30, available_h=10, refills=True),
        sub("b", 5.0, 40, refills=True),
    ]
    result = proj(stocks, [other(0.1)], horizon_h=20)
    assert result.served["b"] == pytest.approx(1.0)
    assert result.served["a"] == pytest.approx(1.0)


def test_a_stock_expiring_mid_step_takes_only_the_demand_before_its_deadline() -> None:
    stocks = [sub("a", 5.0, 0.5), sub("b", 5.0, 40)]
    result = proj(stocks, [other(1.0)], horizon_h=2)
    assert result.served["a"] == pytest.approx(0.5)


def _random_field(rng: random.Random):
    stocks = []
    for i in range(rng.randint(1, 6)):
        if rng.random() < 0.25:
            stocks.append(credit(f"c{i}", rng.uniform(0, 2), rng.uniform(1, 200)))
        else:
            cap = rng.choice([None, 0.25, 1.0])
            stocks.append(
                sub(
                    f"s{i}",
                    rng.uniform(0, 6.25),
                    rng.uniform(0.5, 168),
                    cap=cap,
                    active=cap is not None and rng.random() < 0.5,
                    refills=rng.random() < 0.5,
                    weekly_cap=6.25,
                    fable_rem=rng.choice([None, rng.uniform(0, 3)]),
                    fable_cap=3.125,
                    available_h=rng.choice([0.0, 0.0, rng.uniform(0, 20)]),
                )
            )
    streams = [other(rng.uniform(0, 0.4), fable=rng.random()), runs(rng.uniform(0, 0.05))]
    return stocks, streams


def test_conservation_holds_on_random_fields() -> None:
    rng = random.Random(20261014)
    for _ in range(200):
        stocks, streams = _random_field(rng)
        result = proj(stocks, streams, horizon_h=rng.uniform(5, 200))
        start = sum(s.weekly_rem for s in stocks) + sum(result.refilled.values())
        end = (
            sum(result.served.values())
            + sum(row.leftover for row in result.settled)
            + sum(result.terminal.values())
        )
        assert start == pytest.approx(end, abs=1e-9)


def _random_marginal(rng, *, uncapped):
    stocks, streams = _random_field(rng)
    if uncapped:
        stocks = [
            replace(s, session_cap=None, session_rem=None, session_reset_s=None) for s in stocks
        ]
    target = rng.choice(stocks)
    x = rng.choice([0.003, 0.05, 0.5])
    stream = joint.STREAM_RUN if target.kind == joint.KIND_CREDIT else rng.choice(
        [joint.STREAM_OTHER, joint.STREAM_RUN]
    )
    return x, avoided(stocks, streams, target.id, x, fable=rng.random() < 0.3,
                      stream=stream, horizon_h=rng.uniform(5, 200))


def test_without_five_hour_caps_a_job_avoids_between_zero_and_itself() -> None:
    """The raw difference, not the clamped score: this is the property, unassisted."""
    rng = random.Random(11)
    for _ in range(300):
        x, result = _random_marginal(rng, uncapped=True)
        assert result.job_pse <= x + 1e-12
        assert -1e-9 <= result.raw <= result.job_pse + 1e-9
        assert not result.clamped


def test_with_caps_the_clamp_is_rare_and_always_flagged() -> None:
    """A five-hour window opens at first use, so moving work can shift when later
    windows open and change a capped pool's total intake by more than the job. The
    score is clamped to [0, job] and the clamp is flagged. Measured on 3,000 random
    fields: 8 clamps (0.27%); none at all once caps are removed.
    """
    rng = random.Random(7)
    clamps = 0
    trials = 400
    for _ in range(trials):
        _x, result = _random_marginal(rng, uncapped=False)
        assert 0.0 <= result.avoided <= result.job_pse + 1e-12
        out_of_range = result.raw < -1e-9 or result.raw > result.job_pse + 1e-9
        assert result.clamped == out_of_range
        clamps += out_of_range
    assert clamps / trials < 0.02


def test_the_projection_is_fast_enough_for_a_pick() -> None:
    stocks = [
        sub(f"s{i}", 3.0, 24 * (i + 1) * 0.6, cap=1.0, refills=True, weekly_cap=6.25,
            fable_rem=1.0, fable_cap=3.125)
        for i in range(9)
    ] + [credit("c0", 0.6, 700), credit("c1", 0.3, 800)]
    streams = [other(0.11, fable=0.36), runs(0.01)]
    start = time.perf_counter()
    for stock in stocks:
        joint.marginal(
            stocks, streams, stock_id=stock.id, job_pse=0.003, fable=False,
            stream=joint.STREAM_RUN, now_s=0.0, horizon_s=35 * 24 * H, step_s=H,
        )
    assert time.perf_counter() - start < 1.0


# ======================================================================================
# Building a stock from a snapshot
# ======================================================================================


def _snap(windows, *, provider="claude", tier="max_20x"):
    return AccountSnapshot(
        id="claude", provider=provider, windows=tuple(windows), tier=tier, source="live",
        confidence=1.0, available=True,
    )


def _w(key, used, resets_in_h, length_h, applies_to=None):
    return Window(
        key=key, used_fraction=used, length_s=length_h * H, resets_at_s=resets_in_h * H,
        observed_at_s=0.0, applies_to=frozenset(applies_to) if applies_to else None,
    )


def test_a_snapshot_becomes_a_subscription_stock() -> None:
    snap = _snap([
        _w("5h", 0.4, 2, 5),
        _w("7d", 0.5, 100, 168),
        _w("fable", 0.2, 100, 168, applies_to=["fable"]),
    ])
    stock = joint.subscription_stock(snap, now_s=0.0, capacity=1.0, plan=None, refills=True)
    assert stock.group == "claude"
    assert stock.kind == joint.KIND_SUBSCRIPTION
    assert stock.weekly_rem == pytest.approx(0.5 * 6.25)
    assert stock.weekly_cap == pytest.approx(6.25)
    assert stock.deadline_s == pytest.approx(100 * H)
    assert stock.session_cap == pytest.approx(1.0)
    assert stock.session_rem == pytest.approx(0.6)
    assert stock.session_reset_s == pytest.approx(2 * H)
    assert stock.fable_rem == pytest.approx(0.8 * 3.125)
    assert stock.refills


def test_a_snapshot_without_a_weekly_window_is_not_a_stock() -> None:
    assert joint.subscription_stock(
        _snap([_w("5h", 0.4, 2, 5)]), now_s=0.0, capacity=1.0, plan=None, refills=True
    ) is None


def test_an_expired_five_hour_window_is_not_an_active_window() -> None:
    snap = _snap([_w("5h", 0.9, -1, 5), _w("7d", 0.5, 100, 168)])
    stock = joint.subscription_stock(snap, now_s=0.0, capacity=1.0, plan=None, refills=True)
    assert stock.session_rem is None and stock.session_reset_s is None
