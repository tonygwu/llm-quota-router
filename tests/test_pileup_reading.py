"""An in-flight reservation is a hold, not the vendor's reading.

Concurrent callers pile up. This router dispatches a call, and the vendor's usage
endpoint will not show it for a minute or more, so two callers in the same second would
both see the same free account and both land on it. ``_apply_pileup`` subtracts a
reservation per dispatched call to spread them.

It used to do that by adding the cost straight onto ``used_fraction``, which forged the
vendor's number in exactly the way the manual-use reserve used to. Observed on
2026-09-20: ``status`` showed ``codex_b`` at 43% left with its Luna Reserve bucket at
99%, while the live ``account/rateLimits/read`` response said ``usedPercent: 56`` and
``0``. The account note said ``0.0100 reserved by in-flight calls``; the columns did not,
so a transient reservation read as quota the vendor had seen spent.

The reservation now rides in its own ``Window.reserved_fraction``. Routing is unchanged,
because ``remaining_fraction`` nets it off exactly as before.
"""

from __future__ import annotations

import json
import re

import pytest

from quota_router.cli import MAX_PILEUP_FRACTION, _apply_pileup
from tests.test_cli import NOW, account, env, pick, run, window  # noqa: F401 - env is a fixture

#: One call worth 1% of a window, which is the scale the operator saw live.
PILEUP_1PCT = "[pileup]\ncalls_per_window = 100\nwindow_s = 600\n[hysteresis]\nmin_dwell_calls = 0\n"


def two_accounts() -> tuple:
    """Two candidates, because pileup is not applied when there is nowhere to spread."""
    return (
        account("claude", window("seven_day", 0.56, expected_used=0.50)),
        account("claude_b", window("seven_day", 0.60, expected_used=0.50)),
    )


# ======================================================================================
# The split
# ======================================================================================


def test_a_reservation_does_not_forge_the_vendor_reading() -> None:
    """The defect, at the unit that caused it."""
    adjusted = _apply_pileup((account("codex_b", window("seven_day", 0.56)),), {"codex_b": 0.01})
    weekly = adjusted[0].windows[0]

    assert weekly.used_fraction == pytest.approx(0.56), "the vendor's reading was forged"
    assert weekly.reserved_fraction == pytest.approx(0.01)
    # Unchanged from before the split: this is the number routing divides by.
    assert weekly.remaining_fraction == pytest.approx(0.43)


def test_the_ceiling_still_applies_to_the_reservation_not_to_usage() -> None:
    """A wrong per-call cost is capped, and the cap lands on the reservation."""
    adjusted = _apply_pileup((account("claude", window("seven_day", 0.10)),), {"claude": 5.0})
    weekly = adjusted[0].windows[0]

    assert weekly.used_fraction == pytest.approx(0.10)
    assert weekly.reserved_fraction == pytest.approx(MAX_PILEUP_FRACTION)
    assert weekly.remaining_fraction == pytest.approx(0.65)


def test_an_account_with_no_reservation_carries_an_explicit_zero() -> None:
    """A consumer can subtract the field unconditionally, as it can the hold."""
    adjusted = _apply_pileup((account("claude", window("seven_day", 0.10)),), {"other": 0.01})
    assert adjusted[0].windows[0].reserved_fraction == 0.0


# ======================================================================================
# What the operator sees
# ======================================================================================


def test_status_json_reports_the_vendor_reading_and_the_reservation_apart(env, tmp_path) -> None:
    config = tmp_path / "pileup.toml"
    config.write_text(PILEUP_1PCT)
    snapshots = two_accounts()
    pick(["pick", "--config", str(config)], env, snapshots=snapshots)

    code, out, _ = run(["status", "--config", str(config), "--json"], env, snapshots=snapshots)
    assert code == 0
    accounts = {a["id"]: a for a in json.loads(out)["accounts"]}
    weekly = accounts["claude"]["windows"][0]

    assert weekly["used_fraction"] == pytest.approx(0.56), "the vendor's reading was forged"
    assert weekly["reserved_fraction"] == pytest.approx(0.01)
    assert accounts["claude_b"]["windows"][0]["reserved_fraction"] == 0.0


def test_status_weekly_column_shows_capacity_and_the_reservation_gets_a_column(
    env, tmp_path
) -> None:
    """``44%`` capacity, ``1%`` in flight, ``43%`` spendable -- three separate cells."""
    config = tmp_path / "pileup.toml"
    config.write_text(PILEUP_1PCT)
    snapshots = two_accounts()
    pick(["pick", "--config", str(config)], env, snapshots=snapshots)

    code, out, _ = run(["status", "--config", str(config)], env, snapshots=snapshots)
    assert code == 0
    header = next(line for line in out.splitlines() if line.startswith("ACCOUNT"))
    assert "in_flight" in header and "spendable" in header, out
    # 44% capacity, 1% dispatched and not yet visible to the vendor, 43% routable.
    assert re.search(r"claude\s+max_20x\s+44% 60m\s+1%\s+43%", out), out
    # An account with nothing dispatched keeps dashes rather than repeating its window.
    assert re.search(r"claude_b\s+max_20x\s+40% 60m\s+-\s+-", out), out


def test_the_column_is_absent_when_nothing_is_in_flight(env, tmp_path) -> None:
    """A fleet with no dispatched calls keeps exactly the table it had."""
    config = tmp_path / "pileup.toml"
    config.write_text(PILEUP_1PCT)
    code, out, _ = run(["status", "--config", str(config)], env, snapshots=two_accounts())
    assert code == 0
    assert "in_flight" not in out and "spendable" not in out, out


def test_routing_still_subtracts_the_reservation(env, tmp_path) -> None:
    """The split is presentation. Two picks in one second must still land apart."""
    config = tmp_path / "pileup.toml"
    config.write_text(
        "[pileup]\ncalls_per_window = 2\nwindow_s = 600\n[hysteresis]\nmin_dwell_calls = 0\n"
    )
    snapshots = (
        account("claude", window("seven_day", 0.50, expected_used=0.50)),
        account("claude_b", window("seven_day", 0.52, expected_used=0.50)),
    )
    first = pick(["pick", "--config", str(config)], env, snapshots=snapshots)
    second = pick(["pick", "--config", str(config)], env, snapshots=snapshots)
    assert first["decision"]["account"] == "claude"
    assert second["decision"]["account"] == "claude_b"


# ======================================================================================
# Every window, not just the weekly one
# ======================================================================================


def test_the_pse_objective_sees_the_reservation_on_every_window() -> None:
    """``_apply_pileup`` reserves against all of an account's windows, and the objective
    must see all of them.

    ``stocks_from_snapshot`` reads three windows by their raw usage rather than by
    ``remaining_fraction``. While the reservation was written onto ``used_fraction`` it
    arrived in all three for free. Moving it to its own field has to carry it to all
    three by hand, or the 5-hour and model-scoped stocks silently regain the headroom.
    """
    from quota_router.scoring import stocks_from_snapshot
    from tests.test_cli import FIVE_HOURS

    snap = account(
        "claude",
        window("5h", 0.40, length_s=FIVE_HOURS),
        window("7d", 0.56),
        window("fable", 0.30, applies_to={"fable"}),
    )
    # The statement: reserving 10% must cost the objective exactly what the vendor
    # reporting 10% more spent would have. That is what the old overwrite produced.
    as_if_spent = account(
        "claude",
        window("5h", 0.50, length_s=FIVE_HOURS),
        window("7d", 0.66),
        window("fable", 0.40, applies_to={"fable"}),
    )
    reserved = stocks_from_snapshot(_apply_pileup((snap,), {"claude": 0.10})[0], NOW, 1.0)
    spent = stocks_from_snapshot(as_if_spent, NOW, 1.0)
    assert reserved is not None and spent is not None

    for name in ("session_remaining", "weekly_remaining", "fable_remaining"):
        assert getattr(reserved, name) == pytest.approx(getattr(spent, name)), (
            f"{name} lost the reservation"
        )


def test_spendable_is_the_weekly_arithmetic_even_when_another_window_binds() -> None:
    """The row must subtract: weekly capacity minus the deductions beside it.

    ``spendable`` first read the *binding* window, which is chosen by slack rather than
    by what is left. On the live fleet ``codex_b`` binds on its untouched Luna Reserve
    bucket, so the row read ``44% ... 1% in flight ... 99% spendable`` and the three
    numbers did not subtract. The deductions come out of the weekly pool, so the
    remainder shown beside them has to be the weekly pool's.
    """
    from quota_router.explain import format_status_table

    from tests.test_cli import SEVEN_DAYS

    # The live shape: a reserve bucket nothing has touched, so it has a whole window to
    # burn and zero slack, which makes it bind ahead of the weekly pool.
    snap = _apply_pileup(
        (
            account(
                "codex_b",
                window("7d", 0.56, resets_in_s=37.0 * 3600.0),
                window(
                    "base_model_inference",
                    0.0,
                    resets_in_s=SEVEN_DAYS,
                    applies_to={"base_model_inference"},
                ),
                tier="pro",
            ),
        ),
        {"codex_b": 0.01},
    )
    table = format_status_table(snap, NOW)
    binding = table.splitlines()[1]
    # Guard the premise: the reserve bucket really is what TIGHTEST names.
    assert re.search(r"base_model_inference\s+\d+% over pace", binding), binding
    assert re.search(r"codex_b\s+pro\s+44% 37\.0h\s+1%\s+43%", table), table
