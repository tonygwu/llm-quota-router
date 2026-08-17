"""Tests for :mod:`quota_router.waste` -- the measurement the whole project is judged on.

Every assertion here defends one of three things, and they are not equally interesting:

1. **The series can never be silently shortened.** ``waste.jsonl`` is never rotated,
   never pruned, never rewritten. History pruning trusted a caller-supplied clock and a
   single ad-hoc probe with a synthetic ``--now`` deleted 38 hours of the calibration
   data the project's whole ``k`` measurement rested on. A measurement series that can
   be shortened is worse than none, because it still renders -- it just answers a
   smaller question than the one that was asked.
2. **A reset is never fabricated.** Resets that happened while nobody was looking are
   recorded as unobserved, with no invented remainder. "12 observed, 2 missed" is the
   only honest output; averaging over the hole is the failure this file exists to stop.
3. **The number is in PSE, not in percent.** 55% of a max_20x pool and 55% of a max_5x
   pool are different amounts of lost work, and a fraction alone cannot be summed.

Nothing here may touch the operator's real state directory: every path comes from
``tmp_path`` via an injected environment. A test leaked into the real state dir once,
and that is how the data loss happened.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from quota_router import cli, waste
from quota_router.history import history_path
from quota_router.pse import Plan
from quota_router.state import state_path

FIVE_HOURS = 18_000.0
SEVEN_DAYS = 604_800.0

#: 2026-08-10T00:00:00Z. Every offset below is relative to this.
T0 = 1_786_320_000.0


# ======================================================================================
# Builders
# ======================================================================================


def _iso(epoch_s: float) -> str:
    return (
        datetime.fromtimestamp(epoch_s, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def window(key, used, resets_at_s, observed_at_s, *, length_s=SEVEN_DAYS, applies_to=None):
    return {
        "key": key,
        "used_fraction": used,
        "length_s": length_s,
        "resets_at_s": resets_at_s,
        "observed_at_s": observed_at_s,
        "applies_to": applies_to,
        "expected_used_fraction": None,
    }


def account(
    account_id="claude",
    *windows,
    tier="max_20x",
    capacity=1.0,
    source="live",
):
    return {
        "id": account_id,
        "provider": "claude",
        "windows": list(windows),
        "tier": tier,
        "capacity": capacity,
        "source": source,
        "confidence": 1.0,
        "available": True,
        "note": None,
        "identity": None,
    }


def hrecord(t, *accounts, event="status", chosen=None):
    return {
        "v": 1,
        "t": t,
        "at": _iso(t),
        "event": event,
        "chosen": chosen,
        "model_class": None,
        "multiplier": None,
        "cost": None,
        "regime": None,
        "degraded": False,
        "accounts": list(accounts),
    }


def weekly_reading(
    t, *, used, resets_at_s, account_id="claude", tier="max_20x", capacity=1.0, source="live"
):
    """One poll of one account carrying only its account-wide weekly window."""
    return hrecord(
        t,
        account(
            account_id,
            window("7d", used, resets_at_s, t),
            tier=tier,
            capacity=capacity,
            source=source,
        ),
    )


@pytest.fixture()
def env(tmp_path) -> dict[str, str]:
    """An environment rooted entirely inside the test's temp dir.

    Nothing in this suite may resolve to the operator's real ``~/.local/state``: the
    waste series is append-only precisely because it cannot be reconstructed, and a
    test that writes into it corrupts the measurement it is meant to protect.
    """
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
    }


# ======================================================================================
# Location
# ======================================================================================


def test_waste_path_prefers_explicit_override(tmp_path):
    target = tmp_path / "somewhere" / "custom.jsonl"
    assert waste.waste_path({"QUOTA_ROUTER_WASTE": str(target)}) == target


def test_waste_path_override_may_name_a_directory(tmp_path):
    target = tmp_path / "dir"
    target.mkdir()
    assert waste.waste_path({"QUOTA_ROUTER_WASTE": str(target)}) == target / "waste.jsonl"


def test_waste_path_uses_xdg_state_home(tmp_path):
    resolved = waste.waste_path({"XDG_STATE_HOME": str(tmp_path), "HOME": "/nonexistent"})
    assert resolved == tmp_path / "quota-router" / "waste.jsonl"


def test_waste_path_falls_back_to_local_state(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert waste.waste_path(env) == (
        tmp_path / ".local" / "state" / "quota-router" / "waste.jsonl"
    )


def test_waste_path_expands_tilde(tmp_path):
    env = {"HOME": str(tmp_path), "QUOTA_ROUTER_WASTE": "~/w.jsonl"}
    assert waste.waste_path(env) == tmp_path / "w.jsonl"


def test_waste_lands_in_the_same_directory_as_history_and_state(tmp_path):
    """Three hand-written copies of one resolver; drift between them is the risk.

    The requirement is "same state directory as history.jsonl", and that is only true
    for as long as nobody edits one resolver without the others.
    """
    env = {"HOME": str(tmp_path / "home")}
    assert waste.waste_path(env).parent == history_path(env).parent == state_path(env).parent


# ======================================================================================
# Detecting a reset
# ======================================================================================


def test_a_weekly_reset_is_recorded_with_the_remainder_that_expired(env):
    """The base case: 45% used at the last sighting means 55% of the pool evaporated."""
    reset_at = T0 + SEVEN_DAYS
    records = [
        weekly_reading(reset_at - 3600.0, used=0.40, resets_at_s=reset_at),
        weekly_reading(reset_at - 469.0, used=0.45, resets_at_s=reset_at),
        weekly_reading(reset_at + 600.0, used=0.01, resets_at_s=reset_at + SEVEN_DAYS),
    ]

    detection = waste.detect_resets(records)

    assert [r.account for r in detection.records] == ["claude"]
    row = detection.records[0]
    assert row.window == "7d"
    assert row.observed is True
    assert row.reset_at_s == reset_at
    assert row.remaining_fraction == pytest.approx(0.55)
    # 0.55 of a weekly pool worth 6.25 PSE on a 20x account.
    assert row.wasted_pse == pytest.approx(3.4375)
    assert row.last_observed_at_s == reset_at - 469.0
    assert row.observation_gap_s == pytest.approx(469.0)
    assert row.k == pytest.approx(6.25)
    assert row.tier == "max_20x"


def test_the_same_remainder_on_a_smaller_plan_is_less_lost_work(env):
    """The reason the record carries PSE at all: fractions are not comparable."""
    reset_at = T0 + SEVEN_DAYS
    records = [
        weekly_reading(
            reset_at - 600.0, used=0.45, resets_at_s=reset_at, tier="max_5x", capacity=0.25
        ),
        weekly_reading(
            reset_at + 600.0,
            used=0.0,
            resets_at_s=reset_at + SEVEN_DAYS,
            tier="max_5x",
            capacity=0.25,
        ),
    ]

    row = waste.detect_resets(records).records[0]

    assert row.remaining_fraction == pytest.approx(0.55)
    assert row.wasted_pse == pytest.approx(0.55 * 6.25 * 0.25)


def test_a_per_account_k_is_used_and_recorded_on_the_row(env):
    """``k`` is calibrated per account and will change; a series must not be

    reinterpreted under a later value than the one it was computed with.
    """
    reset_at = T0 + SEVEN_DAYS
    records = [
        weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at),
        weekly_reading(reset_at + 600.0, used=0.0, resets_at_s=reset_at + SEVEN_DAYS),
    ]

    row = waste.detect_resets(
        records, plans={"claude": Plan(weekly_to_session=9.8)}
    ).records[0]

    assert row.k == pytest.approx(9.8)
    assert row.wasted_pse == pytest.approx(0.55 * 9.8)


def test_the_five_hour_window_is_not_counted_as_waste(env):
    """The session window is a flow, not a stock: it is replaced, not lost.

    Recording its rollovers would add ~1,700 rows per account per year of a quantity
    this project explicitly does not call waste, and would inflate any total.
    """
    reset_at = T0 + FIVE_HOURS
    records = [
        hrecord(
            reset_at - 600.0,
            account(
                "claude",
                window("5h", 0.30, reset_at, reset_at - 600.0, length_s=FIVE_HOURS),
                window("7d", 0.20, T0 + SEVEN_DAYS, reset_at - 600.0),
            ),
        ),
        hrecord(
            reset_at + 600.0,
            account(
                "claude",
                window("5h", 0.0, reset_at + FIVE_HOURS, reset_at + 600.0, length_s=FIVE_HOURS),
                window("7d", 0.21, T0 + SEVEN_DAYS, reset_at + 600.0),
            ),
        ),
    ]

    assert waste.detect_resets(records).records == ()


def test_a_scoped_sub_cap_is_recorded_but_coupled_to_the_weekly_pool(env):
    """Fable work spends its own sub-cap *and* the shared weekly pool.

    Modelling it as an independent bucket overstates every account whose weekly pool is
    nearly dry, so the row must never report more than the weekly pool had left.
    """
    reset_at = T0 + SEVEN_DAYS
    before = account(
        "claude",
        window("7d", 0.95, reset_at, reset_at - 600.0),
        window("fable", 0.10, reset_at, reset_at - 600.0, applies_to=["fable"]),
    )
    after = account(
        "claude",
        window("7d", 0.0, reset_at + SEVEN_DAYS, reset_at + 600.0),
        window("fable", 0.0, reset_at + SEVEN_DAYS, reset_at + 600.0, applies_to=["fable"]),
    )
    records = [hrecord(reset_at - 600.0, before), hrecord(reset_at + 600.0, after)]

    rows = {r.window: r for r in waste.detect_resets(records).records}

    assert set(rows) == {"7d", "fable"}
    weekly_left = 0.05 * 6.25
    assert rows["7d"].wasted_pse == pytest.approx(weekly_left)
    # The uncoupled answer would be 0.90 * 0.5 * 6.25 = 2.8125 PSE, which the account
    # could not possibly have delivered: only 0.3125 PSE of weekly pool remained.
    assert rows["fable"].wasted_pse == pytest.approx(weekly_left)
    assert rows["fable"].scoped is True
    assert rows["7d"].scoped is False


def test_a_coupled_scoped_row_says_why_its_two_numbers_disagree(env):
    """On a coupled row ``remaining_fraction`` and ``wasted_pse`` stop agreeing.

    0.90 of the Fable sub-cap is 2.81 PSE by ``fraction x k x tier_scale``, but only
    0.31 PSE was deliverable. A reader opening this file months from now cannot tell
    that from an arithmetic bug unless the row says so, so it does.
    """
    reset_at = T0 + SEVEN_DAYS
    before = account(
        "claude",
        window("7d", 0.95, reset_at, reset_at - 600.0),
        window("fable", 0.10, reset_at, reset_at - 600.0, applies_to=["fable"]),
    )
    after = account(
        "claude",
        window("7d", 0.0, reset_at + SEVEN_DAYS, reset_at + 600.0),
        window("fable", 0.0, reset_at + SEVEN_DAYS, reset_at + 600.0, applies_to=["fable"]),
    )
    rows = {
        r.window: r
        for r in waste.detect_resets(
            [hrecord(reset_at - 600.0, before), hrecord(reset_at + 600.0, after)]
        ).records
    }

    assert "weekly-coupled" in (rows["fable"].reason or "")
    assert rows["fable"].remaining_fraction == pytest.approx(0.90)
    # The account-wide row is not coupled to anything, so it stays silent.
    assert rows["7d"].reason is None


def test_an_uncoupled_scoped_row_carries_no_explanation(env):
    """The note is a flag for a real divergence, not decoration on every scoped row."""
    reset_at = T0 + SEVEN_DAYS
    before = account(
        "claude",
        window("7d", 0.10, reset_at, reset_at - 600.0),
        window("fable", 0.40, reset_at, reset_at - 600.0, applies_to=["fable"]),
    )
    after = account(
        "claude",
        window("7d", 0.0, reset_at + SEVEN_DAYS, reset_at + 600.0),
        window("fable", 0.0, reset_at + SEVEN_DAYS, reset_at + 600.0, applies_to=["fable"]),
    )
    rows = {
        r.window: r
        for r in waste.detect_resets(
            [hrecord(reset_at - 600.0, before), hrecord(reset_at + 600.0, after)]
        ).records
    }

    assert rows["fable"].reason is None
    assert rows["fable"].wasted_pse == pytest.approx(0.60 * 0.5 * 6.25)


# ======================================================================================
# Not fabricating one
# ======================================================================================


def test_resets_that_happened_unobserved_are_recorded_as_missed_not_guessed(env):
    """The machine slept for two weeks; ``resets_at`` came back three windows on.

    The first reset is still measurable -- there is a reading right before it -- but the
    two after it have no reading at all, and no remainder may be invented for them.
    """
    reset_at = T0 + SEVEN_DAYS
    records = [
        weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at),
        weekly_reading(
            reset_at + 3 * SEVEN_DAYS - 600.0,
            used=0.02,
            resets_at_s=reset_at + 3 * SEVEN_DAYS,
        ),
    ]

    rows = waste.detect_resets(records).records

    assert [r.observed for r in rows] == [True, False, False]
    assert [r.reset_at_s for r in rows] == [
        reset_at,
        reset_at + SEVEN_DAYS,
        reset_at + 2 * SEVEN_DAYS,
    ]
    for missed in rows[1:]:
        assert missed.remaining_fraction is None
        assert missed.wasted_pse is None
        assert missed.observation_gap_s is None
        assert "unobserved" in (missed.reason or "")


def test_a_used_fraction_that_drops_without_the_window_moving_is_refused(env):
    """Incoherent data: the same window cannot hold less than it did before.

    Recording a reset here would key it at a reset instant that has not happened yet, so
    the pair is reported and dropped rather than guessed at.
    """
    reset_at = T0 + SEVEN_DAYS
    records = [
        weekly_reading(T0 + 100.0, used=0.45, resets_at_s=reset_at),
        weekly_reading(T0 + 200.0, used=0.10, resets_at_s=reset_at),
    ]

    detection = waste.detect_resets(records)

    assert detection.records == ()
    assert any("incoherent" in w for w in detection.warnings), detection.warnings


def test_a_boundary_that_only_drifts_is_not_a_rollover(env):
    """A replacement window ends a full length after it starts, so a real reset moves

    the boundary by at least one window. Anything smaller is the vendor adjusting a
    boundary, and reading it as a reset would mint a row carrying a remainder that never
    expired.
    """
    reset_at = T0 + SEVEN_DAYS
    records = [
        weekly_reading(T0 + 100.0, used=0.45, resets_at_s=reset_at),
        weekly_reading(T0 + 200.0, used=0.46, resets_at_s=reset_at + 600.0),
    ]

    detection = waste.detect_resets(records)

    assert detection.records == ()
    assert any("without rolling over" in w for w in detection.warnings), detection.warnings


def test_a_sliding_window_produces_no_records_at_all(env):
    """The failure mode that would be silent and enormous.

    If a published window ever slides instead of tiling -- a boundary that advances a
    little on every poll -- the cheap trigger ("moved forward at all") mints a fresh
    reset every fifteen minutes, roughly 35,000 rows a year into a file with no
    retention policy, each carrying a remainder that never expired.
    """
    records = [
        weekly_reading(T0 + i * 900.0, used=0.40 + i * 0.001, resets_at_s=T0 + i * 900.0 + SEVEN_DAYS)
        for i in range(40)
    ]

    detection = waste.detect_resets(records)

    assert detection.records == ()


def test_a_reset_time_that_moves_backwards_is_refused(env):
    reset_at = T0 + SEVEN_DAYS
    records = [
        weekly_reading(T0 + 100.0, used=0.45, resets_at_s=reset_at),
        weekly_reading(T0 + 200.0, used=0.46, resets_at_s=reset_at - 3600.0),
    ]

    detection = waste.detect_resets(records)

    assert detection.records == ()
    assert any("backwards" in w for w in detection.warnings), detection.warnings


def test_a_republished_cache_reading_is_ordered_by_when_it_was_observed(env):
    """The cache re-emits its last reading under a fresh write time on every poll.

    Ordered by the write clock, the republished pre-reset reading sorts *after* the
    post-reset one, and that pair reads as the window jumping backwards -- so the
    detector complains about data that was fine, and the complaint is what a reader
    would go and investigate.
    """
    reset_at = T0 + SEVEN_DAYS
    fresh = weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at)
    post = weekly_reading(reset_at + 600.0, used=0.01, resets_at_s=reset_at + SEVEN_DAYS)
    # Written later, but observed at the old instant: the cache republishing itself.
    stale = weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at, source="cache")
    stale["t"] = reset_at + 900.0

    detection = waste.detect_resets([fresh, post, stale])

    assert len(detection.records) == 1
    assert detection.records[0].reset_at_s == reset_at
    assert detection.warnings == ()


def test_an_unreadable_account_does_not_become_a_blind_reading(env):
    """A failed read is windowless, and windowless means it has no observation time.

    Giving it one -- falling back to the row's write time, which is what the ``k``
    estimator does -- inserts a reading that saw nothing between the two that did. The
    detector pairs neighbours, so the pre-reset sighting is then paired with the blind
    row instead of the post-reset one and the reset becomes undetectable: the account
    with the most quota left is exactly the one that goes dark, so this is the case
    where the measurement would quietly lose its most interesting rows.
    """
    reset_at = T0 + SEVEN_DAYS
    dark = account("claude", tier="max_20x", source="cache")
    dark["available"] = False
    dark["note"] = "access token expired"
    records = [
        weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at),
        hrecord(reset_at - 300.0, dark),
        weekly_reading(reset_at + 600.0, used=0.01, resets_at_s=reset_at + SEVEN_DAYS),
    ]

    rows = waste.detect_resets(records).records

    assert len(rows) == 1
    assert rows[0].remaining_fraction == pytest.approx(0.55)
    assert rows[0].source == "live"
    assert rows[0].last_observed_at_s == reset_at - 600.0


# ======================================================================================
# The file: append-only, idempotent, never shortened
# ======================================================================================


def test_appending_the_same_reset_twice_writes_one_row(env, tmp_path):
    """The reset instant uniquely identifies the occurrence, so a re-run cannot double."""
    path = tmp_path / "waste.jsonl"
    reset_at = T0 + SEVEN_DAYS
    records = [
        weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at),
        weekly_reading(reset_at + 600.0, used=0.01, resets_at_s=reset_at + SEVEN_DAYS),
    ]

    first = waste.update_from_history(records, path=path)
    second = waste.update_from_history(records, path=path)

    assert first.appended == 1
    assert second.appended == 0
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_a_clock_years_in_the_future_deletes_nothing(env, tmp_path):
    """The scar this requirement came from.

    History pruning trusted ``now_s`` absolutely; one probe with a synthetic ``--now``
    put the retention cutoff five months ahead of every file on disk and deleted 38
    hours of calibration data. This series has no retention policy at all, so no clock
    -- injected, synthetic or wrong -- can shorten it.
    """
    path = tmp_path / "waste.jsonl"
    reset_at = T0 + SEVEN_DAYS
    waste.update_from_history(
        [
            weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at),
            weekly_reading(reset_at + 600.0, used=0.01, resets_at_s=reset_at + SEVEN_DAYS),
        ],
        path=path,
    )
    original = path.read_text(encoding="utf-8")

    later = reset_at + 10 * 365 * 86400.0
    waste.update_from_history(
        [
            weekly_reading(later, used=0.10, resets_at_s=later + SEVEN_DAYS),
            weekly_reading(
                later + SEVEN_DAYS + 600.0, used=0.01, resets_at_s=later + 2 * SEVEN_DAYS
            ),
        ],
        path=path,
        now_s=later + 10 * 365 * 86400.0,
    )

    assert path.read_text(encoding="utf-8").startswith(original)
    assert len(waste.read_records(path=path)[0]) == 2


def test_the_writer_never_rotates_the_file(env, tmp_path):
    """No sibling file may appear, however large the series gets."""
    path = tmp_path / "waste.jsonl"
    for index in range(40):
        base = T0 + index * 2 * SEVEN_DAYS
        waste.update_from_history(
            [
                weekly_reading(base + SEVEN_DAYS - 600.0, used=0.45, resets_at_s=base + SEVEN_DAYS),
                weekly_reading(
                    base + SEVEN_DAYS + 600.0, used=0.01, resets_at_s=base + 2 * SEVEN_DAYS
                ),
            ],
            path=path,
        )

    assert sorted(p.name for p in tmp_path.iterdir()) == ["waste.jsonl"]
    assert len(waste.read_records(path=path)[0]) == 40


def test_the_module_contains_no_way_to_delete_or_truncate(env):
    """A structural guard, because the behavioural one only covers paths we thought of.

    ``waste.py`` may open the series for append and for read, and nothing else. If a
    future edit reaches for ``unlink``, ``os.replace``, ``truncate`` or a ``"w"`` mode,
    that is the retention policy this file is not allowed to have, arriving by accident.

    The ban on ``replace`` is deliberately unqualified, which also rules out
    ``str.replace``. An exception list is the part of a guard that gets widened later,
    so ``waste.py`` simply contains no spelling of the name at all.
    """
    import ast
    import pathlib

    source = pathlib.Path(waste.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    banned_attrs = {"unlink", "replace", "rmtree", "truncate", "remove", "rename"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in banned_attrs, (
                f"waste.py calls .{node.attr}(); the series is append-only and may "
                f"never be shortened"
            )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            assert node.value not in {"w", "wb", "w+", "r+"}, (
                "waste.py opens the series for writing; append is the only mode allowed"
            )


def test_a_corrupt_line_does_not_hide_the_good_ones(env, tmp_path):
    """Best-effort reading, like history: a truncated final line is a crash, not a verdict."""
    path = tmp_path / "waste.jsonl"
    reset_at = T0 + SEVEN_DAYS
    waste.update_from_history(
        [
            weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at),
            weekly_reading(reset_at + 600.0, used=0.01, resets_at_s=reset_at + SEVEN_DAYS),
        ],
        path=path,
    )
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"v": 1, "account": "clau')

    rows, warnings = waste.read_records(path=path)

    assert len(rows) == 1
    assert warnings == () or all(isinstance(w, str) for w in warnings)


# ======================================================================================
# Reporting
# ======================================================================================


def test_the_summary_separates_observed_from_missed(env, tmp_path):
    path = tmp_path / "waste.jsonl"
    reset_at = T0 + SEVEN_DAYS
    waste.update_from_history(
        [
            weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at),
            weekly_reading(
                reset_at + 2 * SEVEN_DAYS + 600.0,
                used=0.01,
                resets_at_s=reset_at + 2 * SEVEN_DAYS,
            ),
        ],
        path=path,
    )

    rows, _ = waste.read_records(path=path)
    summary = waste.summarize(rows)

    assert summary["totals"]["resets"] == 2
    assert summary["totals"]["observed"] == 1
    assert summary["totals"]["missed"] == 1
    assert summary["totals"]["wasted_pse"] == pytest.approx(3.4375)


def test_scoped_sub_caps_are_reported_but_never_added_to_the_total(env, tmp_path):
    """Adding them would double-count: a Fable PSE is a weekly PSE, seen twice."""
    path = tmp_path / "waste.jsonl"
    reset_at = T0 + SEVEN_DAYS
    before = account(
        "claude",
        window("7d", 0.45, reset_at, reset_at - 600.0),
        window("fable", 0.90, reset_at, reset_at - 600.0, applies_to=["fable"]),
    )
    after = account(
        "claude",
        window("7d", 0.0, reset_at + SEVEN_DAYS, reset_at + 600.0),
        window("fable", 0.0, reset_at + SEVEN_DAYS, reset_at + 600.0, applies_to=["fable"]),
    )
    waste.update_from_history(
        [hrecord(reset_at - 600.0, before), hrecord(reset_at + 600.0, after)], path=path
    )

    rows, _ = waste.read_records(path=path)
    summary = waste.summarize(rows)

    assert summary["totals"]["wasted_pse"] == pytest.approx(3.4375)
    assert summary["scoped_totals"]["wasted_pse"] == pytest.approx(0.10 * 0.5 * 6.25)


# ======================================================================================
# Where it runs
# ======================================================================================


def _snapshot_window(key, used, resets_at_s, observed_at_s, *, length_s=SEVEN_DAYS):
    from quota_router.types import Window

    return Window(
        key=key,
        used_fraction=used,
        length_s=length_s,
        resets_at_s=resets_at_s,
        observed_at_s=observed_at_s,
    )


def _snapshot(used, resets_at_s, observed_at_s):
    from quota_router.types import AccountSnapshot

    return AccountSnapshot(
        id="claude",
        provider="claude",
        windows=(_snapshot_window("7d", used, resets_at_s, observed_at_s),),
        tier="max_20x",
        source="live",
    )


def _run(argv, env, snapshots, now_s):
    deps = cli.Deps(load_snapshots=lambda **kw: (list(snapshots), []))
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, env=env, stdout=out, stderr=err, now_s=now_s, deps=deps)
    return code, out.getvalue(), err.getvalue()


def test_status_records_a_reset_it_can_see(env, tmp_path):
    """The poller runs ``status``; that is the writer, and it needs no new scheduling."""
    reset_at = T0 + SEVEN_DAYS
    before = reset_at - 600.0
    after = reset_at + 600.0

    _run(["status"], env, [_snapshot(0.45, reset_at, before)], before)
    _run(["status"], env, [_snapshot(0.01, reset_at + SEVEN_DAYS, after)], after)

    rows, _ = waste.read_records(env=env)
    assert [r.reset_at_s for r in rows] == [reset_at]
    assert rows[0].wasted_pse == pytest.approx(3.4375)


def test_pick_does_not_write_the_waste_series(env, tmp_path):
    """``pick`` is latency-critical -- the launcher caps the whole decision at 3 seconds.

    Scanning retained history on that path is exactly the kind of work that turns a
    routing decision into a timeout and a fallback to the default account.
    """
    reset_at = T0 + SEVEN_DAYS
    before = reset_at - 600.0
    after = reset_at + 600.0

    _run(["pick"], env, [_snapshot(0.45, reset_at, before)], before)
    _run(["pick"], env, [_snapshot(0.01, reset_at + SEVEN_DAYS, after)], after)

    assert not waste.waste_path(env).exists()


def test_a_synthetic_now_cannot_change_what_is_recorded(env, tmp_path):
    """``--now`` moves the router's clock; it must not move the measurement.

    The last debugging invocation that fed a synthetic clock into a file-maintenance
    path destroyed 38 hours of data. Everything recorded here comes out of the readings.
    """
    reset_at = T0 + SEVEN_DAYS
    before = reset_at - 600.0
    after = reset_at + 600.0

    _run(["status"], env, [_snapshot(0.45, reset_at, before)], before)
    _run(
        ["status", "--now", _iso(T0 + 400 * 86400.0)],
        env,
        [_snapshot(0.01, reset_at + SEVEN_DAYS, after)],
        after,
    )

    rows, _ = waste.read_records(env=env)
    assert [r.reset_at_s for r in rows] == [reset_at]
    assert rows[0].observation_gap_s == pytest.approx(600.0)


def test_the_waste_command_reports_observed_and_missed(env, tmp_path):
    reset_at = T0 + SEVEN_DAYS
    waste.update_from_history(
        [
            weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at),
            weekly_reading(
                reset_at + 2 * SEVEN_DAYS + 600.0,
                used=0.01,
                resets_at_s=reset_at + 2 * SEVEN_DAYS,
            ),
        ],
        env=env,
    )

    code, out, _ = _run(["waste"], env, [], T0)
    assert code == cli.EXIT_OK
    assert "1 observed" in out and "1 missed" in out

    code, out, _ = _run(["waste", "--json"], env, [], T0)
    payload = json.loads(out)
    assert payload["totals"]["observed"] == 1
    assert payload["totals"]["missed"] == 1
    assert payload["totals"]["wasted_pse"] == pytest.approx(3.4375)


def test_the_waste_command_says_so_when_there_is_nothing_yet(env):
    code, out, _ = _run(["waste"], env, [], T0)
    assert code == cli.EXIT_OK
    assert "no window reset" in out.lower()


def test_backfill_recovers_resets_from_retained_history(env, tmp_path):
    """A gap in the waste series is recoverable for as long as history still holds it."""
    reset_at = T0 + SEVEN_DAYS
    from quota_router import history

    for record in (
        weekly_reading(reset_at - 600.0, used=0.45, resets_at_s=reset_at),
        weekly_reading(reset_at + 600.0, used=0.01, resets_at_s=reset_at + SEVEN_DAYS),
    ):
        path = history_path(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    assert history.iter_records(env=env) is not None
    assert not waste.waste_path(env).exists()

    code, out, _ = _run(["waste", "--backfill"], env, [], reset_at + 1200.0)

    assert code == cli.EXIT_OK
    rows, _ = waste.read_records(env=env)
    assert [r.reset_at_s for r in rows] == [reset_at]
