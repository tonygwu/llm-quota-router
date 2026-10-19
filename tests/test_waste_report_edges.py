"""Cross-key belief composition and capacity fallback semantics (BACKLOG #24)."""
import pytest

from quota_router import calibrations, waste


def row(*, size=1.0, source="tier"):
    return waste.ResetRecord(
        account="a", window="7d", reset_at_s=1000, observed=True,
        remaining_fraction=0.5, wasted_pse=3.125 * size, k=6.25,
        tier="max_20x", tier_scale=size,
        basis={"k": 6.25, "tier": "max_20x", "window_size": size,
               "window_size_source": source, "fable_fraction": None,
               "wasted_pse": 3.125 * size, "ledger_ids": None,
               "missing_reasons": {"ledger_ids": "test has no recorded writer ids"}},
    )


def belief(identifier, key, value, *, recorded="2026-10-19T00:00:00Z", candidates=()):
    record = dict(
        version=1, id=identifier, kind="belief", recorded_at=recorded,
        reconstructed=False, evidence_at=None, scope="account", account="a", key=key,
        value=value, valid_from=None, valid_to=None,
        status="unresolved" if candidates else "resolved", candidates=list(candidates),
        reason="Operator decision for the historical window", measurement_ids=[],
        disputed_measurement_ids=[], decided_by="operator",
    )
    calibrations.validate_record(record)
    return record


def test_newer_whole_pool_resolution_supersedes_old_unresolved_k():
    old = belief("old-k", "weekly_to_session", None, candidates=[
        {"label": "k was four", "value": 4},
        {"label": "k was fifteen", "value": 15},
    ])
    new = belief("resolved-pool", "weekly_pool", {"weekly_to_session": 4, "window_size": 2},
                 recorded="2026-10-20T00:00:00Z")
    report = waste.summarize([row()], ledger=[old, new])
    current = report["rows"][0]["current"]
    assert current["status"] == "belief"
    assert current["wasted_pse"] == pytest.approx(4)
    assert report["unresolved_rows"] == 0


def test_tier_belief_without_historical_scale_is_flagged_unrecomputable():
    report = waste.summarize([row()], ledger=[belief("tier-correction", "tier", "max_5x")])
    current = report["rows"][0]["current"]
    assert current["status"] == "logged_unrecomputable"
    assert current["wasted_pse"] == pytest.approx(3.125)
    assert "tier" in current["reason"] and "scale" in current["reason"]


def test_tier_and_historical_scale_beliefs_recompute_fallback():
    report = waste.summarize([row()], ledger=[
        belief("tier-correction", "tier", "max_5x"),
        belief("historical-scale", "tier_scale", 0.25),
    ])
    current = report["rows"][0]["current"]
    assert current["status"] == "belief"
    assert current["values"]["tier"] == "max_5x"
    assert current["values"]["window_size"] == pytest.approx(0.25)
    assert current["wasted_pse"] == pytest.approx(0.5 * 6.25 * 0.25)


def test_tier_scale_belief_does_not_override_explicit_capacity():
    report = waste.summarize([row(size=2, source="capacity")], ledger=[
        belief("tier-scale", "tier_scale", 0.25),
    ])
    current = report["rows"][0]["current"]
    assert current["values"]["window_size"] == 2
    assert current["wasted_pse"] == pytest.approx(0.5 * 6.25 * 2)
    assert current["status"] == "logged"
    assert current["ledger_ids"] == []
    assert current["reason"] == "no belief covers this reset"


def test_weekly_belief_does_not_value_unsupported_unscoped_window():
    from tests.test_waste_v3 import history

    readings = history()
    for index, reading in enumerate(readings):
        windows = reading["accounts"][0]["windows"]
        windows.append(dict(windows[0], key="30d", length_s=2592000,
                            resets_at_s=1000.5 + index * 2592000))
    records = waste.detect_resets(readings).records
    monthly = next(record for record in records if record.window == "30d")
    assert monthly.wasted_pse is None
    report = waste.summarize(records, ledger=[
        belief("weekly-pool", "weekly_pool", {"weekly_to_session": 4, "window_size": 2}),
    ])
    current = next(item["current"] for item in report["rows"] if item["window"] == "30d")
    assert current["wasted_pse"] is None
    assert current["status"] == "logged_unrecomputable"
    assert "30d" in current["reason"]
    assert report["totals"]["wasted_pse"] == pytest.approx((1 - 0.1234567890123) * 8)


def test_capacity_belief_still_beats_later_tier_scale_belief():
    report = waste.summarize([row()], ledger=[
        belief("measured-capacity", "capacity", 2),
        belief("tier-scale", "tier_scale", 0.25, recorded="2026-10-20T00:00:00Z"),
    ])
    current = report["rows"][0]["current"]
    assert current["values"]["window_size"] == 2
    assert current["wasted_pse"] == pytest.approx(0.5 * 6.25 * 2)


def test_new_v3_missed_record_retains_known_window_size_source():
    history = []
    for observed, reset in [(999, 1000), (1210601, 1815400)]:
        history.append({"t": observed, "accounts": [{
            "id": "a", "provider": "claude", "tier": "max_20x", "capacity": 2.0,
            "source": "live", "windows": [{"key": "7d", "used_fraction": 0.1,
                "length_s": 604800, "resets_at_s": reset,
                "observed_at_s": observed, "applies_to": []}],
        }]})
    missed = [r for r in waste.detect_resets(history).records if not r.observed]
    assert len(missed) == 2
    for record in missed:
        assert record.to_dict()["basis"]["window_size_source"] == "capacity"


def measurement(identifier, key, *, account="a", scope="account", value=6.25):
    return dict(version=1, id=identifier, kind="measurement", recorded_at="2026-10-18T00:00:00Z",
                reconstructed=True, evidence_at="1970-01-01T00:00:00Z", scope=scope,
                account=account, key=key, value=value, spread=None,
                started_at="1970-01-01T00:00:00Z", ended_at="1970-01-01T00:01:00Z",
                method="test", evidence=["test measurement"], caveats=[], corrects=[], disputes=[])


def test_measured_k_does_not_hide_unmeasured_tier_fallback():
    measured = measurement("k-measurement", "weekly_to_session")
    current = belief("k-belief", "weekly_to_session", 6.25)
    current["measurement_ids"] = [measured["id"]]
    report = waste.summarize([row()], ledger=[measured, current])
    assert report["unmeasured_default_rows"] == 1
    assert report["rows"][0]["unmeasured_default_components"] == ["window_size"]


@pytest.mark.parametrize("wrong", [
    measurement("unrelated", "weekly_to_session", account="another-account"),
    measurement("unrelated", "capacity", value=1),
    measurement("unrelated", "weekly_to_session", account=None, scope="default"),
    measurement("unrelated", "weekly_to_session", account=None, scope="tier:max_5x"),
])
def test_arbitrary_measurement_reference_does_not_measure_inherited_k(wrong):
    sample = row(source="capacity")
    sample.basis["k_source"] = "code_default"
    current = belief("k-belief", "weekly_to_session", 6.25)
    current["measurement_ids"] = [wrong["id"]]
    report = waste.summarize([sample], ledger=[wrong, current])
    assert report["unmeasured_default_rows"] == 1
    assert "k" in report["rows"][0]["unmeasured_default_components"]


def test_matching_tier_measurement_covers_k_only():
    sample = row()
    sample.basis["k_source"] = "code_default"
    measured = measurement("tier-k", "weekly_to_session", account=None, scope="tier:max_20x")
    current = belief("k-belief", "weekly_to_session", 6.25)
    current["measurement_ids"] = [measured["id"]]
    report = waste.summarize([sample], ledger=[measured, current])
    assert report["rows"][0]["unmeasured_default_components"] == ["window_size"]
    assert report["rows"][0]["assumption_provenance"]["k"]["measurement_ids"] == ["tier-k"]


def test_fable_default_counted_and_unknown_source_separate():
    from dataclasses import replace
    sample = row(source="capacity")
    sample.basis.update(k_source="config", fable_fraction=0.5,
                        fable_fraction_source="code_default")
    sample = replace(sample, scoped=True, window="fable")
    report = waste.summarize([sample])
    assert report["unmeasured_default_rows"] == 1
    assert report["rows"][0]["unmeasured_default_components"] == ["fable_fraction"]
    sample.basis.pop("fable_fraction_source")
    report = waste.summarize([sample])
    assert report["unmeasured_default_rows"] == 0
    assert report["unknown_basis_source_rows"] == 1
    assert report["rows"][0]["unknown_basis_source_components"] == ["fable_fraction"]


def test_unobserved_rows_do_not_count_default_assumptions():
    from dataclasses import replace
    sample = row()
    sample.basis["k_source"] = "code_default"
    sample.basis["wasted_pse"] = None
    report = waste.summarize([replace(sample, observed=False, wasted_pse=None)])
    assert report["unmeasured_default_rows"] == 0
    assert report["unknown_basis_source_rows"] == 0
