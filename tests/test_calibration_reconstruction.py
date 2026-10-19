"""The reconstruction is a proposal; it cannot turn missing evidence into a fact."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def reconstruction():
    path = Path(__file__).resolve().parents[1] / "ops/reconstruct-calibrations.py"
    spec = importlib.util.spec_from_file_location("calibration_reconstruction", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_required_backups(path):
    """The historical states claimed by #24, with no private configuration."""
    pools = ("antigravity_claude", "antigravity_claude_b", "antigravity_gemini", "antigravity_gemini_b")
    values = {account: {} for account in pools}
    values["claude_c"] = {"weekly_to_session": 9.8}
    for when, changes in [
        ("2026-08-24", {}), ("20261012T205017Z", {}),
        ("20261012T235114Z", {"antigravity_claude": {"weekly_to_session": 2.0}}),
        ("20261014T173530Z", {"antigravity_claude_b": {"weekly_to_session": 2.0}}),
        ("20261018T215748Z", {}),
        ("20261018T230000Z", {"antigravity_claude": {"capacity": 0.088}, "antigravity_claude_b": {"capacity": 0.088}}),
        ("20261019T134201Z", {}),
        ("20261019T203300Z", {"antigravity_gemini": {"weekly_to_session": 4.0, "capacity": 1.96},
                              "antigravity_gemini_b": {"weekly_to_session": 4.0, "capacity": 2.24}}),
    ]:
        for account, update in changes.items():
            values[account].update(update)
        content = "\n".join(f"[accounts.{account}]\n" + "\n".join(f"{key}={value}" for key, value in settings.items())
                            for account, settings in values.items())
        (path / ("config.toml.bak-" + when)).write_text(content)


def test_backup_change_keeps_both_timestamp_bounds(reconstruction, tmp_path):
    (tmp_path / "config.toml.bak-20261012T205017Z").write_text(
        '[accounts.antigravity_claude]\nprovider="antigravity"\n'
    )
    (tmp_path / "config.toml.bak-20261012T235114Z").write_text(
        '[accounts.antigravity_claude]\nweekly_to_session=2.0\n'
    )
    records, warnings = reconstruction.backup_encodings(tmp_path, "2026-10-20T00:00:00Z")
    changed = [r for r in records if r["key"] == "weekly_to_session" and r["value"] == 2.0]
    assert len(changed) == 1
    assert changed[0]["effective_from"] == "2026-10-12T20:50:17Z"
    assert changed[0]["effective_to"] == "2026-10-12T23:51:14Z"
    assert "before an edit" in changed[0]["reason"]
    assert warnings == []


def test_unknown_backup_stamp_is_reported_not_guessed(reconstruction, tmp_path):
    (tmp_path / "config.toml.bak-pre-account").write_text('[accounts.sample]\ncapacity=2.0\n')
    evidence = {}
    records, warnings = reconstruction.backup_encodings(tmp_path, "2026-10-20T00:00:00Z", evidence=evidence)
    assert records == []
    assert len(warnings) == 1
    assert "config.toml.bak-pre-account" in warnings[0]
    assert (evidence["attempted"], evidence["succeeded"], evidence["failed"], evidence["excluded"]) == (1, 0, 0, 1)
    assert evidence["inputs"][0]["reason"] == "undated_backup"


def test_gemini_is_unresolved_through_oct16_and_not_assumed_changed_at_reset(reconstruction):
    records = reconstruction.historical_beliefs(
        "2026-10-20T00:00:00Z", {"claude": "max_20x", "codex": "pro"}
    )
    first = next(r for r in records if r["account"] == "antigravity_gemini" and r["status"] == "unresolved")
    assert first["valid_from"] is None
    assert first["valid_to"] > "2026-10-16T22:15:24Z"
    assert first["value"] is None
    assert [r["value"]["weekly_to_session"] for r in first["candidates"]] == [4.0, 15.0]
    assert not [r for r in records if r["account"] == "codex"]
    assert not [r for r in records if r["account"] == "antigravity_claude_b" and r["key"] == "capacity"]


def test_max20_tier_measurement_does_not_claim_other_tiers_were_measured(reconstruction):
    records = reconstruction.historical_beliefs(
        "2026-10-20T00:00:00Z", {"claude": "max_20x", "claude_e": "max_20x", "codex": "pro"}
    )
    measured = [r for r in records if "measurement-max20-k-6.25" in r["measurement_ids"]]
    assert {r["account"] for r in measured} == {"claude", "claude_e"}
    assert all("tier" in r["reason"] and r["valid_from"] is None for r in measured)


def test_scratch_output_rejects_non_scratch_destination(reconstruction, tmp_path):
    with pytest.raises(ValueError, match="temporary"):
        reconstruction.check_scratch_destination(Path.home() / ".local/state/quota-router")
    with pytest.raises(ValueError, match="already exists"):
        reconstruction.check_scratch_destination(tmp_path)


def test_proposal_validates_complete_manifest_and_historical_disabled_tier(reconstruction, tmp_path):
    import json
    from quota_router import calibrations

    (tmp_path / "config.toml").write_text('[accounts.claude]\ntier="max_20x"\n')
    write_required_backups(tmp_path)
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"generated_at": "2026-10-19T22:29:52Z", "accounts": [
        {"id": "claude", "tier": "max_20x"}]}))
    waste = tmp_path / "waste.jsonl"
    waste_rows = [{"v": 1, "account": "claude_d", "tier": "max_20x"},
                  {"v": 2, "account": "antigravity_gemini", "tier": "unknown", "window": "7d", "reset_at_s": 1792188924.5789251},
                  {"v": 2, "account": "antigravity_gemini_b", "tier": "unknown", "window": "7d", "reset_at_s": 1792188388.6607308}]
    waste_content = "".join(json.dumps(row) + "\n" for row in waste_rows)
    waste.write_text(waste_content)
    analysis = tmp_path / "analysis.json"
    measurements = [reconstruction.measurement(
        "measurement-test-" + account, account, "weekly_pool",
        {"weekly_to_session": 15, "window_size": 1.2}, "2026-10-19T23:30:00Z",
        "2026-10-12T23:00:00Z", ["synthetic test"], "Synthetic fine-movement analysis",
        caveats=["Synthetic test does not provide exact run times"])
        for account in ("antigravity_gemini", "antigravity_gemini_b")]
    analysis.write_text(json.dumps({"ledger_measurements": measurements}))
    proposal = reconstruction.build_proposal(tmp_path, status, "2026-10-19T23:30:00Z", analysis, waste)
    assert proposal["complete"]
    assert any(r["id"] == "belief-claude_d-k" for r in proposal["records"])
    env = {"XDG_STATE_HOME": str(tmp_path / "scratch")}
    calibrations.append_records(proposal["records"], env=env)
    assert len(calibrations.read_records(env=env)) == sum(proposal["counts"].values())
    assert waste.read_text() == waste_content
    missing_transition = tmp_path / "config.toml.bak-20261012T235114Z"
    missing_transition.unlink()
    incomplete = reconstruction.build_proposal(tmp_path, status, "2026-10-19T23:30:00Z", analysis, waste)
    assert not incomplete["complete"]
    checks = incomplete["evidence_coverage"]["required_checks"]["inputs"]
    assert next(row for row in checks if row["name"] == "antigravity_claude/weekly_to_session")["outcome"] == "failed"
    assert incomplete["evidence_coverage"]["backups"]["succeeded"] == 7
    for backup in tmp_path.glob("config.toml.bak-*"):
        backup.unlink()
    waste.write_text("")
    incomplete = reconstruction.build_proposal(tmp_path, status, "2026-10-19T23:30:00Z", analysis, waste)
    assert not incomplete["complete"]
    assert incomplete["evidence_coverage"]["backups"]["attempted"] == 0
    assert incomplete["evidence_coverage"]["waste_rows"]["succeeded"] == 0
    assert incomplete["evidence_coverage"]["required_checks"]["failed"] > 0
    assert any("backup" in warning.lower() for warning in incomplete["warnings"])
    analysis.write_text(json.dumps({"ledger_measurements": measurements[:1]}))
    with pytest.raises(ValueError, match="both Gemini pools"):
        reconstruction.build_proposal(tmp_path, status, "2026-10-19T23:30:00Z", analysis, waste)


def test_builtin_table_removal_and_restoration_preserve_both_changes(reconstruction, tmp_path):
    original = '[accounts.claude]\nweekly_to_session=9.8\ncapacity=2.0\n'
    for when, content in [("20261012T205017Z", original), ("20261012T235114Z", "[accounts]\n"),
                          ("20261014T173530Z", original)]:
        (tmp_path / ("config.toml.bak-" + when)).write_text(content)
    rows, _ = reconstruction.backup_encodings(tmp_path, "2026-10-20T00:00:00Z")
    k_rows = [r for r in rows if r["key"] == "weekly_to_session"]
    assert [r["value"] for r in k_rows] == [9.8, 6.25, 9.8]
    assert [r["source"] for r in k_rows] == ["config", "code_default", "config"]
    assert k_rows[1]["effective_from"] == "2026-10-12T20:50:17Z"
    assert k_rows[1]["effective_to"] == "2026-10-12T23:51:14Z"
    assert k_rows[2]["effective_from"] == "2026-10-12T23:51:14Z"
    assert [r["source"] for r in rows if r["key"] == "capacity"] == ["config", "tier_fallback", "config"]


def test_custom_table_removal_is_absence_and_restoration_reencodes(reconstruction, tmp_path):
    original = '[accounts.custom_pool]\nweekly_to_session=9.8\ncapacity=2.0\ntier="pro"\n'
    for when, content in [("20261012T205017Z", original), ("20261012T235114Z", "[accounts]\n"),
                          ("20261014T173530Z", original)]:
        (tmp_path / ("config.toml.bak-" + when)).write_text(content)
    evidence = {}
    rows, _ = reconstruction.backup_encodings(tmp_path, "2026-10-20T00:00:00Z", evidence=evidence)
    absent = [r for r in rows if r["source"] == "account_absent"]
    assert len(absent) == 1
    assert absent[0]["key"] == "capacity" and absent[0]["value"] is None
    assert "weekly_to_session" in absent[0]["reason"] and "9.8" in absent[0]["reason"]
    assert "not a tier fallback" in absent[0]["reason"]
    restored = [r for r in rows if r["effective_from"] == "2026-10-12T23:51:14Z"]
    assert {r["key"] for r in restored} == {"weekly_to_session", "capacity", "tier"}
    assert {r["source"] for r in restored} == {"config"}
    assert evidence["account_events"][0]["event"] == "custom_account_removed"


def test_invalid_backup_is_counted_as_failure(reconstruction, tmp_path):
    (tmp_path / "config.toml.bak-20261012T205017Z").write_text("[broken")
    evidence = {}
    rows, warnings = reconstruction.backup_encodings(tmp_path, "2026-10-20T00:00:00Z", evidence=evidence)
    assert rows == []
    assert (evidence["attempted"], evidence["succeeded"], evidence["failed"], evidence["excluded"]) == (1, 0, 1, 0)
    assert evidence["failed_by_kind"] == {"invalid_config": 1}
    assert "invalid_config" in warnings[0]
