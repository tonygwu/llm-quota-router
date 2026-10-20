"""The assumption history is append-only, explicit, and isolated from live state."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest

from quota_router import calibrations as c
from quota_router.config import AccountConfig, Config


T0 = "2026-10-19T00:00:00Z"
T1 = "2026-10-20T00:00:00Z"
T2 = "2026-10-21T00:00:00Z"


def common(id, kind, **kwargs):
    return dict(version=1, id=id, kind=kind, recorded_at=T0,
                reconstructed=True, evidence_at="2026-10-12T00:00:00Z",
                scope="account", account="gemini", key="weekly_to_session", **kwargs)


def measurement(id="m1", value=15.0, **kwargs):
    record = common(id, "measurement", value=value, spread=None,
                    started_at="2026-10-12T20:00:00Z", ended_at="2026-10-12T22:00:00Z",
                    method="token dollars / fine weekly movement", evidence=["doc#section"],
                    caveats=["not settled"], corrects=[], disputes=[])
    record.update(kwargs)
    return record


def belief(id="b1", value=15.0, **kwargs):
    record = common(id, "belief", value=value, valid_from=None, valid_to=None,
                    status="resolved", candidates=[], reason="Evidence covers this interval",
                    measurement_ids=["m1"], disputed_measurement_ids=[], decided_by="operator")
    record.update(kwargs)
    return record


def encoding(id="e1", value=15.0, **kwargs):
    record = common(id, "encoding", value=value, source="config", effective_from=None,
                    effective_to=T0, status="explained", evidence=["config backup"],
                    reason="Reconstructed from backups", annotates=None)
    record.update(kwargs)
    return record


@pytest.fixture
def env(tmp_path):
    return {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}


def test_newest_belief_and_strict_as_of_keep_old_rows(env):
    old = [measurement(), belief()]
    newer = belief("b2", 4.0, recorded_at=T1, valid_from="2026-10-16T22:06:00Z")
    c.append_records(old, env=env)
    original = c.ledger_path(env).read_bytes()
    c.append_records([newer], env=env)
    assert c.ledger_path(env).read_bytes().startswith(original)
    records = c.read_records(env=env)
    assert c.resolve_belief(records, "gemini", "weekly_to_session", c.parse_time(T0))["id"] == "b2"
    assert c.resolve_belief(records, "gemini", "weekly_to_session", c.parse_time("2026-10-15T00:00:00Z"))["id"] == "b1"
    assert c.resolve_belief(c.read_records(env=env, beliefs_as_of=T1), "gemini", "weekly_to_session", c.parse_time(T0))["id"] == "b1"
    assert c.read_records(env=env, beliefs_as_of=T0) == []


def test_correction_retains_experiment_and_does_not_change_belief(env):
    c.append_records([measurement(), belief(), measurement("m2", 4, corrects=["m1"], recorded_at=T1)], env=env)
    records = c.read_records(env=env)
    assert [r["id"] for r in records] == ["m1", "b1", "m2"]
    assert c.resolve_belief(records, "gemini", "weekly_to_session", c.parse_time(T1))["value"] == 15
    with pytest.raises(c.LedgerError, match="same experiment"):
        c.append_records([measurement("m3", corrects=["m1"], started_at=T0, ended_at=T1, recorded_at=T2)], env=env)


def test_unknown_experiment_time_is_preserved_with_caveat(env):
    record = measurement(started_at=None, ended_at=None, caveats=["Experiment clock was not preserved; evidence_at is the commit date"])
    c.append_records([record], env=env)
    assert c.read_records(env=env)[0]["started_at"] is None
    with pytest.raises(c.LedgerError, match="caveat"):
        c.append_records([measurement("m2", started_at=None, ended_at=None, caveats=[])], env=env)


def test_half_open_beliefs_tie_by_append_order_and_never_inherit_default_measurement(env):
    default = measurement(scope="tier:max_20x", account=None)
    c.append_records([default, belief(valid_to=T1), belief("tie", 6.25, valid_to=T1)], env=env)
    records = c.read_records(env=env)
    assert c.resolve_belief(records, "gemini", "weekly_to_session", c.parse_time(T0))["id"] == "tie"
    assert c.resolve_belief(records, "gemini", "weekly_to_session", c.parse_time(T1)) is None
    assert c.resolve_belief(records, "unmeasured", "weekly_to_session", c.parse_time(T0)) is None


def test_unresolved_combined_weekly_candidates_and_scope(env):
    unresolved = belief(key="weekly_pool", status="unresolved", value=None,
                        candidates=[{"label": "weekly unchanged", "value": {"weekly_to_session": 4, "window_size": 2.43}},
                                    {"label": "session unchanged", "value": {"weekly_to_session": 15, "window_size": 2.43}}])
    c.append_records([measurement(), unresolved], env=env)
    assert c.resolve_belief(c.read_records(env=env), "gemini", "weekly_pool", c.parse_time(T0))["status"] == "unresolved"
    assert c.resolve_belief(c.read_records(env=env), "codex", "weekly_pool", c.parse_time(T0)) is None


def test_mixed_weekly_candidate_fields_rejected_before_append(env):
    c.append_records([measurement()], env=env)
    original = c.ledger_path(env).read_bytes()
    unresolved = belief(key="weekly_pool", status="unresolved", value=None, candidates=[
        {"label": "new fraction", "value": {"weekly_to_session": 4, "window_size": 2, "fable_fraction": 0.3}},
        {"label": "retain prior fraction", "value": {"weekly_to_session": 4, "window_size": 2}},
    ])
    with pytest.raises(c.LedgerError, match="same conversion fields"):
        c.append_records([unresolved], env=env)
    assert c.ledger_path(env).read_bytes() == original


@pytest.mark.parametrize("change", [
    {"value": float("nan")}, {"value": True}, {"value": 0.5},
    {"recorded_at": "2026-10-19T00:00:00"}, {"recorded_at": "2026-10-19T00:00:00-07:00"},
    {"reconstructed": True, "evidence_at": None}, {"extra": "typo"},
    {"corrects": ["missing"]}, {"evidence_at": T1}, {"ended_at": T1},
    {"key": []}, {"kind": []},
])
def test_invalid_append_never_changes_bytes(env, change):
    c.append_records([measurement()], env=env)
    before = c.ledger_path(env).read_bytes()
    with pytest.raises(c.LedgerError):
        c.append_records([measurement("bad", **change)], env=env)
    assert c.ledger_path(env).read_bytes() == before


def test_duplicate_ids_idempotent_only_for_identical_records(env):
    assert c.append_records([measurement()], env=env) == ["m1"]
    assert c.append_records([measurement()], env=env) == []
    with pytest.raises(c.LedgerError, match="id"):
        c.append_records([measurement(value=4)], env=env)


def test_corrupt_or_partial_ledger_is_not_silently_skipped(env):
    c.append_records([measurement()], env=env)
    with c.ledger_path(env).open("ab") as stream:
        stream.write(b'{"id": "partial"')
    with pytest.raises(c.LedgerError, match="line 2"):
        c.read_records(env=env)
    with pytest.raises(c.LedgerError):
        c.append_records([measurement("m2")], env=env)


def seed_config(cfg, env):
    records = []
    for i, item in enumerate(c.loaded_values(cfg)):
        records.append(encoding(f"seed-{i}", **item))
    c.append_records(records, env=env)
    c.mark_reconstruction_complete(env=env, now_s=c.parse_time(T0), reason="Fixture reconstruction")


def test_missing_reconstruction_disables_detection_without_creating_ledger(env):
    cfg = Config(accounts={"gemini": AccountConfig("gemini", weekly_to_session=4)})
    warnings = c.detect_config_changes(cfg, now_s=c.parse_time(T1), env=env)
    assert len(warnings) == 1 and "reconstruction" in warnings[0]
    assert not c.ledger_path(env).exists()
    c.append_records([measurement()], env=env)
    assert len(c.detect_config_changes(cfg, now_s=c.parse_time(T1), env=env)) == 1
    assert len(c.read_records(env=env)) == 1


def test_changed_encoding_appends_once_and_warns_until_evidence(env):
    cfg = Config(accounts={"gemini": AccountConfig("gemini", weekly_to_session=4)})
    seed_config(cfg, env)
    changed = replace(cfg, accounts={"gemini": replace(cfg.accounts["gemini"], weekly_to_session=15)})
    assert c.detect_config_changes(cfg, now_s=c.parse_time(T1), env=env) == []
    with ThreadPoolExecutor(max_workers=6) as pool:
        warnings = list(pool.map(lambda _: c.detect_config_changes(changed, now_s=c.parse_time(T1), env=env), range(6)))
    assert all(len(w) == 1 and "unexplained" in w[0] for w in warnings)
    unexplained = [r for r in c.read_records(env=env) if r["kind"] == "encoding" and r["status"] == "unexplained"]
    assert len(unexplained) == 1
    annotation = dict(unexplained[0], id="annotated", status="explained", recorded_at=T2,
                      annotates=unexplained[0]["id"], evidence=["doc#new-measurement"], reason="Applied evidence")
    c.append_records([annotation], env=env)
    assert c.detect_config_changes(changed, now_s=c.parse_time(T2), env=env) == []


def test_equal_numeric_value_changed_source_is_an_encoding_change(env):
    cfg = Config(accounts={"gemini": AccountConfig("gemini")})
    seed_config(cfg, env)
    changed = replace(cfg, accounts={"gemini": replace(cfg.accounts["gemini"], weekly_to_session=6.25)})
    assert len(c.detect_config_changes(changed, now_s=c.parse_time(T1), env=env)) == 1


def test_annotating_superseded_change_does_not_reinstall_its_value(env):
    cfg = Config(accounts={"gemini": AccountConfig("gemini", weekly_to_session=4)})
    seed_config(cfg, env)
    fifteen = replace(cfg, accounts={"gemini": replace(cfg.accounts["gemini"], weekly_to_session=15)})
    c.detect_config_changes(fifteen, now_s=c.parse_time(T1), env=env)
    original = c.read_records(env=env)[-1]
    c.detect_config_changes(cfg, now_s=c.parse_time(T2), env=env)
    current = c.read_records(env=env)[-1]
    annotation = dict(original, id="annotation", recorded_at="2026-10-22T00:00:00Z",
                      status="explained", evidence=["doc#old-change"], reason="Explained old change",
                      annotates=original["id"])
    c.append_records([annotation], env=env)
    before = c.ledger_path(env).read_bytes()
    warnings = c.detect_config_changes(cfg, now_s=c.parse_time("2026-10-23T00:00:00Z"), env=env)
    assert c.ledger_path(env).read_bytes() == before
    assert len(warnings) == 1 and current["id"] in warnings[0]
    assert c.encoding_ids(c.read_records(env=env), "gemini")["weekly_to_session"] == current["id"]


def test_observed_tier_tracks_actual_writer_and_missing_observation_does_not_revert(env):
    cfg = Config(accounts={"claude": AccountConfig("claude")})
    initial = c.loaded_values(cfg, observed_tiers={"claude": "max_20x"})
    rows = [encoding(f"seed-{i}", **item) for i, item in enumerate(initial)]
    c.append_records(rows, env=env)
    c.mark_reconstruction_complete(env=env, now_s=c.parse_time(T0), reason="Fixture")
    assert c.detect_config_changes(cfg, now_s=c.parse_time(T1), env=env) == []
    warnings = c.detect_config_changes(cfg, observed_tiers={"claude": "max_5x"}, now_s=c.parse_time(T1), env=env)
    assert len(warnings) == 2
    values = {r["key"]: r["value"] for r in c.read_records(env=env) if r["id"].startswith("encoding-")}
    assert values == {"tier": "max_5x", "tier_scale": 0.25}


def test_reconstruction_marker_checks_initial_ledger_without_blocking_appends(env):
    c.append_records([measurement()], env=env)
    c.mark_reconstruction_complete(env=env, now_s=c.parse_time(T0), reason="Fixture")
    c.append_records([belief()], env=env)
    assert c.reconstruction_ready(env=env)
    path = c.ledger_path(env)
    path.write_text(path.read_text().replace('"value":15.0', '"value":16.0'))
    with pytest.raises(c.LedgerError, match="reconstruction"):
        c.reconstruction_ready(env=env)
