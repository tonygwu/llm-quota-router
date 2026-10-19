"""Raw reset provenance must survive history expiry and later calibration changes."""
import json

import pytest

from quota_router import replay, waste
from quota_router.pse import Plan


def history(used=0.1234567890123, *, scoped=False):
    rows = []
    for observed, reset, fraction in [(999.25, 1000.5, used), (1001.75, 605800.5, 0.0)]:
        windows = [{"key": "7d", "used_fraction": fraction, "length_s": 604800,
                    "resets_at_s": reset, "observed_at_s": observed, "applies_to": []}]
        if scoped:
            windows.append({**windows[0], "key": "fable", "used_fraction": 0.1,
                            "applies_to": ["fable"]})
        rows.append({"t": observed, "accounts": [{"id": "a", "provider": "claude",
                     "tier": "max_20x", "capacity": 2.0, "source": "live", "windows": windows}]})
    return rows


def test_v3_preserves_unrounded_readings_and_explicit_basis():
    record, = waste.detect_resets(history()).records
    payload = record.to_dict()
    assert payload["v"] == 3
    assert payload["raw"]["before"]["used_fraction"] == 0.1234567890123
    assert payload["raw"]["before"]["resets_at_s"] == 1000.5
    assert payload["raw"]["before"]["observed_at_s"] == 999.25
    assert payload["raw"]["after"]["observed_at_s"] == 1001.75
    assert payload["basis"]["window_size_source"] == "capacity"
    assert payload["basis"]["window_size"] == 2.0
    assert "k" not in payload and "wasted_pse" not in payload


def test_v3_retains_weekly_reading_for_fable_coupling():
    records = waste.detect_resets(history(0.99, scoped=True)).records
    record = next(row for row in records if row.scoped)
    payload = record.to_dict()
    assert payload["raw"]["weekly_before"]["used_fraction"] == 0.99
    assert payload["basis"]["fable_fraction"] == 0.5


def test_upgrade_copies_earliest_basis_without_rewriting(tmp_path):
    path = tmp_path / "waste.jsonl"
    record, = waste.detect_resets(history()).records
    old = dict(record.to_dict(), v=2, k=12.0, tier="max_20x", tier_scale=1.0,
               wasted_pse=10.518519)
    old.pop("basis", None)
    old.pop("raw", None)
    first_line = json.dumps(old) + "\n"
    path.write_text(first_line)
    newer, = waste.detect_resets(history(), plans={"a": Plan(weekly_to_session=4)}).records
    assert waste.append_records([newer], path=path).appended == 1
    assert path.read_text().startswith(first_line)
    payload = json.loads(path.read_text().splitlines()[1])
    assert payload["basis"]["k"] == 12.0
    assert payload["basis"]["wasted_pse"] == 10.518519
    assert payload["basis"]["window_size_source"] is None
    assert payload["basis"]["ledger_ids"] is None
    assert payload["basis"]["copied_from_version"] == 2
    assert payload["basis"]["missing_reasons"]
    rows, warnings = waste.read_records(path)
    assert not warnings
    assert replay.recorded_waste(rows, accounts=["a"], start_s=0, end_s=2000) == 10.518519
    assert waste.append_records([newer], path=path).appended == 0


def test_v3_replay_reads_logged_basis(tmp_path):
    path = tmp_path / "waste.jsonl"
    record, = waste.detect_resets(history()).records
    waste.append_records([record], path=path)
    raw = json.loads(path.read_text())
    assert "wasted_pse" in raw["basis"]
    records, warnings = waste.read_records(path)
    assert not warnings
    assert replay.recorded_waste(records, accounts=["a"], start_s=0, end_s=2000) == pytest.approx(
        (1 - 0.1234567890123) * 6.25 * 2
    )


def belief(identifier="b1", *, k=4.0, size=2.0, recorded="2026-10-19T00:00:00Z",
           start=None, end=None, status="resolved", candidates=None):
    return {"version": 1, "id": identifier, "kind": "belief", "account": "a",
            "scope": "account", "key": "weekly_pool", "recorded_at": recorded,
            "reconstructed": False, "evidence_at": None,
            "valid_from": start, "valid_to": end, "status": status,
            "value": {"weekly_to_session": k, "window_size": size, "fable_fraction": 0.5}
                     if status == "resolved" else None,
            "candidates": candidates or [], "reason": "test experiment supports this interval",
            "measurement_ids": [], "disputed_measurement_ids": [], "decided_by": "operator"}


def test_no_belief_uses_logged_and_later_belief_keeps_both_figures():
    row, = waste.detect_resets(history()).records
    plain = waste.summarize([row])
    assert plain["rows"][0]["current"]["status"] == "logged"
    revised = waste.summarize([row], ledger=[belief()])
    result = revised["rows"][0]
    assert result["current"]["status"] == "belief"
    assert result["current"]["wasted_pse"] == pytest.approx((1 - 0.1234567890123) * 8)
    assert result["current"]["ledger_ids"] == ["b1"]
    assert result["logged"] == plain["rows"][0]["logged"]


def test_latest_belief_and_as_of_reproduce_prior_report():
    row, = waste.detect_resets(history()).records
    older = belief()
    newer = belief("b2", k=15, recorded="2026-10-20T00:00:00Z")
    latest = waste.summarize([row], ledger=[older, newer])
    assert latest["rows"][0]["current"]["ledger_ids"] == ["b2"]
    past = waste.summarize([row], ledger=[older, newer], beliefs_as_of="2026-10-20T00:00:00Z")
    assert past["rows"][0]["current"]["ledger_ids"] == ["b1"]
    before = waste.summarize([row], ledger=[older], beliefs_as_of="2026-10-18T00:00:00Z")
    assert before["basis_mode"] == "logged_only_no_ledger_records"
    assert before["rows"][0]["current"]["status"] == "logged"


def test_later_provider_change_does_not_revalue_earlier_reset():
    row, = waste.detect_resets(history()).records
    ledger = [belief(k=15, end="1970-01-01T01:00:00Z"),
              belief("b2", start="1970-01-01T01:00:00Z", recorded="2026-10-20T00:00:00Z")]
    report = waste.summarize([row], ledger=ledger)
    assert report["rows"][0]["current"]["values"]["k"] == 15


def test_unresolved_excludes_current_total_but_keeps_candidates_and_logged_total():
    row, = waste.detect_resets(history()).records
    unresolved = belief(status="unresolved", candidates=[
        {"label": "weekly pool unchanged", "value": {"weekly_to_session": 4, "window_size": 2.43}},
        {"label": "session pool unchanged", "value": {"weekly_to_session": 15, "window_size": 2.43}}])
    report = waste.summarize([row], ledger=[unresolved])
    assert report["totals"]["wasted_pse"] == 0
    assert report["logged_totals"]["totals"]["wasted_pse"] > 0
    assert report["unresolved_rows"] == 1
    candidates = report["rows"][0]["current"]["candidates"]
    assert [x["wasted_pse"] for x in candidates] == pytest.approx(
        [(1 - 0.1234567890123) * k * 2.43 for k in (4, 15)])


def test_fable_recomputes_coupled_minimum_with_current_belief():
    rows = waste.detect_resets(history(0.99, scoped=True)).records
    report = waste.summarize(rows, ledger=[belief()])
    fable = next(row for row in report["rows"] if row["scoped"])
    assert fable["current"]["wasted_pse"] == pytest.approx(0.01 * 4 * 2)
    assert report["scoped_totals"]["wasted_pse"] == pytest.approx(0.08)
    assert report["totals"]["wasted_pse"] == pytest.approx(0.08)


def test_legacy_fable_with_missing_raw_coupling_stays_logged_with_reason():
    row = waste.ResetRecord(account="a", window="fable", reset_at_s=1000,
                            observed=True, scoped=True, remaining_fraction=0.9,
                            wasted_pse=0.0625, k=6.25, tier_scale=1.0, version=2)
    report = waste.summarize([row], ledger=[belief()])
    current = report["rows"][0]["current"]
    assert current["status"] == "logged_unrecomputable"
    assert current["wasted_pse"] == 0.0625
    assert "weekly reading" in current["reason"]
    assert report["unrecomputable_rows"] == 1


def test_replay_refuses_a_missing_logged_figure_instead_of_counting_zero():
    row = waste.ResetRecord(account="a", window="7d", reset_at_s=1000,
                            observed=True, wasted_pse=None)
    with pytest.raises(ValueError, match="logged PSE"):
        replay.recorded_waste([row], accounts=["a"], start_s=0, end_s=2000)


@pytest.mark.parametrize("change", ["raw_nan", "raw_missing", "negative_size", "bad_source"])
def test_invalid_v3_data_is_reported_unreadable(tmp_path, change):
    row, = waste.detect_resets(history()).records
    payload = row.to_dict()
    if change == "raw_nan":
        payload["raw"]["before"]["used_fraction"] = float("nan")
    elif change == "raw_missing":
        del payload["raw"]["weekly_before"]
    elif change == "negative_size":
        payload["basis"]["window_size"] = -1
    else:
        payload["basis"]["window_size_source"] = "guess"
    path = tmp_path / "waste.jsonl"
    path.write_text(json.dumps(payload) + "\n")
    records, warnings = waste.read_records(path)
    assert records == ()
    assert len(warnings) == 1 and "1 unreadable" in warnings[0]


def test_append_lock_failure_is_bounded_and_does_not_write(tmp_path, monkeypatch):
    import fcntl
    path = tmp_path / "waste.jsonl"
    path.touch()
    row, = waste.detect_resets(history()).records
    monkeypatch.setattr(waste, "LOCK_TIMEOUT_S", 0.02)
    with path.open("a") as other_writer:
        fcntl.flock(other_writer, fcntl.LOCK_EX)
        result = waste.append_records([row], path=path)
    assert not result.ok and result.appended == 0
    assert "lock timed out" in result.warnings[0]
    assert path.read_bytes() == b""
