"""``pick`` shares measured group demand across eligible accounts.

The waste term is ``weekly left - allocated rate x hours to reset``. The historical
Claude scenario below must still favor its earlier reset under measured demand.

* Explicit scalar rates override group maps, then legacy provider maps, then defaults.
* The CLI reports each group's budget and every default; explain exposes each share.
* The five-hour ceiling applies to the group budget before sharing it.
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


def _group(other, run_rate, coverage=1.0, accounts=("claude", "claude_e")):
    return forecast.GroupForecast(
        other=forecast.StreamRate(pse_per_h=other, fable_share=0.4),
        run=forecast.StreamRate(pse_per_h=run_rate, fable_share=1.0),
        coverage=coverage,
        accounts=accounts,
        measured_pse=(other + run_rate) * 168,
        manual_pse_per_h=0.0,
    )


def _forecast(env, *, computed_at_s=NOW, coverage=1.0, other=MEASURED_OTHER,
              run_rate=MEASURED_RUN, group="claude", extra_groups=None) -> None:
    members = {
        "claude": ("claude", "claude_e"), "codex": ("codex",),
        "antigravity_claude": ("antigravity_claude",),
        "antigravity_gemini": ("antigravity_gemini",),
    }
    groups = {group: _group(other, run_rate, coverage, members.get(group, ()))}
    for name, (extra_other, extra_run) in (extra_groups or {}).items():
        groups[name] = _group(extra_other, extra_run, accounts=members.get(name, ()))
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
    # claude still drains before Monday; claude_e gets only its share of future work.
    scores = {row["account"]: row["score"] for row in payload["ranked"]}
    assert 0.0 < scores["claude"] < 1e-6  # the deadline term: nothing wasted
    allocated = MEASURED * 1.5 / (2.875 + 1.5)
    assert scores["claude_e"] == pytest.approx(1.5 - allocated * 7.7)


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
    # The earlier pool still has quota at risk under its share of the capped rate.
    assert payload["decision"]["account"] == "claude_e"


def test_explain_names_the_rate_and_its_source(env) -> None:
    _, err = _pick(env, "--explain")
    assert "rate: claude 0.040 PSE/h default: " in err
    _forecast(env, computed_at_s=NOW - 20 * 60)
    _, err = _pick(env, "--explain")
    assert "rate: claude 0.159 PSE/h measured (forecast 20m" in err
    assert "rate PSE/h" in err
    for name, remaining in (("claude", 2.875), ("claude_e", 1.5)):
        row = next(line for line in err.splitlines()
                   if len(line.split()) > 1 and line.split()[0].isdigit()
                   and line.split()[1] == name)
        assert f"{MEASURED * remaining / 4.375:.4f}" in row


def test_explain_json_carries_the_same_rate_block(env) -> None:
    _forecast(env)
    code, out, err = run(["explain", "--json"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    payload = json.loads(out)
    assert payload["rate"]["claude"]["source"] == "measured"
    rates = {row["account_id"]: row["rate_pse_per_hour"] for row in payload["ranked"]}
    assert rates == pytest.approx({
        "claude": MEASURED * 2.875 / 4.375,
        "claude_e": MEASURED * 1.5 / 4.375,
    })
    assert sum(rates.values()) == pytest.approx(MEASURED)


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
env = { AGY_MODEL = "claude" }
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
    _forecast(env, extra_groups={"antigravity_claude": (ANTIGRAVITY_OWN, 0.0),
                                 "codex": (CODEX_MEASURED, 0.0)})
    payload = _pick_with_fallback(env)
    report = payload["rate"]["antigravity_claude"]
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
    _forecast(env, extra_groups={"antigravity_claude": (ANTIGRAVITY_OWN, 0.0)})
    payload = _pick_with_fallback(env)
    report = payload["rate"]["antigravity_claude"]
    assert report["measured_pse_per_h"] == pytest.approx(MEASURED)  # claude only; no codex group
    assert report["measured_pse_per_h"] != pytest.approx(ANTIGRAVITY_OWN)


def test_a_fallback_only_provider_with_nothing_measured_behind_it_defaults_and_says_so(env) -> None:
    _config(env, FALLBACK_CONFIG)
    _forecast(env, group="antigravity_claude", other=ANTIGRAVITY_OWN, run_rate=0.0)
    payload = _pick_with_fallback(env)
    report = payload["rate"]["antigravity_claude"]
    assert report["source"] == "default"
    assert report["pse_per_h"] == pytest.approx(scoring.DEFAULT_RATE_PSE_PER_HOUR)
    assert "stands behind no measured demand" in report["reason"]
    assert any(w.startswith("antigravity_claude: working rate defaulted") for w in payload["warnings"])


def test_explain_prints_the_fallback_rule(env) -> None:
    _config(env, FALLBACK_CONFIG)
    _forecast(env, extra_groups={"codex": (CODEX_MEASURED, 0.0)})
    code, out, err = run(
        ["pick", "--dry-run", "--json", "--explain", "--only", "claude,claude_e,antigravity_claude"],
        env, snapshots=(*_scenario(), _fallback_pool()), cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    assert "rate: antigravity_claude 0.200 PSE/h measured" in err
    assert "capped from the measured 0.221 PSE/h; fallback pool: credited with the demand it stands behind" in err


# The credit exists because a placeholder-sized pool read at its own starved rate looks
# like the fleet's biggest waste. A measured `capacity` removes that premise, and on
# 2026-10-19 the credit was the only thing keeping both sized Gemini pools at 0.000 PSE at
# risk, so every capability pick ranked them by deadline, behind every account that resets sooner.
SIZED_FALLBACK_CONFIG = FALLBACK_CONFIG + "capacity = 2.0\n"
ANTIGRAVITY_OWN_ABOVE_DEFAULT = 0.05


def _second_fallback_pool():
    return account(
        "antigravity_gemini",
        window("5h", 0.0, length_s=FIVE_HOURS, resets_in_s=5 * HOUR),
        window("7d", 0.0, resets_in_s=7 * 24 * HOUR),
        tier="unknown",
    )


def test_a_fallback_only_provider_whose_pools_are_all_sized_is_credited_with_its_own_demand(env) -> None:
    _config(env, SIZED_FALLBACK_CONFIG)
    _forecast(env, extra_groups={"antigravity_claude": (ANTIGRAVITY_OWN_ABOVE_DEFAULT, 0.0),
                                 "codex": (CODEX_MEASURED, 0.0)})
    code, out, err = run(
        ["pick", "--dry-run", "--json", "--explain", "--only", "claude,claude_e,antigravity_claude"],
        env, snapshots=(*_scenario(), _fallback_pool()), cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    payload = json.loads(out)
    report = payload["rate"]["antigravity_claude"]
    assert report["source"] == "measured"
    assert report["measured_pse_per_h"] == pytest.approx(ANTIGRAVITY_OWN_ABOVE_DEFAULT)
    assert report["pse_per_h"] == pytest.approx(ANTIGRAVITY_OWN_ABOVE_DEFAULT)
    assert report["capped"] is False
    assert "stands behind" not in report["reason"]
    assert "measured capacity" in report["reason"]
    assert _rate_warnings(payload) == []
    # 2.0 x 6.25 = 12.5 PSE a week, of which 0.05 PSE/h over 168 h drains 8.4: the rest
    # is at risk, and it outweighs claude_e's 0.27 PSE.
    scores = {row["account"]: row["score"] for row in payload["ranked"]}
    assert scores["antigravity_claude"] > 1.0, scores
    assert payload["decision"]["account"] == "antigravity_claude"
    assert "rate: antigravity_claude 0.050 PSE/h measured" in err
    assert "fallback pool with a measured capacity" in err


def test_a_sized_fallback_pool_below_the_default_rate_still_says_so(env) -> None:
    """Its own demand is the truth about it once it is sized, starved or not, so the
    usual below-default warning applies to it like any measured provider."""
    _config(env, SIZED_FALLBACK_CONFIG)
    _forecast(env, extra_groups={"antigravity_claude": (ANTIGRAVITY_OWN, 0.0)})
    payload = _pick_with_fallback(env)
    assert payload["rate"]["antigravity_claude"]["pse_per_h"] == pytest.approx(ANTIGRAVITY_OWN)
    warnings = _rate_warnings(payload)
    assert len(warnings) == 1, warnings
    assert warnings[0].startswith("antigravity_claude: measured demand 0.005 PSE/h")


def test_a_sized_fallback_pool_above_the_ceiling_warns_like_any_measured_provider(env) -> None:
    """Only a credited pool sits at the ceiling by design. A sized one is scored on its
    own demand, so demand above the ceiling is news and is said."""
    _config(env, SIZED_FALLBACK_CONFIG)
    _forecast(env, extra_groups={"antigravity_claude": (0.3, 0.0)})
    payload = _pick_with_fallback(env)
    report = payload["rate"]["antigravity_claude"]
    assert report["capped"] is True
    assert report["pse_per_h"] == pytest.approx(scoring.MAX_SUSTAINED_RATE_PSE_PER_HOUR)
    warnings = _rate_warnings(payload)
    assert len(warnings) == 1, warnings
    assert warnings[0].startswith("antigravity_claude: measured demand 0.300 PSE/h exceeds the five-hour ceiling")


def test_one_unsized_fallback_pool_keeps_the_credit_for_its_provider(env) -> None:
    """Fallback sizing eligibility is per provider. While any of its pools has the
    placeholder size, its own starved demand would make that pool read as the biggest
    waste, so the provider keeps the demand it stands behind."""
    _config(env, SIZED_FALLBACK_CONFIG + '\n[accounts.antigravity_gemini]\nprovider = "antigravity"\nunmetered = "fallback"\n')
    _forecast(env, extra_groups={"antigravity_claude": (ANTIGRAVITY_OWN, 0.0),
                                 "codex": (CODEX_MEASURED, 0.0)})
    code, out, err = run(
        ["pick", "--dry-run", "--json", "--only", "claude,claude_e,antigravity_claude,antigravity_gemini"],
        env, snapshots=(*_scenario(), _fallback_pool(), _second_fallback_pool()),
        cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    report = json.loads(out)["rate"]["antigravity_claude"]
    assert report["measured_pse_per_h"] == pytest.approx(MEASURED + CODEX_MEASURED)
    assert "stands behind" in report["reason"]


def test_a_plain_pick_never_returns_a_sized_fallback_pool_even_when_it_wastes_most(env) -> None:
    """Sizing a pool lets its waste count, and then it outranks every Claude account. The
    capability-only rule is then the one thing that keeps a plain pick, whose caller
    assumes Claude, from returning it."""
    _config(env, SIZED_FALLBACK_CONFIG)
    _forecast(env, extra_groups={"antigravity_claude": (ANTIGRAVITY_OWN_ABOVE_DEFAULT, 0.0)})
    snapshots = (*_scenario(), _fallback_pool())
    # Named with --only, it wins on waste: the exclusion below is what stops it.
    code, out, err = run(["pick", "--dry-run", "--json", "--only", "claude,claude_e,antigravity_claude"],
                         env, snapshots=snapshots, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert json.loads(out)["decision"]["account"] == "antigravity_claude"
    code, out, err = run(["pick", "--dry-run", "--json"], env, snapshots=snapshots,
                         cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    payload = json.loads(out)
    assert payload["decision"]["account"] == "claude_e"
    excluded = {row["account"]: row["reason"] for row in payload["excluded"]}
    assert "serves capability picks only" in excluded["antigravity_claude"]


def test_a_provider_with_a_metered_account_uses_its_own_demand_whatever_its_sizes(env) -> None:
    """The credit is for a provider whose accounts are all fallback-only. One metered
    account makes the provider measured, so an unsized fallback pool beside it changes
    nothing."""
    _config(env, '[accounts.antigravity_claude]\nprovider = "antigravity"\nenv = { AGY_MODEL = "claude" }\n'
                 '\n[accounts.antigravity_gemini]\nprovider = "antigravity"\nunmetered = "fallback"\n')
    _forecast(env, extra_groups={"antigravity_claude": (ANTIGRAVITY_OWN, 0.0),
                                 "codex": (CODEX_MEASURED, 0.0)})
    code, out, err = run(
        ["pick", "--dry-run", "--json", "--only", "claude,claude_e,antigravity_claude,antigravity_gemini"],
        env, snapshots=(*_scenario(), _fallback_pool(), _second_fallback_pool()),
        cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    report = json.loads(out)["rate"]["antigravity_claude"]
    assert report["measured_pse_per_h"] == pytest.approx(ANTIGRAVITY_OWN)
    assert report["reason"] is None  # a plain measured provider: no fallback rule applied


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
