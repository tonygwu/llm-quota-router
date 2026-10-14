"""The replay that gates making the joint objective the default.

What must hold before any number it prints is believed:

* ``actual`` reproduces the waste series for the same window (the fidelity gate).
* The metric tells policies apart: a trace where today's objective lets a small,
  soon-expiring pool lapse while earliest-deadline routing drains it.
* No lookahead: a policy's choices before time t do not change when the trace after t
  does.
* Unmet demand is counted separately and never folded into waste.
* History is only read.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from quota_router import cli, replay, waste
from tests.test_waste import T0, account, hrecord, window

HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY
STEP = 900.0
COUNT_FROM = T0 + WEEK


def _series(account_id, *, start, end, used_at, resets_at, tier="max_20x", capacity=1.0):
    """Readings every 15 minutes. ``used_at(t)`` gives the weekly bar, ``resets_at(t)`` the
    boundary it publishes."""
    out = []
    t = start
    while t <= end:
        out.append(
            hrecord(
                t,
                account(account_id, window("7d", round(used_at(t), 4), resets_at(t), t),
                        tier=tier, capacity=capacity),
            )
        )
        t += STEP
    return out


def _lapse_scenario(*, spike_after=None):
    """A: 0.625 PSE left, resets 10h into the count. B: 5 PSE left, resets at 100h.

    Demand is 0.1 PSE/h throughout: in the counted window B's bar rises 1.6 points an
    hour; in the look-back week three accounts whose plans ended before counting carried
    it. Before B's reset the fleet can absorb everything it holds, so the only waste a
    policy can cause is letting A lapse.
    """
    a_reset = COUNT_FROM + 10 * HOUR
    b_reset = COUNT_FROM + 100 * HOUR
    end = COUNT_FROM + 20 * HOUR
    a = _series(
        "claude_a", start=T0, end=end,
        used_at=lambda t: 0.9 if t < a_reset else 0.0,
        resets_at=lambda t: a_reset if t < a_reset else a_reset + WEEK,
    )

    def b_used(t):
        if t <= COUNT_FROM:
            return 0.2
        used = 0.2 + 0.016 * (t - COUNT_FROM) / HOUR
        if spike_after is not None and t > spike_after:
            used += 0.3
        return min(1.0, used)

    b = _series("claude_b", start=T0, end=end, used_at=b_used, resets_at=lambda t: b_reset)
    lookback = []
    for name in ("claude_c", "claude_d", "claude_e"):
        lookback += _series(
            name, start=T0, end=COUNT_FROM - HOUR,
            used_at=lambda t: min(1.0, 0.0055 * (t - T0) / HOUR),
            resets_at=lambda t: T0 + 30 * DAY,
        )
    return sorted(a + b + lookback, key=lambda r: r["t"]), end


def _replay(records, end, policies, **kw):
    return replay.run_replay(
        records,
        waste_rows=waste.detect_resets(records).records,
        plans={},
        plan_ends={name: COUNT_FROM - 1.0 for name in ("claude_c", "claude_d", "claude_e")},
        count_from_s=COUNT_FROM,
        end_s=end,
        policies=policies,
        **kw,
    )


def _by_policy(result):
    return {p.policy: p for p in result.policies}


def test_actual_reproduces_the_waste_series() -> None:
    reset = COUNT_FROM + 3 * DAY
    records = _series(
        "claude", start=T0, end=COUNT_FROM + 5 * DAY,
        used_at=lambda t: min(1.0, 0.002 * (t - T0) / HOUR) if t < reset else 0.002 * (t - reset) / HOUR,
        resets_at=lambda t: reset if t < reset else reset + WEEK,
    )
    result = replay.run_replay(
        records,
        waste_rows=waste.detect_resets(records).records,
        plans={},
        plan_ends={},
        count_from_s=COUNT_FROM,
        end_s=COUNT_FROM + 5 * DAY,
        policies=["actual"],
    )
    actual = _by_policy(result)["actual"]
    assert result.recorded_waste_pse == pytest.approx((1 - 0.002 * 10 * 24) * 6.25, abs=0.05)
    assert actual.observed_waste_pse == pytest.approx(result.recorded_waste_pse, abs=0.05)
    assert result.fidelity_ok is True


def test_the_metric_catches_a_small_pool_left_to_lapse() -> None:
    records, end = _lapse_scenario()
    results = _by_policy(_replay(records, end, ["own_waste", "edf", "joint"]))
    # Today's objective prefers B's bigger pile (its own waste at the hardcoded 0.04
    # PSE/h) and lets most of A lapse.
    assert results["own_waste"].waste_pse > 0.4
    # Earliest deadline drains A down to the 2% eligibility floor production also
    # applies; that floor (2% of 6.25 PSE) is all it leaves.
    assert results["edf"].waste_pse == pytest.approx(0.125, abs=0.02)
    assert results["joint"].waste_pse == pytest.approx(0.125, abs=0.02)
    # B drains before its reset under every policy, so the tail does not hide the lapse.
    assert results["own_waste"].tail_pse == pytest.approx(results["joint"].tail_pse, abs=0.02)
    assert results["own_waste"].metric_pse > results["joint"].metric_pse + 0.25


def test_a_policy_sees_no_demand_from_after_its_decision() -> None:
    records, end = _lapse_scenario()
    spiked, _ = _lapse_scenario(spike_after=COUNT_FROM + 15 * HOUR)
    for policy in ("joint", "own_waste_measured_rate"):
        plain = _by_policy(_replay(records, end, [policy]))[policy].choices
        moved = _by_policy(_replay(spiked, end, [policy]))[policy].choices
        before = COUNT_FROM + 15 * HOUR - STEP
        assert [c for c in plain if c[0] < before] == [c for c in moved if c[0] < before]


def test_unmet_demand_is_counted_apart_from_waste() -> None:
    # One pool with 0.1 PSE left, far from its reset, against 2 PSE of demand.
    spec = replay.PoolSpec(
        id="claude", provider="claude", tier="max_20x", tier_scale=1.0,
        plan=replay.Plan(weekly_to_session=1.0), events=(), plan_end_s=None,
        start_used=0.9, start_fable_used=None, has_fable=False,
    )
    look = int(WEEK / STEP)
    demand = (0.0,) * look + (0.5,) * 4
    trace = replay.Trace(
        start_s=COUNT_FROM - WEEK, bin_s=STEP, group_pse=demand,
        group_fable_pse=(0.0,) * len(demand), by_account={"claude": demand},
        by_account_fable={"claude": (0.0,) * len(demand)},
    )
    result = replay.simulate(
        [spec], trace, policy="edf", count_from_s=COUNT_FROM,
        end_s=COUNT_FROM + 4 * STEP, job_pse=0.01,
    )
    # Served down to the 2% floor (0.02 PSE), the rest unmet, nothing called waste.
    assert result.served_pse == pytest.approx(0.08, abs=0.011)
    assert result.unmet_pse == pytest.approx(2.0 - result.served_pse, abs=1e-9)
    assert result.waste_pse == 0.0


def test_an_unknown_policy_is_refused() -> None:
    records, end = _lapse_scenario()
    with pytest.raises(ValueError, match="magic"):
        _replay(records, end, ["magic"])


def test_the_replay_command_reads_history_without_touching_it(tmp_path) -> None:
    from tests.test_cli import _NO_CONFIG_FILES, run

    records, end = _lapse_scenario()
    history = tmp_path / "hist" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text("".join(json.dumps(r) + "\n" for r in records))
    before = hashlib.sha256(history.read_bytes()).hexdigest()
    env = {"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "state")}
    code, out, err = run(
        ["replay", "--json", "--history", str(history), "--days", str(20 / 24),
         "--policies", "own_waste,edf"],
        env,
        now_s=end,
        cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    payload = json.loads(out)
    assert [p["policy"] for p in payload["policies"]] == ["own_waste", "edf"]
    assert hashlib.sha256(history.read_bytes()).hexdigest() == before


def test_dropping_a_pool_keeps_its_demand() -> None:
    records, end = _lapse_scenario()
    full = _by_policy(_replay(records, end, ["edf"]))["edf"]
    without_b = _replay(records, end, ["edf"], without=["claude_b"])
    edf = _by_policy(without_b)["edf"]
    # B carried the counted demand. Without its pool, A serves 0.5 PSE above its floor
    # before its 10h reset and refills after it, so the first 10h leave 0.5 unmet.
    assert edf.served_pse + edf.unmet_pse == pytest.approx(full.served_pse, abs=1e-6)
    assert edf.unmet_pse == pytest.approx(0.5, abs=0.02)
    assert without_b.fidelity_ok is None
    assert "claude_b" not in without_b.accounts
    with pytest.raises(ValueError, match="nope"):
        _replay(records, end, ["edf"], without=["nope"])
