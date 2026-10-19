"""Demand belongs to a spendable model pool, including through cached forecasts."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from quota_router import cli, forecast, joint, replay
from quota_router.config import AccountConfig, Config
from tests.test_cli import NOW, account, window
from tests.test_forecast import HOUR, T0, _hourly


def _config():
    return Config(accounts={
        "gemini": AccountConfig("gemini", provider="antigravity", capacity=1.0),
        "claude_pool": AccountConfig(
            "claude_pool", provider="antigravity", capacity=0.088,
            env={"AGY_MODEL": "Claude Opus"},
        ),
    })


def _records():
    return sorted(
        _hourly(10, 0.01, account_id="gemini", provider="antigravity")
        + _hourly(10, 0.02, account_id="claude_pool", provider="antigravity"),
        key=lambda row: row["t"],
    )


def test_config_groups_use_pool_selector_and_keep_disabled_history():
    config = _config()
    config = replace(config, accounts={
        **config.accounts,
        "old": AccountConfig("old", provider="antigravity", enabled=False,
                             env={"AGY_MODEL": "claude"}),
    })
    groups = config.demand_groups()
    assert groups == {"gemini": "antigravity_gemini", "claude_pool": "antigravity_claude",
                      "old": "antigravity_claude"}
    assert config.engine_cfg()["demand_group_by_account"] == groups


def test_unknown_antigravity_pool_is_not_guessed_from_its_name():
    from quota_router.demand import group_for

    assert group_for("antigravity_claude_unknown", "antigravity") == "antigravity"
    assert group_for("api", "anthropic_api") == "claude"
    assert group_for("missing", "unsupported") is None


def test_forecast_separates_model_pools_before_adding_demand():
    result = forecast.measure(
        _records(), plans={}, now_s=T0 + 10 * HOUR, window_s=10 * HOUR,
        manual_rates={}, demand_groups=_config().demand_groups(),
    )
    assert set(result.groups) == {"antigravity_gemini", "antigravity_claude"}
    assert result.groups["antigravity_gemini"].other.pse_per_h == pytest.approx(0.0625)
    assert result.groups["antigravity_claude"].other.pse_per_h == pytest.approx(0.125)


def test_changed_membership_rejects_and_refreshes_a_fresh_cache(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    now = T0 + 10 * HOUR
    old = forecast.measure(_records(), plans={}, now_s=now, window_s=10 * HOUR,
                           manual_rates={})
    forecast.write_cache(old, env=env)
    groups = _config().demand_groups()
    assert "group" in forecast.grouping_problem(old, groups)
    refreshed = forecast.refresh_if_due(
        _records, now_s=now + 1, plans={}, manual_rates={}, window_s=10 * HOUR,
        refresh_s=900, env=env, demand_groups=groups,
    )
    assert refreshed.refreshed
    assert forecast.grouping_problem(refreshed.forecast, groups) is None
    changed = {**groups, "gemini": "antigravity_claude"}
    assert forecast.grouping_problem(refreshed.forecast, changed) is not None


def test_working_rates_use_separate_groups_and_reject_merged_cache(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    config = _config()
    snaps = [account(name, window("7d", 0.1), provider="antigravity")
             for name in config.accounts]
    records = _records()
    kwargs = dict(plans={}, now_s=T0 + 10 * HOUR, window_s=10 * HOUR, manual_rates={})
    old = forecast.measure(records, **kwargs)
    forecast.write_cache(old, env=env)
    warnings = []
    rates = cli._working_rates(config, snaps, kwargs["now_s"], env, warnings)
    assert set(rates) == {"antigravity_gemini", "antigravity_claude"}
    assert all(row["source"] == "default" for row in rates.values())
    assert any("group" in warning for warning in warnings)
    measured = forecast.measure(records, **kwargs, demand_groups=config.demand_groups())
    forecast.write_cache(measured, env=env)
    rates = cli._working_rates(config, snaps, kwargs["now_s"], env, [])
    assert rates["antigravity_gemini"]["pse_per_h"] == pytest.approx(0.0625)
    assert rates["antigravity_claude"]["pse_per_h"] == pytest.approx(0.125)


def test_joint_stock_keeps_the_configured_group():
    snap = account("gemini", window("7d", 0.1), provider="antigravity")
    stock = joint.subscription_stock(snap, now_s=NOW, capacity=1.0, plan=None,
                                     refills=True, group="antigravity_gemini")
    assert stock.group == "antigravity_gemini"


def test_joint_uses_pool_groups_and_rejects_an_old_merged_cache(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    config = _config()
    snap = account("gemini", window("7d", 0.1), provider="antigravity")
    measured = forecast.measure(
        _records(), plans={}, now_s=T0 + 10 * HOUR, window_s=10 * HOUR,
        manual_rates={}, demand_groups=config.demand_groups(),
    )
    measured = replace(measured, computed_at_s=NOW)
    forecast.write_cache(measured, env=env)
    kwargs = dict(
        config=config, base_config=config, fleet=[snap], candidates=[snap],
        state=cli.StateSnapshot(), now_s=NOW, env=env,
        args=SimpleNamespace(job_pse=0.01), model=SimpleNamespace(model_class=None),
        capability_ctx=None, warnings=[], objective_override="joint",
    )
    context, report = cli._build_joint(**kwargs)
    assert report["used"] == "joint"
    # Missing the unrelated Claude-model group does not make this Gemini decision
    # a partial view of its own group.
    assert report["partial_fleet"] is False
    assert {stock.group for stock in context.stocks} == {"antigravity_gemini"}
    old = replace(measured, groups={"antigravity": measured.groups["antigravity_gemini"]})
    forecast.write_cache(old, env=env)
    context, report = cli._build_joint(**kwargs)
    assert context is None
    assert "grouping changed" in report["fallback"]


def test_replay_filters_model_pools_and_drains_matching_terminal_stocks():
    specs, trace = replay.reconstruct(
        _records(), group="antigravity_gemini", plans={}, plan_ends={},
        start_s=T0 + 5 * HOUR, end_s=T0 + 10 * HOUR, bin_s=HOUR,
        demand_groups=_config().demand_groups(),
    )
    assert [spec.id for spec in specs] == ["gemini"]
    assert set(trace.by_account) == {"gemini"}
    kwargs = dict(
        records=_records(), waste_rows=None, plans={}, plan_ends={},
        count_from_s=T0 + 5 * HOUR, end_s=T0 + 10 * HOUR,
        bin_s=HOUR, policies=["joint", "edf"],
    )
    gemini = replay.run_replay(
        **kwargs, group="antigravity_gemini", demand_groups=_config().demand_groups(),
    )
    # Relabeling the same measured demand and stocks cannot change the result. In
    # particular, the terminal projection must drain the finer group's stocks too.
    relabeled = replay.run_replay(
        **kwargs, group="claude", demand_groups={
            "gemini": "claude", "claude_pool": "antigravity_claude",
        },
    )
    # At hour 10 the 6.25-PSE pool is 10% used. The trailing week contains
    # ten 1%-bar rises, so its rate is .625 / 168 PSE/h. Its deadline is
    # hour 171, leaving 161h to drain. This independent value catches a
    # group mismatch even if both relabeled arms suffer the same bug.
    expected_tail = 6.25 * 0.9 - (0.625 / 168) * 161
    for actual, expected in zip(gemini.policies, relabeled.policies):
        assert actual.served_pse == pytest.approx(0.3125)
        assert actual.tail_pse == pytest.approx(expected_tail)
        assert actual.tail_pse == pytest.approx(expected.tail_pse)
        assert actual.metric_pse == pytest.approx(expected.metric_pse)
