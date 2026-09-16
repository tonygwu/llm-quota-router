"""Tests for the manual-use reserve, ``[accounts.<id>] manual_rate_per_day``.

An account may also serve a person working by hand: a desktop app that cannot switch
accounts, or an interactive session on the default account. Routing that spends such an
account down to zero leaves that person with nothing. The reserve holds back what the
person is expected to use before the weekly reset, and it shrinks as the reset approaches:

    reserve   = manual_rate_per_day x days_to_weekly_reset
    spendable = max(0, remaining - reserve)

``manual_rate_per_day`` is a fraction of the weekly pool per day. There is no built-in
default: an account without the key routes exactly as before.

Nothing here reads the operator's machine. Snapshots are built in memory and every config
file lives in the test's temp dir.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from quota_router.config import ConfigError, load_config
from quota_router.types import SOURCE_LIVE, AccountSnapshot, Window
from tests.test_cli import NOW, pick, run

DAY = 86400.0
HOUR = 3600.0

SECOND_CODEX = '[accounts.codex_b]\nprovider = "codex"\nconfig_dir = "~/.codex-b"\n'
RATE_ON_CODEX = "\n[accounts.codex]\nmanual_rate_per_day = 0.05\n"
RESERVE_LINE = (
    "reserve codex: 72% left - 33% held for manual use (0.05/day x 6.6d to reset) "
    "= 39% spendable"
)


def weekly(used: float, days_to_reset: float, *, key: str = "7d") -> Window:
    return Window(
        key=key,
        used_fraction=used,
        length_s=7 * DAY,
        resets_at_s=NOW + days_to_reset * DAY,
        observed_at_s=NOW,
    )


def codex(used: float = 0.28, days: float = 6.6) -> AccountSnapshot:
    return AccountSnapshot(id="codex", windows=(weekly(used, days),), tier="pro", source=SOURCE_LIVE)


def codex_b(used: float = 0.0, days: float = 7.0) -> AccountSnapshot:
    return AccountSnapshot(id="codex_b", windows=(weekly(used, days),), tier="pro", source=SOURCE_LIVE)


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


# ======================================================================================
# The arithmetic
# ======================================================================================


def test_the_reserve_is_the_rate_times_the_days_to_reset() -> None:
    from quota_router.reserve import manual_reserve

    r = manual_reserve(account_id="codex", remaining=0.72, days_to_reset=6.6, rate_per_day=0.05)
    assert r.reserve == pytest.approx(0.33)
    assert r.spendable == pytest.approx(0.39)


def test_the_reserve_is_zero_at_the_weekly_reset() -> None:
    from quota_router.reserve import manual_reserve

    r = manual_reserve(account_id="codex", remaining=0.72, days_to_reset=0.0, rate_per_day=0.05)
    assert r.reserve == 0.0
    assert r.spendable == pytest.approx(0.72)


def test_spendable_is_never_negative_and_the_reserve_is_reported_uncapped() -> None:
    from quota_router.reserve import manual_reserve

    r = manual_reserve(account_id="codex", remaining=0.72, days_to_reset=6.6, rate_per_day=0.2)
    assert r.spendable == 0.0
    assert r.reserve == pytest.approx(1.32)


def test_only_the_weekly_window_of_an_account_that_sets_a_rate_is_held_back() -> None:
    from quota_router.reserve import apply_manual_reserve

    bucket = Window(
        key="codex_bengalfox", used_fraction=0.1, length_s=5 * HOUR,
        resets_at_s=NOW + HOUR, observed_at_s=NOW,
    )
    with_rate = AccountSnapshot(
        id="codex", windows=(weekly(0.28, 6.6), bucket), tier="pro", source=SOURCE_LIVE
    )
    without = codex_b()

    adjusted, reserves = apply_manual_reserve([with_rate, without], {"codex": 0.05}, NOW)

    assert adjusted[0].window("7d").remaining_fraction == pytest.approx(0.39)
    assert adjusted[0].window("codex_bengalfox") == bucket
    assert adjusted[1] == without
    assert [(r.account_id, round(r.spendable, 6)) for r in reserves] == [("codex", 0.39)]


# ======================================================================================
# Configuration: per account, no default, refuse what cannot be right
# ======================================================================================


def test_manual_rate_per_day_is_read_per_account(env, tmp_path) -> None:
    path = tmp_path / "rate.toml"
    path.write_text("[accounts.codex]\nmanual_rate_per_day = 0.05\n", encoding="utf-8")
    cfg = load_config(env=env, explicit_path=path)
    assert cfg.account("codex").manual_rate_per_day == 0.05
    assert cfg.account("claude").manual_rate_per_day is None


def test_no_account_holds_a_reserve_unless_the_operator_sets_one(env) -> None:
    cfg = load_config(env=env)
    assert {a.id: a.manual_rate_per_day for a in cfg.accounts.values()} == {
        a.id: None for a in cfg.accounts.values()
    }


@pytest.mark.parametrize("value", ["-0.01", "nan", "inf", '"0.05"', "true"])
def test_an_impossible_rate_is_refused(env, tmp_path, value: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(f"[accounts.codex]\nmanual_rate_per_day = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="manual_rate_per_day"):
        load_config(env=env, explicit_path=path)


def test_a_rate_on_an_account_the_router_cannot_read_is_refused(env, tmp_path) -> None:
    """A mistyped id would otherwise create a phantom account and leave the real one unprotected."""
    path = tmp_path / "typo.toml"
    path.write_text("[accounts.codx]\nmanual_rate_per_day = 0.05\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="codx"):
        load_config(env=env, explicit_path=path)


# ======================================================================================
# Routing
# ======================================================================================


def test_with_spendable_below_the_floor_pick_routes_to_the_second_account(env) -> None:
    write_config(env, SECOND_CODEX + RATE_ON_CODEX)
    payload = pick(
        ["pick", "--only", "codex,codex_b", "--min-remaining", "0.5", "--dry-run"],
        env,
        snapshots=[codex(), codex_b()],
    )
    assert payload["decision"]["account"] == "codex_b", payload["decision"]
    reason = next(row["reason"] for row in payload["excluded"] if row["account"] == "codex")
    assert "39" in reason, reason


def test_without_a_reserve_the_same_floor_keeps_the_first_account(env) -> None:
    """The control: the reserve, not the floor alone, is what moved the pick."""
    write_config(env, SECOND_CODEX)
    payload = pick(
        ["pick", "--only", "codex,codex_b", "--min-remaining", "0.5", "--dry-run"],
        env,
        snapshots=[codex(), codex_b()],
    )
    assert payload["decision"]["account"] == "codex", payload["decision"]


def test_routing_still_spends_the_surplus_above_the_reserve(env) -> None:
    """39% spendable clears the default floor, and codex resets first, so it is spent first."""
    write_config(env, SECOND_CODEX + RATE_ON_CODEX)
    payload = pick(
        ["pick", "--only", "codex,codex_b", "--dry-run"], env, snapshots=[codex(), codex_b()]
    )
    assert payload["decision"]["account"] == "codex", payload["decision"]


def test_the_reserve_is_generic_and_holds_on_a_claude_account_too(env) -> None:
    write_config(env, "[accounts.claude]\nmanual_rate_per_day = 0.1\n")
    claude = AccountSnapshot(
        id="claude",
        windows=(
            Window(key="5h", used_fraction=0.1, length_s=5 * HOUR, resets_at_s=NOW + HOUR, observed_at_s=NOW),
            weekly(0.2, 3.0),
        ),
        tier="max_20x",
        source=SOURCE_LIVE,
    )
    code, out, _ = run(["status", "--json"], env, snapshots=[claude])
    assert code == 0
    row = next(a for a in json.loads(out)["accounts"] if a["id"] == "claude")
    # 7d: 0.8 left - 0.1 x 3 days = 0.5, tighter than the 5h window's 0.9.
    assert row["min_remaining"] == pytest.approx(0.5)


# ======================================================================================
# What the operator sees
# ======================================================================================


def test_the_reserve_does_not_forge_the_vendor_reading(env) -> None:
    """``used_fraction`` stays what the vendor said; the hold is its own field.

    The reserve used to be applied by overwriting ``used_fraction`` with ``1 -
    spendable``, because that is the field scoring divides by. It routed correctly and
    published a number the vendor never said: on 2026-09-16 the operator's codex account
    read ``used_fraction = 1.0`` in ``status --json`` while OpenAI reported 0.87, and a
    consumer could not tell "the vendor cut me off" from "my own router held this back".
    Two separate agents reached the wrong conclusion from it in one day.

    So the two facts are separated. ``used_fraction`` is the vendor's reading and
    ``held_fraction`` is the router's own policy. Routing is unchanged: what scoring
    consumes is ``remaining_fraction``, which now nets off both.
    """
    from quota_router.reserve import apply_manual_reserve

    adjusted, _ = apply_manual_reserve([codex()], {"codex": 0.05}, NOW)
    window = adjusted[0].window("7d")

    assert window.used_fraction == pytest.approx(0.28), "the vendor's reading was forged"
    assert window.held_fraction == pytest.approx(0.33), "the hold is not carried"
    # Unchanged from before the split: this is the number routing divides by.
    assert window.remaining_fraction == pytest.approx(0.39)


def test_status_json_reports_the_vendor_reading_and_the_hold_apart(env) -> None:
    """The operator-facing contract, which is where the wrong diagnosis was formed."""
    write_config(env, SECOND_CODEX + RATE_ON_CODEX)
    code, out, _ = run(["status", "--json"], env, snapshots=[codex(), codex_b()])
    assert code == 0
    accounts = {a["id"]: a for a in json.loads(out)["accounts"]}
    weekly_window = next(w for w in accounts["codex"]["windows"] if w["key"] == "7d")

    assert weekly_window["used_fraction"] == pytest.approx(0.28)
    assert weekly_window["held_fraction"] == pytest.approx(0.33)
    # An account with no rate carries a zero hold rather than a missing key, so a
    # consumer can subtract it unconditionally.
    b_weekly = next(w for w in accounts["codex_b"]["windows"] if w["key"] == "7d")
    assert b_weekly["held_fraction"] == 0.0


def test_a_held_window_is_not_scored_as_vendor_exhausted(env) -> None:
    """Routing is byte-identical to the overwrite it replaces.

    ``Stocks`` is fed ``weekly_used``, which before the split was the inflated figure.
    It still is: a held fraction is unavailable to routing in exactly the way spent
    quota is, so the objective must not suddenly see 0.33 of headroom reappear.
    """
    write_config(env, SECOND_CODEX + RATE_ON_CODEX)
    payload = pick(
        ["pick", "--only", "codex,codex_b", "--dry-run"], env, snapshots=[codex(), codex_b()]
    )
    ranked = {r["account"]: r for r in payload["ranked"]}
    assert ranked["codex"]["remaining"] == pytest.approx(0.39)


def test_status_shows_the_reserve_and_the_spendable_amount(env) -> None:
    write_config(env, SECOND_CODEX + RATE_ON_CODEX)
    code, out, _ = run(["status"], env, snapshots=[codex(), codex_b()])
    assert code == 0
    assert re.search(r"codex\s+pro\s+39% 6\.6d", out), out
    assert RESERVE_LINE in out, out


def test_status_json_carries_the_reserve_for_each_account(env) -> None:
    write_config(env, SECOND_CODEX + RATE_ON_CODEX)
    code, out, _ = run(["status", "--json"], env, snapshots=[codex(), codex_b()])
    assert code == 0
    accounts = {a["id"]: a for a in json.loads(out)["accounts"]}
    held = accounts["codex"]["manual_reserve"]
    assert held["rate_per_day"] == 0.05
    assert held["remaining"] == pytest.approx(0.72)
    assert held["reserve"] == pytest.approx(0.33)
    assert held["spendable"] == pytest.approx(0.39)
    assert accounts["codex"]["min_remaining"] == pytest.approx(0.39)
    assert accounts["codex_b"]["manual_reserve"] is None


def test_pick_explain_prints_the_reserve_line(env) -> None:
    write_config(env, SECOND_CODEX + RATE_ON_CODEX)
    code, _, err = run(
        ["pick", "--only", "codex,codex_b", "--dry-run", "--explain"],
        env,
        snapshots=[codex(), codex_b()],
    )
    assert code == 0
    assert RESERVE_LINE in err, err


def test_a_rate_on_an_account_with_no_weekly_window_is_reported(env) -> None:
    write_config(env, SECOND_CODEX + RATE_ON_CODEX)
    no_weekly = AccountSnapshot(
        id="codex",
        windows=(Window(key="codex_bengalfox", used_fraction=0.1, length_s=5 * HOUR,
                        resets_at_s=NOW + HOUR, observed_at_s=NOW),),
        tier="pro",
        source=SOURCE_LIVE,
    )
    code, _, err = run(["status"], env, snapshots=[no_weekly, codex_b()])
    assert code == 0
    assert "codex: manual_rate_per_day is set but the account reports no 7-day window" in err, err


def test_history_records_the_reading_not_the_reserve(env) -> None:
    """Calibration and waste read history; a reserve written into it would be fake usage."""
    from quota_router import history as history_mod

    write_config(env, SECOND_CODEX + RATE_ON_CODEX)
    code, _, _ = run(["status"], env, snapshots=[codex(), codex_b()])
    assert code == 0
    record = history_mod.latest_record(env=env)
    assert record is not None, "status should have written a history record"
    recorded = history_mod.record_to_snapshots(record, source=SOURCE_LIVE, confidence=1.0)
    window = next(s for s in recorded if s.id == "codex").window("7d")
    assert window.used_fraction == pytest.approx(0.28)
