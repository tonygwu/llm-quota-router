"""Tests for ``[accounts.<id>] plan_ends``: quota that ends when the subscription does.

A cancelled subscription keeps serving until the end of the period already paid for,
and the vendor says nothing about it in advance: its windows keep their usual weekly
boundary. Quota stranded by that end is the same waste as quota stranded at a weekly
reset, so the router should see it the same way. Until ``plan_ends`` every window on the
account counts as ending then; from ``plan_ends`` on, the account is out of routing.

Nothing here reads the operator's machine. Snapshots are built in memory and every config
file lives in the test's temp dir.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from quota_router import launcher
from quota_router.config import ConfigError, load_config
from quota_router.plan_end import apply_plan_ends
from quota_router.types import SOURCE_LIVE, AccountSnapshot, Window, unreadable_reason
from tests.test_cli import NOW, _NO_CONFIG_FILES, pick, run

DAY = 86400.0
HOUR = 3600.0

#: One day after NOW, as an operator would write it.
ENDS_TEXT = datetime.fromtimestamp(NOW + DAY, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
ENDS_S = NOW + DAY


def window(key: str, used: float, length_s: float, resets_in_s: float, **extra) -> Window:
    return Window(
        key=key,
        used_fraction=used,
        length_s=length_s,
        resets_at_s=NOW + resets_in_s,
        observed_at_s=NOW,
        **extra,
    )


def claude(*, five_used: float = 0.1, seven_used: float = 0.5) -> AccountSnapshot:
    """Half its week left, and that week ends in two days."""
    return AccountSnapshot(
        id="claude",
        windows=(
            window("5h", five_used, 5 * HOUR, 3 * HOUR),
            window("7d", seven_used, 7 * DAY, 2 * DAY),
        ),
        tier="max_20x",
        source=SOURCE_LIVE,
    )


def claude_d(*, five_used: float = 0.0, seven_used: float = 0.0) -> AccountSnapshot:
    """A fresh week that the vendor says ends in six days."""
    return AccountSnapshot(
        id="claude_d",
        windows=(
            window("5h", five_used, 5 * HOUR, 4 * HOUR),
            window("7d", seven_used, 7 * DAY, 6 * DAY),
        ),
        tier="max_20x",
        source=SOURCE_LIVE,
    )


@pytest.fixture()
def env(tmp_path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
    }


def write_config(env: dict[str, str], body: str) -> Path:
    path = Path(env["XDG_CONFIG_HOME"]) / "quota-router" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


ENDS_ON_D = f'[accounts.claude_d]\nplan_ends = "{ENDS_TEXT}"\n'
PICK_BOTH = ["pick", "--only", "claude,claude_d", "--dry-run"]


# ======================================================================================
# The pure adjustment
# ======================================================================================


def test_a_window_that_outlives_the_plan_ends_with_it() -> None:
    [ended] = apply_plan_ends([claude_d()], {"claude_d": NOW + 2 * HOUR}, NOW)
    five, seven = ended.windows
    assert seven.resets_at_s == NOW + 2 * HOUR
    assert five.resets_at_s == NOW + 2 * HOUR
    assert ended.available
    # The vendor's reading of how much is used is untouched; only the deadline moves.
    assert [w.used_fraction for w in ended.windows] == [0.0, 0.0]


def test_a_window_that_resets_before_the_plan_ends_is_left_alone() -> None:
    [kept] = apply_plan_ends([claude_d()], {"claude_d": NOW + 5 * HOUR}, NOW)
    five, seven = kept.windows
    assert five.resets_at_s == NOW + 4 * HOUR, "the 5h window rolls over before the end"
    assert seven.resets_at_s == NOW + 5 * HOUR


def test_an_upstream_pacing_baseline_is_dropped_when_the_deadline_moves() -> None:
    """A baseline paced against the old deadline would misstate the shortened window."""
    paced = AccountSnapshot(
        id="claude_d",
        windows=(window("7d", 0.2, 7 * DAY, 6 * DAY, expected_used_fraction=0.15),),
        tier="max_20x",
        source=SOURCE_LIVE,
    )
    [ended] = apply_plan_ends([paced], {"claude_d": ENDS_S}, NOW)
    assert ended.windows[0].expected_used_fraction is None


def test_an_account_without_plan_ends_is_untouched() -> None:
    before = [claude(), claude_d()]
    assert apply_plan_ends(before, {"claude_d": ENDS_S}, NOW)[0] == before[0]
    assert apply_plan_ends(before, {}, NOW) == before


def test_from_the_end_on_the_account_is_unavailable_and_says_why() -> None:
    [ended] = apply_plan_ends([claude_d()], {"claude_d": NOW}, NOW)
    assert not ended.available
    assert "plan ended" in (ended.note or "")
    assert datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%dT%H:%MZ") in ended.note
    # It was read; calling it unreadable would send the operator to fix a login.
    assert ended.windows, "the reading is kept, so status can still show it"
    assert unreadable_reason(ended) is None


# ======================================================================================
# Configuration
# ======================================================================================


def test_plan_ends_is_read_per_account(env, tmp_path) -> None:
    write_config(env, ENDS_ON_D)
    config = load_config(cwd=_NO_CONFIG_FILES, env=env)
    assert config.plan_ends() == {"claude_d": pytest.approx(ENDS_S)}
    assert config.account("claude").plan_ends_s is None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('"2026-10-16T00:00:00"', id="no-zone"),
        pytest.param('"2026-10-16"', id="date-only"),
        pytest.param('"next thursday"', id="not-a-time"),
        pytest.param("20261016", id="not-a-string"),
    ],
)
def test_plan_ends_must_be_an_instant_with_a_zone(env, value) -> None:
    """A wall time with no zone is a different instant on every machine that reads it."""
    write_config(env, f"[accounts.claude_d]\nplan_ends = {value}\n")
    with pytest.raises(ConfigError, match="plan_ends"):
        load_config(cwd=_NO_CONFIG_FILES, env=env)


def test_an_offset_is_accepted_and_means_the_same_instant(env) -> None:
    write_config(env, '[accounts.claude_d]\nplan_ends = "2026-10-15T17:00:00-07:00"\n')
    expected = datetime(2026, 10, 16, tzinfo=timezone.utc).timestamp()
    config = load_config(cwd=_NO_CONFIG_FILES, env=env)
    assert config.plan_ends() == {"claude_d": pytest.approx(expected)}


def test_plan_ends_on_an_account_nothing_reads_is_refused(env) -> None:
    """A mistyped id would leave the real account routed to after its plan ended."""
    write_config(env, f'[accounts.clade_d]\nplan_ends = "{ENDS_TEXT}"\n')
    with pytest.raises(ConfigError, match="plan_ends, but no adapter reads"):
        load_config(cwd=_NO_CONFIG_FILES, env=env)


def test_a_disabled_account_contributes_no_plan_end(env) -> None:
    write_config(env, ENDS_ON_D + "enabled = false\n")
    assert load_config(cwd=_NO_CONFIG_FILES, env=env).plan_ends() == {}


# ======================================================================================
# Routing
# ======================================================================================


def test_without_plan_ends_the_account_whose_week_ends_first_is_spent_first(env) -> None:
    """The control: claude's week ends in two days, claude_d's in six."""
    write_config(env, "")
    payload = pick(PICK_BOTH, env, snapshots=[claude(), claude_d()])
    assert payload["decision"]["account"] == "claude", payload["decision"]


def test_with_plan_ends_the_ending_account_is_spent_first(env) -> None:
    write_config(env, ENDS_ON_D)
    payload = pick(PICK_BOTH, env, snapshots=[claude(), claude_d()])
    assert payload["decision"]["account"] == "claude_d", payload["decision"]


def test_a_full_five_hour_window_still_sends_work_elsewhere(env) -> None:
    """The reason to adjust deadlines rather than pin: the next account takes over."""
    write_config(env, ENDS_ON_D)
    payload = pick(PICK_BOTH, env, snapshots=[claude(), claude_d(five_used=1.0)])
    assert payload["decision"]["account"] == "claude", payload["decision"]


def test_after_the_end_the_account_is_excluded_by_name(env) -> None:
    write_config(env, ENDS_ON_D)
    code, out, _ = run(PICK_BOTH, env, snapshots=[claude(), claude_d()], now_s=ENDS_S + 60)
    assert code == 0
    import json

    payload = json.loads(out)
    assert payload["decision"]["account"] == "claude", payload["decision"]
    reason = next(row["reason"] for row in payload["excluded"] if row["account"] == "claude_d")
    assert "plan ended" in reason, reason


def test_history_records_the_vendor_deadline_not_the_plan_end(env) -> None:
    """The waste series reads history; a moved deadline written there would invent resets."""
    from quota_router import history as history_mod

    write_config(env, ENDS_ON_D)
    code, _, _ = run(["status"], env, snapshots=[claude(), claude_d()])
    assert code == 0
    record = history_mod.latest_record(env=env)
    assert record is not None
    recorded = history_mod.record_to_snapshots(record, source=SOURCE_LIVE, confidence=1.0)
    seven = next(s for s in recorded if s.id == "claude_d").window("7d")
    assert seven.resets_at_s == pytest.approx(NOW + 6 * DAY)


# ======================================================================================
# What the operator sees
# ======================================================================================


def test_status_says_when_the_plan_ends_and_shows_the_moved_deadline(env) -> None:
    write_config(env, ENDS_ON_D)
    code, out, _ = run(["status"], env, snapshots=[claude(), claude_d()])
    assert code == 0
    assert f"plan ends: claude_d at {ENDS_TEXT[:16]}Z" in out, out
    row = next(line for line in out.splitlines() if line.startswith("claude_d "))
    assert "24.0h" in row and "6.0d" not in row, row


def test_status_names_an_ended_plan_and_its_row_reads_unavailable(env) -> None:
    write_config(env, ENDS_ON_D)
    code, out, _ = run(["status"], env, snapshots=[claude(), claude_d()], now_s=ENDS_S + 60)
    assert code == 0
    assert f"plan ended: claude_d at {ENDS_TEXT[:16]}Z" in out, out
    row = next(line for line in out.splitlines() if line.startswith("claude_d "))
    assert row.rstrip().endswith("unavailable"), row


# ======================================================================================
# The launcher's last resort
# ======================================================================================


def test_the_schedule_fallback_skips_an_account_whose_plan_has_ended(env) -> None:
    """With no reading at all, cl may route on weekly schedules; an ended plan is not one."""
    write_config(
        env,
        '[accounts.claude]\nweekly_reset = "Mon 16:00 America/Los_Angeles"\n'
        '[accounts.claude_d]\nweekly_reset = "Tue 11:00 America/Los_Angeles"\n'
        f'plan_ends = "{ENDS_TEXT}"\n',
    )
    config = load_config(cwd=_NO_CONFIG_FILES, env=env)
    assert set(launcher._schedules(config, ["claude", "claude_d"], NOW)) == {"claude", "claude_d"}
    assert set(launcher._schedules(config, ["claude", "claude_d"], ENDS_S)) == {"claude"}
