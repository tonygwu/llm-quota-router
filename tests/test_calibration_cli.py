"""CLI evidence writes and status hooks never reach the operator's state."""

from dataclasses import replace
import json

import pytest

from quota_router import calibrations as c, cli, waste
from quota_router.config import AccountConfig, Config
from tests.test_calibrations import T0, T1, T2, belief, encoding, measurement
from tests.test_waste import SEVEN_DAYS, _run, _snapshot


@pytest.fixture
def env(tmp_path):
    return {"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"), "PATH": "/usr/bin:/bin"}


def test_append_stamps_recording_time_and_preserves_evidence_date(tmp_path, env):
    supplied = measurement()
    path = tmp_path / "records.json"
    path.write_text(json.dumps([supplied]))
    code, out, err = _run(["calibrations", "--append", str(path), "--json"], env, [], c.parse_time(T1))
    assert code == cli.EXIT_OK, err
    record, = json.loads(out)["records"]
    assert record["recorded_at"] == T1
    assert record["evidence_at"] == supplied["evidence_at"]
    assert record["reconstructed"] is True
    assert json.loads(out)["reconstruction_complete"] is False


def test_cli_annotation_explains_change_without_rewriting_old_event(tmp_path, env):
    cfg = Config(accounts={"gemini": AccountConfig("gemini", weekly_to_session=4)})
    initial = [encoding(f"seed-{i}", **item) for i, item in enumerate(c.loaded_values(cfg))]
    c.append_records(initial, env=env)
    c.mark_reconstruction_complete(env=env, now_s=c.parse_time(T0), reason="Fixture reconstruction")
    changed = replace(cfg, accounts={"gemini": replace(cfg.accounts["gemini"], weekly_to_session=15)})
    assert len(c.detect_config_changes(changed, now_s=c.parse_time(T1), env=env)) == 1
    original = c.read_records(env=env)[-1]
    annotation = dict(original, id="evidence-added", status="explained",
                      annotates=original["id"], evidence=["docs/experiment.md#result"],
                      reason="Operator supplied the measurement behind this change")
    path = tmp_path / "annotation.json"
    path.write_text(json.dumps(annotation))
    before = c.ledger_path(env).read_bytes()
    code, out, err = _run(["calibrations", "--append", str(path), "--json"], env, [], c.parse_time(T2))
    assert code == cli.EXIT_OK, err
    assert c.ledger_path(env).read_bytes().startswith(before)
    records = json.loads(out)["records"]
    assert records[-2] == original
    assert records[-1]["recorded_at"] == T2
    assert c.detect_config_changes(changed, now_s=c.parse_time(T2), env=env) == []


@pytest.mark.parametrize("raw", ["{not json", "[] trailing", "null", '[{"key":"typo"}]'])
def test_bad_append_is_nonzero_and_keeps_existing_bytes(tmp_path, env, raw):
    c.append_records([measurement()], env=env)
    before = c.ledger_path(env).read_bytes()
    path = tmp_path / "bad.json"
    path.write_text(raw)
    code, _, err = _run(["calibrations", "--append", str(path)], env, [], c.parse_time(T1))
    assert code != cli.EXIT_OK
    assert err
    assert c.ledger_path(env).read_bytes() == before


def test_duplicate_json_fields_are_rejected_before_appending(tmp_path, env):
    c.append_records([measurement()], env=env)
    before = c.ledger_path(env).read_bytes()
    raw = json.dumps(measurement("m2"))
    raw = '{"value": 4.0,' + raw[1:]
    path = tmp_path / "duplicate.json"
    path.write_text(raw)
    code, _, err = _run(["calibrations", "--append", str(path)], env, [], c.parse_time(T1))
    assert code != cli.EXIT_OK
    assert "duplicate" in err
    assert c.ledger_path(env).read_bytes() == before


@pytest.mark.parametrize("cutoff", ["2026-10-19T00:00:00", "2026-10-19T00:00:00-07:00", "nonsense"])
def test_beliefs_as_of_requires_explicit_utc(env, cutoff):
    code, _, err = _run(["waste", "--beliefs-as-of", cutoff, "--json"], env, [], c.parse_time(T1))
    assert code != cli.EXIT_OK
    assert "UTC" in err


def test_as_of_boundary_reproduces_older_belief_without_changing_logs(env):
    c.append_records([measurement(), belief(), belief("b2", 4.0, recorded_at=T1)], env=env)
    row = waste.ResetRecord(account="gemini", window="7d", reset_at_s=c.parse_time(T0),
                            observed=True, remaining_fraction=0.5, wasted_pse=7.5,
                            k=15.0, tier_scale=1.0)
    waste.append_records([row], env=env)
    before = (c.ledger_path(env).read_bytes(), waste.waste_path(env).read_bytes())
    code, out, err = _run(["waste", "--beliefs-as-of", T1, "--json"], env, [], c.parse_time(T2))
    assert code == cli.EXIT_OK, err
    report = json.loads(out)
    assert report["totals"]["wasted_pse"] == 7.5
    assert report["rows"][0]["current"]["ledger_ids"] == ["b1"]
    code, out, err = _run(["waste", "--json"], env, [], c.parse_time(T2))
    assert code == cli.EXIT_OK, err
    assert json.loads(out)["totals"]["wasted_pse"] == 2.0
    code, out, err = _run(["waste", "--beliefs-as-of", T0, "--json"], env, [], c.parse_time(T2))
    assert code == cli.EXIT_OK, err
    assert json.loads(out)["basis_mode"] == "logged_only_no_ledger_records"
    assert (c.ledger_path(env).read_bytes(), waste.waste_path(env).read_bytes()) == before


def test_status_without_ledger_warns_once_without_detecting_encodings(env):
    at = c.parse_time(T0)
    code, out, err = _run(["status", "--json"], env, [_snapshot(0.25, at + SEVEN_DAYS, at)], at)
    assert code == cli.EXIT_OK, err
    warnings = [text for text in json.loads(out)["warnings"] if "calibration reconstruction" in text]
    assert len(warnings) == 1
    assert not c.ledger_path(env).exists()


def test_invalid_ledger_keeps_status_readable_and_stops_reset_write(env):
    reset = c.parse_time(T1)
    before_time, after_time = reset - 60, reset + 60
    code, _, err = _run(["status", "--json"], env, [_snapshot(0.45, reset, before_time)], before_time)
    assert code == cli.EXIT_OK, err
    original = waste.waste_path(env).read_bytes()
    c.ledger_path(env).write_text("{broken ledger}\n")
    code, out, err = _run(["status", "--json"], env,
                         [_snapshot(0.01, reset + SEVEN_DAYS, after_time)], after_time)
    assert code == cli.EXIT_OK, err
    assert any("ledger invalid; waste recording stopped" in text for text in json.loads(out)["warnings"])
    assert waste.waste_path(env).read_bytes() == original
    assert waste.read_records(env=env)[0] == ()
