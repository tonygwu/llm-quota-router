"""The joint objective wired into ``select`` and ``pick``, behind ``[engine] objective``.

What must hold:

* Hysteresis brakes only inside a tie class. A leader that avoids more than
  ``objective_gap`` PSE more waste takes the seat at once; within the gap the dwell and
  the slack margin keep the incumbent as today.
* Deprioritization still outranks the objective.
* Under ``objective = "joint"`` a pick ranks by fleet waste avoided. The case it exists
  for: two accounts both waste, so a job avoids the same on either, and the earlier
  deadline wins -- where today's objective picks the bigger pile.
* A missing or stale forecast falls back to ``own_waste`` for the whole decision, says
  so in a warning, and records why in history.
* Shadow mode decides by ``own_waste`` and records the joint winner beside it.
* With the switch off nothing changes: no ``objective`` key anywhere.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from quota_router import cli, forecast
from quota_router.select import SelectionState, select
from tests.test_cli import (
    FIVE_HOURS,
    NOW,
    _NO_CONFIG_FILES,
    account,
    run,
    window,
)

HOUR = 3600.0


class FakeJoint:
    """Duck-typed stand-in: fixed scores, so hysteresis is tested on its own."""

    def __init__(self, scores, gap):
        self.scores = scores
        self.objective_gap = gap

    def rescore(self, rows):
        return [
            replace(row, score=self.scores[row.account_id], reason=f"fake {row.account_id}")
            for row in rows
        ]


def _pair():
    return (
        account("claude", window("7d", 0.5, resets_in_s=50 * HOUR)),
        account("claude_b", window("7d", 0.5, resets_in_s=60 * HOUR)),
    )


def test_inside_a_tie_class_the_incumbent_keeps_its_seat() -> None:
    state = SelectionState()
    first = select(_pair(), NOW, state=state,
                   joint=FakeJoint({"claude": 0.0031, "claude_b": 0.0030}, 0.0003))
    assert first.chosen == "claude"
    second = select(_pair(), NOW + 1, state=state,
                    joint=FakeJoint({"claude": 0.0030, "claude_b": 0.0032}, 0.0003))
    assert second.chosen == "claude"
    assert second.sticky_applied


def test_a_challenger_beyond_the_gap_takes_the_seat_at_once() -> None:
    state = SelectionState()
    select(_pair(), NOW, state=state,
           joint=FakeJoint({"claude": 0.0031, "claude_b": 0.0030}, 0.0003))
    second = select(_pair(), NOW + 1, state=state,
                    joint=FakeJoint({"claude": 0.0, "claude_b": 0.003}, 0.0003))
    assert second.chosen == "claude_b"
    assert not second.sticky_applied


def test_deprioritization_still_outranks_the_objective() -> None:
    decision = select(
        _pair(), NOW, cfg={"deprioritized_accounts": ["claude"]},
        joint=FakeJoint({"claude": 0.003, "claude_b": 0.0}, 0.0003),
    )
    assert decision.chosen == "claude_b"


# ======================================================================================
# pick
# ======================================================================================


def _scenario():
    """claude_b: 10% of its week left, resets in 2h. claude: 80% left, resets in 24h.

    Own-waste shares the default rate over both pools and picks claude, the bigger
    pile. Spread over the fleet, 0.11 PSE/h cannot drain either before its reset, so a
    job avoids the same waste on both and the earlier deadline, claude_b, wins.
    """
    return (
        account(
            "claude",
            window("5h", 0.1, length_s=FIVE_HOURS, resets_in_s=2 * HOUR),
            window("7d", 0.2, resets_in_s=24 * HOUR),
        ),
        account(
            "claude_b",
            window("5h", 0.1, length_s=FIVE_HOURS, resets_in_s=1 * HOUR),
            window("7d", 0.9, resets_in_s=2 * HOUR),
        ),
    )


def _config(env, body: str) -> None:
    xdg = Path(env["XDG_CONFIG_HOME"]) / "quota-router"
    xdg.mkdir(parents=True, exist_ok=True)
    (xdg / "config.toml").write_text(body, encoding="utf-8")


def _forecast(env, *, computed_at_s=NOW, coverage=1.0, rate=0.11) -> None:
    group = forecast.GroupForecast(
        other=forecast.StreamRate(pse_per_h=rate, fable_share=0.0),
        run=forecast.StreamRate(pse_per_h=0.0, fable_share=0.0),
        coverage=coverage,
        accounts=("claude", "claude_b"),
        measured_pse=rate * 168,
        manual_pse_per_h=0.0,
    )
    forecast.write_cache(
        forecast.Forecast(computed_at_s=computed_at_s, window_s=7 * 86400.0,
                          groups={"claude": group}),
        env=env,
    )


def _pick(env, *extra):
    code, out, err = run(
        ["pick", "--dry-run", "--json", *extra], env, snapshots=_scenario(),
        cwd=_NO_CONFIG_FILES,
    )
    assert code == cli.EXIT_OK, err
    return json.loads(out)


def _history(env):
    path = Path(env["XDG_STATE_HOME"]) / "quota-router" / "history.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture()
def env(tmp_path):
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
    }


def test_todays_objective_picks_the_bigger_pile(env) -> None:
    payload = _pick(env)
    assert payload["decision"]["account"] == "claude"
    assert "objective" not in payload


def test_the_joint_objective_picks_the_earlier_deadline_when_both_waste(env) -> None:
    _config(env, '[engine]\nobjective = "joint"\n')
    _forecast(env)
    payload = _pick(env)
    assert payload["decision"]["account"] == "claude_b"
    assert "PSE projected fleet waste avoided" in payload["decision"]["reason"]
    assert payload["objective"]["used"] == "joint"
    assert payload["objective"]["fallback"] is None
    scores = {row["account"]: row["score"] for row in payload["ranked"]}
    # Both avoid the whole job; only the tie-break separates them.
    assert scores["claude"] == pytest.approx(0.003, abs=1e-6)
    assert scores["claude_b"] == pytest.approx(0.003, abs=1e-6)
    assert scores["claude_b"] > scores["claude"]


def test_job_pse_sets_the_job_size(env) -> None:
    _config(env, '[engine]\nobjective = "joint"\n')
    _forecast(env)
    payload = _pick(env, "--job-pse", "0.05")
    assert payload["objective"]["job_pse"] == pytest.approx(0.05)
    assert max(row["score"] for row in payload["ranked"]) == pytest.approx(0.05, abs=1e-6)


@pytest.mark.parametrize(
    "setup, fragment",
    [
        (lambda env: None, "no forecast"),
        (lambda env: _forecast(env, computed_at_s=NOW - 7 * HOUR), "stale"),
        (lambda env: _forecast(env, coverage=0.2), "coverage"),
    ],
)
def test_an_unusable_forecast_falls_back_to_own_waste_and_says_so(env, setup, fragment) -> None:
    _config(env, '[engine]\nobjective = "joint"\n')
    setup(env)
    payload = _pick(env)
    assert payload["decision"]["account"] == "claude"
    assert payload["objective"]["used"] == "own_waste"
    assert fragment in payload["objective"]["fallback"]
    assert any("joint objective unavailable" in w for w in payload["warnings"])


def test_history_records_the_objective_only_when_configured(env) -> None:
    run(["pick", "--json"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    assert "objective" not in _history(env)[-1]
    _config(env, '[engine]\nobjective = "joint"\n')
    _forecast(env)
    # --no-sticky: the first pick seated claude, and within a tie class hysteresis
    # rightly keeps an incumbent whose dwell is not met.
    run(["pick", "--json", "--no-sticky"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    record = _history(env)[-1]
    assert record["objective"]["used"] == "joint"
    assert record["chosen"] == "claude_b"


def test_within_a_tie_the_incumbent_from_todays_objective_keeps_its_seat(env) -> None:
    run(["pick", "--json"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    _config(env, '[engine]\nobjective = "joint"\n')
    _forecast(env)
    run(["pick", "--json"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    assert _history(env)[-1]["chosen"] == "claude"


def test_shadow_mode_decides_by_own_waste_and_records_the_joint_winner(env) -> None:
    _config(env, '[engine]\nshadow = true\n')
    _forecast(env)
    payload = _pick(env)
    assert payload["decision"]["account"] == "claude"
    assert payload["objective"]["used"] == "own_waste"
    assert payload["objective"]["shadow_chosen"] == "claude_b"
    run(["pick", "--json"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    assert _history(env)[-1]["objective"]["shadow_chosen"] == "claude_b"


def test_an_only_subset_is_marked_as_a_partial_fleet(env) -> None:
    _config(env, '[engine]\nobjective = "joint"\n')
    _forecast(env)
    payload = _pick(env, "--only", "claude_b")
    assert "claude_b" == payload["decision"]["account"]
    assert payload["objective"]["partial_fleet"] is True


def test_explain_shows_the_projection(env) -> None:
    _config(env, '[engine]\nobjective = "joint"\n')
    _forecast(env)
    code, out, err = run(["explain"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    text = out + err
    assert "projection" in text
    assert "claude_b" in text and "leftover" in text
    assert "objective: projected fleet waste this job avoids (joint)" in text
    assert "PSE at risk of expiring before reset" not in text
    code, out, err = run(["explain", "--json"], env, snapshots=_scenario(), cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    projection = json.loads(out)["projection"]
    assert projection["objective"]["used"] == "joint"
    leftovers = {row["id"]: row["leftover_pse"] for row in projection["settled"]}
    # 0.11 PSE/h over claude_b's 2h leaves 0.625 - 0.22; claude takes the next 22h.
    assert leftovers["claude_b"] == pytest.approx(0.625 - 0.22, abs=1e-6)
    assert leftovers["claude"] == pytest.approx(5.0 - 0.11 * 22, abs=1e-6)
    assert {row["id"] for row in projection["avoided"]} == {"claude", "claude_b"}


def test_the_library_takes_the_job_size_and_reports_the_objective(env) -> None:
    from quota_router import select_account

    _config(env, '[engine]\nobjective = "joint"\n')
    _forecast(env)
    selection = select_account(
        env=env, now_s=NOW, record=False, cwd=_NO_CONFIG_FILES, job_pse=0.05,
        deps=cli.Deps(load_snapshots=lambda **kw: (list(_scenario()), [])),
    )
    assert selection.account == "claude_b"
    assert selection.to_dict()["objective"]["job_pse"] == pytest.approx(0.05)
    with pytest.raises(ValueError, match="job_pse"):
        select_account(env=env, now_s=NOW, record=False, cwd=_NO_CONFIG_FILES, job_pse=0.0)


def test_a_stale_reading_past_its_reset_is_a_fresh_stock_not_past_waste() -> None:
    """A cached reading can name a weekly reset that has already passed. The window has
    rolled, so the stock is full again with its next deadline; booking its old leftover
    at a deadline in the past would count waste nobody can avoid."""
    from quota_router import joint

    stale = account(
        "claude",
        window("5h", 0.0, length_s=FIVE_HOURS, resets_in_s=-40 * HOUR),
        window("7d", 0.6, resets_in_s=-45 * HOUR),
    )
    stock = joint.subscription_stock(stale, now_s=NOW, capacity=1.0, plan=None, refills=True)
    assert stock.weekly_rem == pytest.approx(6.25)
    assert stock.deadline_s == pytest.approx(NOW - 45 * HOUR + 7 * 86400)
    assert stock.session_rem is None
    ended = joint.subscription_stock(stale, now_s=NOW, capacity=1.0, plan=None, refills=False)
    assert ended is None
