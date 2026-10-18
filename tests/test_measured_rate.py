"""``pick`` credits each account with the *measured* working rate, not a hardcoded guess.

The waste term is ``weekly left - rate x hours to reset``, so the rate decides whether
a long horizon can drain a big pool. On the evening of 2026-10-17 PT the hardcoded
0.04 PSE/h said that neither ``claude`` (46% of its week left, 40.7h to reset) nor
``claude_e`` (24% left, 7.7h to reset) could be drained, so the bigger pile won and the
pool expiring in eight hours was left to expire. The forecast had measured 0.16 PSE/h for the claude group, at
which ``claude`` is drained anyway and ``claude_e`` loses 0.27 PSE unless spent now.

* The scorer reads a per-provider rate map. An explicit scalar (what ``replay`` sets)
  still wins; nothing configured means the documented default; a malformed rate raises.
* ``pick`` fills the map from the forecast cache under the same age and coverage gates
  the joint objective uses, reports the rate applied per provider in ``rate``, and warns
  on every fall-through to the default.
* A measured rate above the five-hour ceiling is applied at the ceiling, and says so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quota_router import cli, forecast, scoring
from quota_router.pse import Stocks, wasted_pse
from tests.test_cli import FIVE_HOURS, NOW, _NO_CONFIG_FILES, account, run, window

HOUR = 3600.0
MEASURED_OTHER = 0.147
MEASURED_RUN = 0.012
MEASURED = MEASURED_OTHER + MEASURED_RUN


def _scenario():
    """2026-10-18T06:14Z, as the router's own history recorded it."""
    return (
        account(
            "claude",
            window("5h", 0.38, length_s=FIVE_HOURS, resets_in_s=0.7 * HOUR),
            window("7d", 0.54, resets_in_s=40.7 * HOUR),
        ),
        account(
            "claude_e",
            window("5h", 0.02, length_s=FIVE_HOURS, resets_in_s=4.6 * HOUR),
            window("7d", 0.76, resets_in_s=7.7 * HOUR),
        ),
    )


def _group(other, run_rate, coverage=1.0):
    return forecast.GroupForecast(
        other=forecast.StreamRate(pse_per_h=other, fable_share=0.4),
        run=forecast.StreamRate(pse_per_h=run_rate, fable_share=1.0),
        coverage=coverage,
        accounts=("claude", "claude_e"),
        measured_pse=(other + run_rate) * 168,
        manual_pse_per_h=0.0,
    )


def _forecast(env, *, computed_at_s=NOW, coverage=1.0, other=MEASURED_OTHER,
              run_rate=MEASURED_RUN, group="claude", extra_groups=None) -> None:
    groups = {group: _group(other, run_rate, coverage)}
    for name, (extra_other, extra_run) in (extra_groups or {}).items():
        groups[name] = _group(extra_other, extra_run)
    forecast.write_cache(
        forecast.Forecast(computed_at_s=computed_at_s, window_s=7 * 86400.0, groups=groups),
        env=env,
    )


def _pick(env, *extra, now_s=NOW):
    code, out, err = run(
        ["pick", "--dry-run", "--json", *extra], env, snapshots=_scenario(),
        cwd=_NO_CONFIG_FILES, now_s=now_s,
    )
    assert code == cli.EXIT_OK, err
    return json.loads(out), err


def _rate_warnings(payload):
    return [
        w for w in payload["warnings"]
        if "working rate" in w or "ceiling" in w or "below the" in w
    ]


@pytest.fixture()
def env(tmp_path):
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
    }


# ======================================================================================
# The scorer: where the rate comes from
# ======================================================================================


def test_rate_for_reads_the_provider_map_and_defaults_the_rest() -> None:
    cfg = {"rate_pse_per_hour_by_provider": {"claude": 0.16}}
    assert scoring.rate_for(cfg, "claude") == pytest.approx(0.16)
    assert scoring.rate_for(cfg, "codex") == scoring.DEFAULT_RATE_PSE_PER_HOUR
    assert scoring.rate_for(None, "claude") == scoring.DEFAULT_RATE_PSE_PER_HOUR
    assert scoring.rate_for({}, "claude") == scoring.DEFAULT_RATE_PSE_PER_HOUR


def test_an_explicit_scalar_rate_still_wins_so_replay_keeps_its_meaning() -> None:
    cfg = {"rate_pse_per_hour": 0.5, "rate_pse_per_hour_by_provider": {"claude": 0.16}}
    assert scoring.rate_for(cfg, "claude") == pytest.approx(0.5)


@pytest.mark.parametrize(
    "cfg",
    [
        {"rate_pse_per_hour_by_provider": {"claude": "fast"}},
        {"rate_pse_per_hour_by_provider": {"claude": -0.1}},
        {"rate_pse_per_hour_by_provider": {"claude": float("nan")}},
        {"rate_pse_per_hour_by_provider": {"claude": True}},
        {"rate_pse_per_hour_by_provider": [0.16]},
        {"rate_pse_per_hour": "0.16 PSE/h"},
    ],
)
def test_a_malformed_rate_raises_rather_than_defaulting(cfg) -> None:
    with pytest.raises(ValueError):
        scoring.rate_for(cfg, "claude")


def test_account_score_uses_the_providers_measured_rate() -> None:
    claude, _ = _scenario()
    cfg = {"rate_pse_per_hour_by_provider": {"claude": MEASURED}}
    row = scoring.account_score(claude, NOW, cfg)
    stocks = scoring.stocks_from_snapshot(claude, NOW, claude.capacity)
    assert row.rate_pse_per_hour == pytest.approx(MEASURED)
    # Nothing wasted at the measured rate: the score is the earliest-deadline term,
    # below any waste worth acting on.
    assert wasted_pse(stocks, now_s=NOW, rate_pse_per_hour=MEASURED) == 0.0
    assert row.score == pytest.approx(
        scoring.pse_objective(stocks, NOW, None, MEASURED)[0], abs=1e-12
    )
    assert 0.0 < row.score < 1e-6
    assert "nothing wasted" in row.reason
    # And at the default the same account carries 1.25 PSE of phantom waste.
    at_default = scoring.account_score(claude, NOW, {})
    assert at_default.rate_pse_per_hour == scoring.DEFAULT_RATE_PSE_PER_HOUR
    assert at_default.score == pytest.approx(1.247, abs=0.01)
    assert "at risk of expiring" in at_default.reason


def test_the_recorded_rate_is_the_one_applied_after_the_ceiling() -> None:
    claude, _ = _scenario()
    row = scoring.account_score(claude, NOW, {"rate_pse_per_hour_by_provider": {"claude": 0.9}})
    assert row.rate_pse_per_hour == pytest.approx(scoring.MAX_SUSTAINED_RATE_PSE_PER_HOUR)
    assert "rate_pse_per_hour" in row.to_dict()


# ======================================================================================
# pick: the forecast feeds the scorer, and the payload says what was applied
# ======================================================================================


def test_without_a_forecast_the_default_ranks_the_bigger_pile_first_and_says_so(env) -> None:
    payload, _ = _pick(env)
    assert payload["decision"]["account"] == "claude"
    report = payload["rate"]["claude"]
    assert report["source"] == "default"
    assert report["pse_per_h"] == pytest.approx(scoring.DEFAULT_RATE_PSE_PER_HOUR)
    assert report["measured_pse_per_h"] is None
    assert report["forecast_age_s"] is None
    assert report["capped"] is False
    assert "no forecast" in report["reason"]
    warnings = _rate_warnings(payload)
    assert len(warnings) == 1 and warnings[0].startswith("claude: working rate defaulted")
    assert "no forecast" in warnings[0]


def test_with_the_measured_rate_the_pool_expiring_first_wins(env) -> None:
    _forecast(env, computed_at_s=NOW - 20 * 60)
    payload, _ = _pick(env)
    assert payload["decision"]["account"] == "claude_e"
    report = payload["rate"]["claude"]
    assert report == {
        "pse_per_h": pytest.approx(MEASURED),
        "measured_pse_per_h": pytest.approx(MEASURED),
        "source": "measured",
        "forecast_age_s": pytest.approx(20 * 60),
        "capped": False,
        "reason": None,
    }
    assert _rate_warnings(payload) == []
    # claude is drained before Monday either way; claude_e loses 0.27 PSE unless spent.
    scores = {row["account"]: row["score"] for row in payload["ranked"]}
    assert 0.0 < scores["claude"] < 1e-6  # the deadline term: nothing wasted
    assert scores["claude_e"] == pytest.approx(0.276, abs=0.01)


def test_the_rate_block_lists_only_providers_whose_score_used_a_rate(env) -> None:
    _forecast(env)
    payload, _ = _pick(env)
    assert set(payload["rate"]) == {"claude"}


@pytest.mark.parametrize(
    ("setup", "fragment"),
    [
        (lambda env: _forecast(env, computed_at_s=NOW - 7 * HOUR), "stale"),
        (lambda env: _forecast(env, computed_at_s=NOW + HOUR), "stale"),
        (lambda env: _forecast(env, coverage=0.2), "coverage"),
        (lambda env: _forecast(env, group="codex"), "no claude group"),
    ],
)
def test_an_unusable_forecast_falls_back_to_the_default_and_says_why(env, setup, fragment) -> None:
    setup(env)
    payload, _ = _pick(env)
    assert payload["decision"]["account"] == "claude"
    report = payload["rate"]["claude"]
    assert report["source"] == "default"
    assert report["pse_per_h"] == pytest.approx(scoring.DEFAULT_RATE_PSE_PER_HOUR)
    assert fragment in report["reason"]
    warnings = _rate_warnings(payload)
    assert len(warnings) == 1 and fragment in warnings[0]


def test_a_stale_forecast_still_reports_its_age(env) -> None:
    _forecast(env, computed_at_s=NOW - 7 * HOUR)
    payload, _ = _pick(env)
    assert payload["rate"]["claude"]["forecast_age_s"] == pytest.approx(7 * HOUR)


def test_a_measured_rate_above_the_ceiling_is_applied_at_the_ceiling_and_reported(env) -> None:
    _forecast(env, other=0.5, run_rate=0.0)
    payload, _ = _pick(env)
    report = payload["rate"]["claude"]
    assert report["source"] == "measured"
    assert report["measured_pse_per_h"] == pytest.approx(0.5)
    assert report["pse_per_h"] == pytest.approx(scoring.MAX_SUSTAINED_RATE_PSE_PER_HOUR)
    assert report["capped"] is True
    warnings = _rate_warnings(payload)
    assert len(warnings) == 1 and "ceiling" in warnings[0]
    # At the ceiling neither pool wastes anything, so the earlier deadline wins.
    assert payload["decision"]["account"] == "claude_e"


def test_explain_names_the_rate_and_its_source(env) -> None:
    _, err = _pick(env, "--explain")
    assert "rate: claude 0.040 PSE/h default: " in err
    _forecast(env, computed_at_s=NOW - 20 * 60)
    _, err = _pick(env, "--explain")
    assert "rate: claude 0.159 PSE/h measured (forecast 20m" in err


def test_explain_json_carries_the_same_rate_block(env) -> None:
    _forecast(env)
    code, out, err = run(["explain", "--json"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    payload = json.loads(out)
    assert payload["rate"]["claude"]["source"] == "measured"


def test_the_joint_objective_still_reads_the_same_forecast_gates(env) -> None:
    """The refactor shares the gates with ``_build_joint``; its messages must not drift."""
    xdg = Path(env["XDG_CONFIG_HOME"]) / "quota-router"
    xdg.mkdir(parents=True, exist_ok=True)
    (xdg / "config.toml").write_text('[engine]\nobjective = "joint"\n', encoding="utf-8")
    _forecast(env, coverage=0.2)
    payload, _ = _pick(env)
    assert payload["objective"]["used"] == "own_waste"
    assert "coverage 20%, below 50%" in payload["objective"]["fallback"]
    assert "coverage 20%, below 50%" in payload["rate"]["claude"]["reason"]


# ======================================================================================
# A fallback-only provider is credited with the demand it stands behind
# ======================================================================================

FALLBACK_CONFIG = """
[accounts.antigravity_claude]
provider = "antigravity"
unmetered = "fallback"
"""
CODEX_MEASURED = 0.062
ANTIGRAVITY_OWN = 0.005


def _config(env, body: str) -> None:
    xdg = Path(env["XDG_CONFIG_HOME"]) / "quota-router"
    xdg.mkdir(parents=True, exist_ok=True)
    (xdg / "config.toml").write_text(body, encoding="utf-8")


def _fallback_pool(used: float = 0.0):
    """An Antigravity pool as the /usage reader reports it: fresh, a week to its reset."""
    return account(
        "antigravity_claude",
        window("5h", used, length_s=FIVE_HOURS, resets_in_s=5 * HOUR),
        window("7d", used, resets_in_s=7 * 24 * HOUR),
        tier="unknown",
    )


def _pick_with_fallback(env):
    code, out, err = run(
        ["pick", "--dry-run", "--json", "--only", "claude,claude_e,antigravity_claude"],
        env, snapshots=(*_scenario(), _fallback_pool()), cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    return json.loads(out)


def test_a_fallback_only_provider_is_credited_with_the_demand_it_stands_behind(env) -> None:
    """Its own 0.005 PSE/h would make a full placeholder-sized pool the fleet's biggest
    waste; the demand it stands behind (every metered group) drains it on paper."""
    _config(env, FALLBACK_CONFIG)
    _forecast(env, extra_groups={"antigravity": (ANTIGRAVITY_OWN, 0.0),
                                 "codex": (CODEX_MEASURED, 0.0)})
    payload = _pick_with_fallback(env)
    report = payload["rate"]["antigravity"]
    assert report["source"] == "measured"
    assert report["measured_pse_per_h"] == pytest.approx(MEASURED + CODEX_MEASURED)
    assert report["pse_per_h"] == pytest.approx(scoring.MAX_SUSTAINED_RATE_PSE_PER_HOUR)
    assert report["capped"] is True
    assert "stands behind" in report["reason"]
    assert f"claude {MEASURED:.3f}" in report["reason"] and f"codex {CODEX_MEASURED:.3f}" in report["reason"]
    assert payload["rate"]["claude"]["pse_per_h"] == pytest.approx(MEASURED)
    # No warning: the ceiling is the expected place for a pool credited with everything.
    assert _rate_warnings(payload) == []
    scores = {row["account"]: row["score"] for row in payload["ranked"]}
    assert scores["antigravity_claude"] < 1e-6, scores
    assert payload["decision"]["account"] == "claude_e"


def test_a_fallback_only_provider_never_uses_its_own_starved_rate(env) -> None:
    _config(env, FALLBACK_CONFIG)
    _forecast(env, extra_groups={"antigravity": (ANTIGRAVITY_OWN, 0.0)})
    payload = _pick_with_fallback(env)
    report = payload["rate"]["antigravity"]
    assert report["measured_pse_per_h"] == pytest.approx(MEASURED)  # claude only; no codex group
    assert report["measured_pse_per_h"] != pytest.approx(ANTIGRAVITY_OWN)


def test_a_fallback_only_provider_with_nothing_measured_behind_it_defaults_and_says_so(env) -> None:
    _config(env, FALLBACK_CONFIG)
    _forecast(env, group="antigravity", other=ANTIGRAVITY_OWN, run_rate=0.0)
    payload = _pick_with_fallback(env)
    report = payload["rate"]["antigravity"]
    assert report["source"] == "default"
    assert report["pse_per_h"] == pytest.approx(scoring.DEFAULT_RATE_PSE_PER_HOUR)
    assert "stands behind no measured demand" in report["reason"]
    assert any(w.startswith("antigravity: working rate defaulted") for w in payload["warnings"])


def test_explain_prints_the_fallback_rule(env) -> None:
    _config(env, FALLBACK_CONFIG)
    _forecast(env, extra_groups={"codex": (CODEX_MEASURED, 0.0)})
    code, out, err = run(
        ["pick", "--dry-run", "--json", "--explain", "--only", "claude,claude_e,antigravity_claude"],
        env, snapshots=(*_scenario(), _fallback_pool()), cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    assert "rate: antigravity 0.200 PSE/h measured" in err
    assert "capped from the measured 0.221 PSE/h; fallback pool: credited with the demand it stands behind" in err


# ======================================================================================
# Review follow-ups
# ======================================================================================


def test_status_never_warns_about_a_rate_it_does_not_use(env) -> None:
    """``status`` surfaces no waste-term result and is the command that refreshes the
    forecast, so a default-rate warning there would tell the operator to run ``status``."""
    code, out, err = run(["status", "--json"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    payload = json.loads(out)
    assert not [w for w in payload["warnings"] if "working rate" in w], payload["warnings"]
    code, out, err = run(["status"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert "working rate" not in out + err


@pytest.mark.parametrize("measured", [0.0, 0.02])
def test_a_measured_rate_below_the_default_is_applied_and_reported(env, measured) -> None:
    """A trailing average reads 0.000 after a week away. It is applied as measured --
    it is the truth about demand -- but said, because the bigger pile then wins again."""
    _forecast(env, other=measured, run_rate=0.0)
    payload, err = _pick(env, "--explain")
    report = payload["rate"]["claude"]
    assert report["source"] == "measured"
    assert report["pse_per_h"] == pytest.approx(measured)
    warnings = _rate_warnings(payload)
    assert len(warnings) == 1, warnings
    assert f"measured demand {measured:.3f} PSE/h" in warnings[0]
    assert "below the 0.040 PSE/h default" in warnings[0]
    assert payload["decision"]["account"] == "claude"  # the bigger pile, said out loud
    assert f"rate: claude {measured:.3f} PSE/h measured" in err
