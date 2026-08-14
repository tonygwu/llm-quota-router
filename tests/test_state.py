"""Tests for :mod:`quota_router.state`.

The theme of every test here is the same: **state is an optimization, never a
dependency**. A locked, corrupt, unreadable or missing state file must degrade to
"proceed statelessly" and must never raise into, or delay, a routing decision.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from quota_router.state import (
    DEFAULT_LOCK_TIMEOUT_S,
    GLOBAL_SCOPE,
    STATE_VERSION,
    Reservation,
    StateStore,
    StickyEntry,
    state_path,
)

fcntl = pytest.importorskip("fcntl", reason="advisory locking is POSIX-only")

NOW = 1_786_819_612.0


@pytest.fixture()
def store(tmp_path):
    """A store on a throwaway path, with the real (2 second) lock budget."""
    return StateStore(tmp_path / "state.json")


# ======================================================================================
# Location
# ======================================================================================


def test_state_path_prefers_explicit_override(tmp_path):
    target = tmp_path / "somewhere" / "custom.json"
    assert state_path({"QUOTA_ROUTER_STATE": str(target)}) == target


def test_state_path_override_may_name_a_directory(tmp_path):
    target = tmp_path / "dir"
    target.mkdir()
    assert state_path({"QUOTA_ROUTER_STATE": str(target)}) == target / "state.json"


def test_state_path_uses_xdg_state_home(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": "/nonexistent"}
    assert state_path(env) == tmp_path / "quota-router" / "state.json"


def test_state_path_falls_back_to_local_state(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert state_path(env) == tmp_path / ".local" / "state" / "quota-router" / "state.json"


def test_state_path_expands_tilde(tmp_path):
    env = {"HOME": str(tmp_path), "QUOTA_ROUTER_STATE": "~/s.json"}
    assert state_path(env) == tmp_path / "s.json"


# ======================================================================================
# Round trip
# ======================================================================================


def test_missing_file_loads_as_empty_not_an_error(store):
    snapshot = store.load()
    assert snapshot.sticky == {}
    assert snapshot.reservations == ()
    assert snapshot.stateless is False
    assert snapshot.warnings == ()


def test_record_pick_round_trips_sticky_and_reservation(store):
    write = store.record_pick("claude_b", now_s=NOW, cost=0.5, scope="fable")
    assert write.ok is True

    snapshot = store.load()
    assert snapshot.incumbent("fable", NOW + 10, ttl_s=900) == "claude_b"
    assert snapshot.reserved_cost("claude_b", NOW + 10, window_s=60) == pytest.approx(0.5)


def test_scoped_pick_also_refreshes_the_global_incumbent(store):
    store.record_pick("claude_c", now_s=NOW, cost=0.1, scope="fable")
    snapshot = store.load()
    assert snapshot.incumbent(GLOBAL_SCOPE, NOW, ttl_s=900) == "claude_c"


def test_write_is_atomic_and_leaves_no_temporary_files(store, tmp_path):
    store.record_pick("claude", now_s=NOW, cost=0.1)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert json.loads((tmp_path / "state.json").read_text())["version"] == STATE_VERSION


def test_selection_blob_is_persisted_verbatim(store):
    """The selection layer is pure, so its dwell counters have to live here."""
    blob = {"entries": {"claude|claude_b": {"account_id": "claude", "calls": 2}}}
    store.record_pick("claude", now_s=NOW, cost=0.1, selection=blob)
    assert store.load().selection == blob


def test_selection_blob_survives_a_pick_that_does_not_supply_one(store):
    store.record_pick("claude", now_s=NOW, cost=0.1, selection={"entries": {"a": 1}})
    store.record_pick("claude", now_s=NOW + 1, cost=0.1)
    assert store.load().selection == {"entries": {"a": 1}}


# ======================================================================================
# Sticky lifetime
# ======================================================================================


def test_incumbent_expires_after_its_ttl(store):
    store.record_pick("claude_b", now_s=NOW, cost=0.0, scope="opus")
    snapshot = store.load()
    assert snapshot.incumbent("opus", NOW + 899, ttl_s=900) == "claude_b"
    assert snapshot.incumbent("opus", NOW + 901, ttl_s=900) is None


def test_incumbent_is_scoped_per_model_class(store):
    """A Fable call must not inherit an opus incumbent pinned behind a spent window."""
    store.record_pick("claude_b", now_s=NOW, cost=0.0, scope="opus")
    snapshot = store.load()
    assert snapshot.incumbent("fable", NOW, ttl_s=900) is None


def test_expired_sticky_scopes_are_pruned_on_write(store):
    store.record_pick("claude", now_s=NOW, cost=0.0, scope="opus")
    store.record_pick("claude_b", now_s=NOW + 5000, cost=0.0, scope="fable")
    sticky = store.load().sticky
    assert "opus" not in sticky
    assert "fable" in sticky


def test_a_clock_that_moved_backwards_does_not_expire_the_incumbent():
    entry = StickyEntry(account="claude", at_s=NOW + 60, scope=GLOBAL_SCOPE)
    assert entry.is_fresh(NOW, ttl_s=900) is True


# ======================================================================================
# Pileup reservations
# ======================================================================================


def test_reservations_outside_the_window_are_ignored(store):
    store.record_pick("claude", now_s=NOW, cost=1.0)
    snapshot = store.load()
    assert snapshot.reserved_cost("claude", NOW + 30, window_s=60) == pytest.approx(1.0)
    assert snapshot.reserved_cost("claude", NOW + 90, window_s=60) == 0.0


def test_reservations_accumulate_across_concurrent_picks(store):
    """The whole point: three callers in the same second stack up on that account."""
    for offset in range(3):
        store.record_pick("claude_b", now_s=NOW + offset, cost=0.25)
    reserved = store.load().reserved_by_account(NOW + 3, window_s=60)
    assert reserved == {"claude_b": pytest.approx(0.75)}


def test_reservations_are_pruned_on_write(store):
    store.record_pick("claude", now_s=NOW, cost=0.5)
    store.record_pick("claude", now_s=NOW + 10_000, cost=0.5, window_s=60)
    assert len(store.load().reservations) == 1


def test_reservations_are_capped(store):
    for offset in range(10):
        store.record_pick("claude", now_s=NOW + offset, cost=0.1, max_records=4)
    assert len(store.load().reservations) == 4


def test_zero_cost_picks_record_no_reservation(store):
    store.record_pick("claude", now_s=NOW, cost=0.0)
    assert store.load().reservations == ()


def test_reservation_written_slightly_in_the_future_still_counts():
    reservation = Reservation(account="claude", at_s=NOW + 2, cost=0.5)
    assert reservation.is_active(NOW, window_s=60) is True


# ======================================================================================
# Corruption and version skew
# ======================================================================================


def test_corrupt_json_is_a_warning_not_an_exception(store, tmp_path):
    (tmp_path / "state.json").write_text("{not json at all")
    snapshot = store.load()
    assert snapshot.sticky == {}
    assert snapshot.warnings and "corrupt" in snapshot.warnings[0]
    assert snapshot.stateless is False  # empty, but usable -- the next write repairs it


def test_a_corrupt_file_is_repaired_by_the_next_write(store, tmp_path):
    (tmp_path / "state.json").write_text("garbage")
    assert store.record_pick("claude", now_s=NOW, cost=0.1).ok is True
    assert store.load().incumbent(GLOBAL_SCOPE, NOW, ttl_s=900) == "claude"


def test_unknown_schema_version_is_ignored(store, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"version": 999, "sticky": {}}))
    snapshot = store.load()
    assert snapshot.sticky == {}
    assert "version" in snapshot.warnings[0]


def test_non_object_state_is_ignored(store, tmp_path):
    (tmp_path / "state.json").write_text("[1, 2, 3]")
    assert store.load().sticky == {}


def test_malformed_rows_are_dropped_individually(store, tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "sticky": {"*": {"account": "claude", "at_s": NOW}, "bad": {"account": 7}},
                "reservations": [
                    {"account": "claude", "at_s": NOW, "cost": 0.5},
                    {"account": "claude"},
                    "not-a-row",
                ],
            }
        )
    )
    snapshot = store.load()
    assert set(snapshot.sticky) == {"*"}
    assert len(snapshot.reservations) == 1


def test_empty_file_is_not_corrupt(store, tmp_path):
    (tmp_path / "state.json").write_text("")
    assert store.load().warnings == ()


def test_unwritable_location_degrades_to_a_warning(tmp_path):
    """A file where a directory should be: writing must fail soft, not explode."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am not a directory")
    store = StateStore(blocker / "state.json")
    write = store.record_pick("claude", now_s=NOW, cost=0.1)
    assert write.ok is False
    assert write.warnings


# ======================================================================================
# Locking -- the hard 2 second budget
# ======================================================================================


def test_default_lock_timeout_is_two_seconds():
    assert DEFAULT_LOCK_TIMEOUT_S == 2.0
    assert StateStore("/tmp/unused.json").lock_timeout_s == 2.0


def _hold_lock(store):
    """Take the store's advisory lock from an independent file description."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(store.lock_path, "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_load_proceeds_statelessly_when_the_lock_is_held(tmp_path):
    store = StateStore(tmp_path / "state.json", lock_timeout_s=0.05)
    store.record_pick("claude", now_s=NOW, cost=0.5)

    handle = _hold_lock(store)
    try:
        snapshot = store.load()
    finally:
        handle.close()

    assert snapshot.stateless is True
    assert snapshot.incumbent(GLOBAL_SCOPE, NOW, ttl_s=900) is None, (
        "a stateless snapshot must report no incumbent, not the one on disk"
    )
    assert snapshot.reservations == ()
    assert snapshot.warnings and "busy" in snapshot.warnings[0]


def test_write_is_a_no_op_when_the_lock_is_held(tmp_path):
    store = StateStore(tmp_path / "state.json", lock_timeout_s=0.05)
    handle = _hold_lock(store)
    try:
        write = store.record_pick("claude", now_s=NOW, cost=0.5)
    finally:
        handle.close()

    assert write.ok is False
    assert write.stateless is True
    assert not (tmp_path / "state.json").exists()


def test_lock_contention_never_raises_and_stays_inside_its_budget(tmp_path):
    store = StateStore(tmp_path / "state.json", lock_timeout_s=0.2)
    handle = _hold_lock(store)
    try:
        started = time.monotonic()
        snapshot = store.load()
        elapsed = time.monotonic() - started
    finally:
        handle.close()

    assert snapshot.stateless is True
    assert elapsed < 2.0, f"blocked for {elapsed:.2f}s on a 0.2s budget"


def test_the_lock_loop_is_bounded_by_the_deadline_not_by_luck(tmp_path):
    """With a stuck lock, the poll loop must stop the moment the clock passes the budget."""
    ticks = iter([0.0, 0.0, 1.0, 1.5, 2.5, 3.0, 3.5, 4.0])
    sleeps: list[float] = []
    store = StateStore(
        tmp_path / "state.json",
        lock_timeout_s=DEFAULT_LOCK_TIMEOUT_S,
        clock=lambda: next(ticks),
        sleep=sleeps.append,
    )
    handle = _hold_lock(store)
    try:
        snapshot = store.load()
    finally:
        handle.close()

    assert snapshot.stateless is True
    assert len(sleeps) <= 3, f"polled {len(sleeps)} times past a 2s deadline"


def test_lock_is_released_after_a_successful_operation(tmp_path):
    """Two sequential operations must not deadlock on our own lock."""
    store = StateStore(tmp_path / "state.json", lock_timeout_s=0.2)
    assert store.record_pick("claude", now_s=NOW, cost=0.1).ok is True
    assert store.record_pick("claude_b", now_s=NOW + 1, cost=0.1).ok is True
    assert store.load().incumbent(GLOBAL_SCOPE, NOW + 1, ttl_s=900) == "claude_b"


# ======================================================================================
# Disabled store
# ======================================================================================


def test_disabled_store_reads_and_writes_nothing(tmp_path):
    store = StateStore(tmp_path / "state.json", enabled=False)
    assert store.load().stateless is True
    assert store.record_pick("claude", now_s=NOW, cost=1.0).ok is False
    assert not (tmp_path / "state.json").exists()


def test_record_pick_without_an_account_is_rejected_quietly(store):
    write = store.record_pick("", now_s=NOW, cost=1.0)
    assert write.ok is False
    assert write.warnings


def test_clear_removes_the_file_and_tolerates_a_missing_one(store, tmp_path):
    store.record_pick("claude", now_s=NOW, cost=0.1)
    assert store.clear().ok is True
    assert not (tmp_path / "state.json").exists()
    assert store.clear().ok is True


def test_state_snapshot_to_dict_is_json_serializable(store):
    store.record_pick("claude", now_s=NOW, cost=0.25, selection={"entries": {}})
    payload = store.load().to_dict()
    assert json.loads(json.dumps(payload))["reservations"][0]["account"] == "claude"
