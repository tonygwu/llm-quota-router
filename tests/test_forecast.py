"""The demand forecast: how much work, in PSE per hour, each provider group runs.

The joint projection spreads this forecast over the fleet's stocks, so every number
here becomes routing. What it must get right:

* Consumption is the rise of each weekly window between two consecutive readings,
  converted to PSE with the account's own ``k`` and tier. A reading republished from the
  cache (same observation time) adds nothing; a reset counts only what was used after it;
  a one-point quantization drop counts zero, never negative.
* Groups never mix: Codex work is not Claude demand.
* Work that went through ``quotapick run`` on a subscription is the ``run`` stream, not
  ``other``; work on a credit pool never showed up in a subscription window at all.
* A manual reserve already removes its quota from the stock, so it is subtracted from
  demand rather than counted twice.
* The cache is written atomically and read back exactly; a missing, corrupt or
  wrong-version file is reported as a problem, never treated as a zero forecast.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from quota_router import cli, forecast
from quota_router.pse import Plan
from tests.test_waste import FIVE_HOURS, SEVEN_DAYS, T0, account, hrecord, window

HOUR = 3600.0
WEEK_END = T0 + SEVEN_DAYS + 3 * HOUR  # a weekly boundary that no test crosses by accident


def _reading(t, used, *, account_id="claude", provider="claude", tier="max_20x",
             capacity=1.0, resets_at_s=WEEK_END, fable_used=None, observed_at_s=None):
    obs = t if observed_at_s is None else observed_at_s
    windows = [window("7d", used, resets_at_s, obs)]
    if fable_used is not None:
        windows.append(window("fable", fable_used, resets_at_s, obs, applies_to=["fable"]))
    return hrecord(
        t, account(account_id, *windows, tier=tier, capacity=capacity, provider=provider)
    )


def _hourly(hours, per_hour, **kw):
    """Hourly readings rising by ``per_hour`` of the weekly pool each hour."""
    return [_reading(T0 + h * HOUR, round(h * per_hour, 6), **kw) for h in range(hours + 1)]


def _measure(records, *, hours, plans=None, manual=None):
    return forecast.measure(
        records,
        plans=plans or {},
        now_s=T0 + hours * HOUR,
        window_s=hours * HOUR,
        manual_rates=manual or {},
    )


def test_steady_use_gives_the_exact_rate() -> None:
    result = _measure(_hourly(168, 0.005), hours=168)
    claude = result.groups["claude"]
    assert claude.other.pse_per_h == pytest.approx(0.005 * 6.25)
    assert claude.measured_pse == pytest.approx(168 * 0.005 * 6.25)
    assert claude.coverage == pytest.approx(1.0)
    assert claude.accounts == ("claude",)


def test_a_republished_reading_adds_nothing() -> None:
    records = _hourly(10, 0.01)
    # The cache source re-emits hour 5's reading at hour 5.5 with its old observation time.
    records.insert(6, _reading(T0 + 5.5 * HOUR, 0.05, observed_at_s=T0 + 5 * HOUR))
    assert _measure(records, hours=10).groups["claude"].measured_pse == pytest.approx(
        10 * 0.01 * 6.25
    )


def test_a_reset_counts_only_what_was_used_after_it() -> None:
    records = [
        _reading(T0, 0.70, resets_at_s=T0 + HOUR),
        _reading(T0 + 2 * HOUR, 0.10, resets_at_s=T0 + HOUR + SEVEN_DAYS),
    ]
    assert _measure(records, hours=2).groups["claude"].measured_pse == pytest.approx(
        0.10 * 6.25
    )


def test_a_quantization_drop_counts_zero() -> None:
    records = [_reading(T0, 0.50), _reading(T0 + HOUR, 0.49), _reading(T0 + 2 * HOUR, 0.49)]
    assert _measure(records, hours=2).groups["claude"].measured_pse == 0.0


def test_a_wobble_back_to_the_old_value_counts_zero() -> None:
    # 1% quantization flickers; measuring against the high-water mark keeps a
    # 0.50 -> 0.49 -> 0.50 flicker from inventing a point of use.
    records = [_reading(T0, 0.50), _reading(T0 + HOUR, 0.49), _reading(T0 + 2 * HOUR, 0.50)]
    assert _measure(records, hours=2).groups["claude"].measured_pse == 0.0


def test_tier_and_per_account_k_set_the_unit() -> None:
    records = _hourly(10, 0.01, account_id="claude_c", tier="max_5x", capacity=0.25)
    plain = _measure(records, hours=10).groups["claude"].measured_pse
    assert plain == pytest.approx(10 * 0.01 * 6.25 * 0.25)
    calibrated = _measure(
        records, hours=10, plans={"claude_c": Plan(weekly_to_session=9.8)}
    ).groups["claude"].measured_pse
    assert calibrated == pytest.approx(10 * 0.01 * 9.8 * 0.25)


def test_fable_share_is_the_fable_part_of_weekly_use() -> None:
    records = [
        _reading(T0 + h * HOUR, round(h * 0.01, 6), fable_used=round(h * 0.01, 6))
        for h in range(11)
    ]
    other = _measure(records, hours=10).groups["claude"].other
    # Fable cap is half the weekly cap, so equal fractions are half the PSE.
    assert other.fable_share == pytest.approx(0.5)


def test_groups_never_mix() -> None:
    records = _hourly(10, 0.01) + _hourly(
        10, 0.02, account_id="codex", provider="codex", tier="pro"
    )
    groups = _measure(records, hours=10).groups
    assert groups["claude"].measured_pse == pytest.approx(10 * 0.01 * 6.25)
    assert groups["codex"].measured_pse == pytest.approx(10 * 0.02 * 6.25)
    assert groups["codex"].accounts == ("codex",)


def test_only_the_window_is_counted_and_a_straddling_pair_is_prorated() -> None:
    records = [_reading(T0, 0.0), _reading(T0 + 4 * HOUR, 0.04)]
    # Window covers the last 2 of the 4 hours between the readings: half the rise.
    result = forecast.measure(
        records, plans={}, now_s=T0 + 4 * HOUR, window_s=2 * HOUR, manual_rates={}
    )
    assert result.groups["claude"].measured_pse == pytest.approx(0.02 * 6.25)


def test_coverage_is_the_share_of_hours_with_a_reading() -> None:
    records = [_reading(T0 + h * HOUR, round(h * 0.01, 6)) for h in range(0, 11, 2)]
    assert _measure(records, hours=10).groups["claude"].coverage == pytest.approx(0.5)


def test_a_manual_reserve_is_subtracted_and_floored_at_zero() -> None:
    records = _hourly(24, 0.01)
    measured = 0.01 * 6.25
    reserve = 0.04 * 6.25 / 24  # 4% of the weekly pool per day
    result = _measure(records, hours=24, manual={"claude": 0.04})
    assert result.groups["claude"].manual_pse_per_h == pytest.approx(reserve)
    assert result.groups["claude"].other.pse_per_h == pytest.approx(measured - reserve)
    floored = _measure(records, hours=24, manual={"claude": 1.0})
    assert floored.groups["claude"].other.pse_per_h == 0.0


def _run_record(t, cost_pse, pool_kind, *, fable=False):
    return {
        "v": 1,
        "t": t,
        "event": "run",
        "accounts": [],
        "run": {"group": "claude", "pool_kind": pool_kind, "cost_pse": cost_pse, "fable": fable},
    }


def test_run_work_is_its_own_stream() -> None:
    records = _hourly(10, 0.01) + [
        _run_record(T0 + 3 * HOUR, 0.05, "subscription"),
        _run_record(T0 + 4 * HOUR, 0.02, "credit"),
    ]
    claude = _measure(records, hours=10).groups["claude"]
    measured = 10 * 0.01 * 6.25
    # Subscription run work is inside the measured rise, so it leaves `other`.
    assert claude.other.pse_per_h == pytest.approx((measured - 0.05) / 10)
    # Credit run work never touched a subscription window, but it is still run demand.
    assert claude.run.pse_per_h == pytest.approx(0.07 / 10)


def test_an_account_with_no_reading_in_the_window_is_warned_about() -> None:
    records = _hourly(10, 0.01) + [_reading(T0 - 30 * HOUR, 0.5, account_id="claude_b")]
    result = _measure(records, hours=10)
    assert any("claude_b" in w for w in result.warnings)


# ======================================================================================
# The cache
# ======================================================================================


def _env(tmp_path):
    return {"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "state")}


def test_the_cache_round_trips_and_leaves_no_temp_file(tmp_path) -> None:
    env = _env(tmp_path)
    measured = _measure(_hourly(10, 0.01), hours=10)
    path = forecast.write_cache(measured, env=env)
    assert path == forecast.forecast_path(env)
    assert path.parent == tmp_path / "state" / "quota-router"
    assert sorted(p.name for p in path.parent.iterdir()) == ["forecast.json"]
    loaded, problem = forecast.read_cache(env=env)
    assert problem is None
    assert loaded == measured


@pytest.mark.parametrize(
    "body, fragment",
    [(None, "no forecast"), ("{not json", "unreadable"), ('{"v": 99}', "version")],
)
def test_a_bad_cache_is_a_problem_not_a_zero(tmp_path, body, fragment) -> None:
    env = _env(tmp_path)
    if body is not None:
        path = forecast.forecast_path(env)
        path.parent.mkdir(parents=True)
        path.write_text(body)
    loaded, problem = forecast.read_cache(env=env)
    assert loaded is None
    assert fragment in problem


def test_refresh_writes_when_missing_and_waits_when_fresh(tmp_path) -> None:
    env = _env(tmp_path)
    records = _hourly(10, 0.01)
    now = T0 + 10 * HOUR
    kwargs = dict(plans={}, manual_rates={}, window_s=10 * HOUR, refresh_s=900.0, env=env)
    assert forecast.refresh_if_due(lambda: records, now_s=now, **kwargs).refreshed
    first = forecast.read_cache(env=env)[0]
    assert not forecast.refresh_if_due(lambda: records, now_s=now + 600, **kwargs).refreshed
    later = forecast.refresh_if_due(lambda: records, now_s=now + 901, **kwargs)
    assert later.refreshed
    assert forecast.read_cache(env=env)[0].computed_at_s == now + 901
    assert first.computed_at_s == now


def test_a_cache_from_the_future_is_refreshed(tmp_path) -> None:
    env = _env(tmp_path)
    records = _hourly(10, 0.01)
    kwargs = dict(plans={}, manual_rates={}, window_s=10 * HOUR, refresh_s=900.0, env=env)
    forecast.refresh_if_due(lambda: records, now_s=T0 + 99 * HOUR, **kwargs)
    assert forecast.refresh_if_due(lambda: records, now_s=T0 + 10 * HOUR, **kwargs).refreshed


# ======================================================================================
# The CLI
# ======================================================================================


def _write_history(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_forecast_command_reads_history_without_touching_it(tmp_path) -> None:
    from tests.test_cli import _NO_CONFIG_FILES, run

    history = tmp_path / "hist" / "history.jsonl"
    _write_history(history, _hourly(168, 0.005))
    before = hashlib.sha256(history.read_bytes()).hexdigest()
    env = _env(tmp_path)
    code, out, err = run(
        ["forecast", "--json", "--history", str(history)],
        env,
        now_s=T0 + 168 * HOUR,
        cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    payload = json.loads(out)
    assert payload["groups"]["claude"]["other"]["pse_per_h"] == pytest.approx(0.005 * 6.25)
    assert hashlib.sha256(history.read_bytes()).hexdigest() == before
    # Without --refresh nothing is cached.
    assert not forecast.forecast_path(env).exists()


def test_forecast_refresh_writes_the_cache(tmp_path) -> None:
    from tests.test_cli import _NO_CONFIG_FILES, run

    history = tmp_path / "hist" / "history.jsonl"
    _write_history(history, _hourly(24, 0.01))
    env = _env(tmp_path)
    code, out, err = run(
        ["forecast", "--refresh", "--history", str(history)],
        env,
        now_s=T0 + 24 * HOUR,
        cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    assert "claude" in out
    assert forecast.read_cache(env=env)[0] is not None


def test_status_keeps_the_forecast_warm(tmp_path) -> None:
    from tests.test_cli import _NO_CONFIG_FILES, real_capture, run

    env = _env(tmp_path)
    code, _out, err = run(
        ["status", "--json"], env, snapshots=real_capture(), cwd=_NO_CONFIG_FILES
    )
    assert code == cli.EXIT_OK, err
    assert forecast.forecast_path(env).exists()


def test_a_credit_pool_reading_is_not_subscription_demand() -> None:
    # A credit pool snapshot carries a `credit` window, not a weekly one. Its spend reaches
    # the forecast through `run` lines; its reading must not join the claude accounts or
    # dilute their coverage.
    credit = hrecord(
        T0 + HOUR,
        account("anthropic_api_d", window("credit", 0.3, T0 + 100 * HOUR, T0 + HOUR,
                                          length_s=30 * 86400.0), provider="anthropic_api"),
    )
    claude = _measure(_hourly(10, 0.01) + [credit], hours=10).groups["claude"]
    assert claude.accounts == ("claude",)
    assert claude.coverage == pytest.approx(1.0)
