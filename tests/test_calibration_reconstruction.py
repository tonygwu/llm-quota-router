"""The reconstruction is a proposal; it cannot turn missing evidence into a fact."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def reconstruction():
    path = Path(__file__).resolve().parents[1] / "ops/reconstruct-calibrations.py"
    spec = importlib.util.spec_from_file_location("calibration_reconstruction", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    records, warnings = reconstruction.backup_encodings(tmp_path, "2026-10-20T00:00:00Z")
    assert records == []
    assert len(warnings) == 1
    assert "config.toml.bak-pre-account" in warnings[0]


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
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"generated_at": "2026-10-19T22:29:52Z", "accounts": [
        {"id": "claude", "tier": "max_20x"}]}))
    waste = tmp_path / "waste.jsonl"
    waste.write_text(json.dumps({"v": 1, "account": "claude_d", "tier": "max_20x"}) + "\n")
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
    assert waste.read_text() == json.dumps({"v": 1, "account": "claude_d", "tier": "max_20x"}) + "\n"
    analysis.write_text(json.dumps({"ledger_measurements": measurements[:1]}))
    with pytest.raises(ValueError, match="both Gemini pools"):
        reconstruction.build_proposal(tmp_path, status, "2026-10-19T23:30:00Z", analysis, waste)

