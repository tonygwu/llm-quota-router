"""Tests for ``quotapick doctor``.

The command exists for failures that produce a well-formed answer naming the
wrong account. An old copy of this package in a consumer's environment does not
reject ``manual_rate_per_day`` -- it ignores it, routes onto the reserved
account, and reports success.

Nothing here runs a real consumer interpreter except the one running the tests,
and that one only to confirm a live probe round-trips. Everything else injects a
fake ``run``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quota_router import doctor
from quota_router.reserve import manual_reserve


# ======================================================================================
# The probe script
# ======================================================================================


def test_probe_substitutes_the_feature_list_and_leaves_no_marker() -> None:
    source = doctor.probe_source(["reserve", "scoring"])
    assert "__FEATURES__" not in source
    assert "('reserve', 'scoring')" in source


def test_probe_survives_a_percent_sign_in_its_own_body() -> None:
    """Regression: the script contains "%s: %s" for its own error formatting.

    Building it with %-formatting raised "not enough arguments for format
    string", which surfaced as `quotapick: unexpected failure` and no report.
    """
    assert "%s: %s" in doctor._PROBE
    doctor.probe_source()  # must not raise


#: The ``src`` directory the tests imported ``quota_router`` from.
_THIS_SRC = Path(doctor.__file__).resolve().parents[1]


@pytest.fixture
def child_imports_this_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a child interpreter import the ``quota_router`` these tests import.

    pytest finds the package through ``pythonpath = ["src"]``, which edits only
    pytest's own ``sys.path``. A child started from ``sys.executable`` does not
    inherit that. In a venv that never installed the package, which is exactly
    the venv CI builds, the probe answered "No module named 'quota_router'". A
    venv made by ``uv sync`` hides the problem with an editable install, so
    these tests passed on every developer machine and failed on every CI run
    from 2026-09-20.
    """
    existing = os.environ.get("PYTHONPATH")
    value = str(_THIS_SRC) if not existing else os.pathsep.join([str(_THIS_SRC), existing])
    monkeypatch.setenv("PYTHONPATH", value)


def test_a_real_interpreter_round_trips_the_probe(child_imports_this_checkout) -> None:
    """One live probe, against the interpreter running the tests.

    A fake `run` can only prove the parsing. This proves the script itself is
    valid Python for the version range consumers actually use.
    """
    report = doctor.inspect_consumer(sys.executable)
    assert report.error is None, report
    assert report.features.get("reserve") is True, report
    # The child must have probed this checkout, not some other installed copy.
    assert Path(report.path).resolve().parent == _THIS_SRC / "quota_router", report


# ======================================================================================
# Probing one environment
# ======================================================================================


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    def run(argv, **kwargs):  # noqa: ANN001, ANN003
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    return run


def test_a_healthy_consumer_is_reported_with_its_version_and_features() -> None:
    payload = json.dumps(
        {"version": "0.1.6", "path": "/x/quota_router/__init__.py", "features": {"reserve": True}}
    )
    report = doctor.inspect_consumer("/x/python", run=_fake_run(stdout=payload))
    assert report.version == "0.1.6"
    assert report.missing == ()


def test_a_stale_consumer_names_the_feature_it_lacks() -> None:
    payload = json.dumps({"version": "0.1.0", "features": {"reserve": False}})
    report = doctor.inspect_consumer("/x/python", run=_fake_run(stdout=payload))
    assert report.missing == ("reserve",)


def test_a_probe_that_prints_nothing_is_an_error_not_a_pass() -> None:
    report = doctor.inspect_consumer(
        "/x/python", run=_fake_run(stdout="", stderr="boom", returncode=1)
    )
    assert report.error is not None
    assert "boom" in report.error


def test_a_probe_that_prints_junk_is_an_error_not_a_pass() -> None:
    report = doctor.inspect_consumer("/x/python", run=_fake_run(stdout="not json"))
    assert report.error is not None
    assert "not JSON" in report.error


def test_only_the_last_stdout_line_is_parsed() -> None:
    """A consumer's sitecustomize or a deprecation notice may print first."""
    noise = "warning: something\n" + json.dumps({"version": "0.1.6", "features": {"reserve": True}})
    report = doctor.inspect_consumer("/x/python", run=_fake_run(stdout=noise))
    assert report.version == "0.1.6"


def test_a_hanging_interpreter_times_out_rather_than_wedging_the_report() -> None:
    def run(argv, **kwargs):  # noqa: ANN001, ANN003
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    report = doctor.inspect_consumer("/x/python", timeout_s=1, run=run)
    assert report.error is not None
    assert "timed out" in report.error


def test_a_missing_interpreter_is_an_error_not_a_crash() -> None:
    def run(argv, **kwargs):  # noqa: ANN001, ANN003
        raise FileNotFoundError("no such file")

    report = doctor.inspect_consumer("/x/python", run=run)
    assert report.error is not None
    assert "FileNotFoundError" in report.error


# ======================================================================================
# Discovery
# ======================================================================================


def test_discovery_does_not_resolve_a_venv_interpreter_symlink(tmp_path: Path) -> None:
    """Regression, 2026-09-20: resolving found the BASE interpreter every time.

    A venv's bin/python is a symlink to the system interpreter. Resolving it
    collapses every environment on the machine onto the same few interpreters,
    none of which can import the consumer's quota_router -- so every stale
    environment was reported as "no module installed", which reads as nothing to
    check. Being invoked THROUGH the venv path is what makes the import work.
    """
    base = tmp_path / "usr" / "bin"
    base.mkdir(parents=True)
    real = base / "python3"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)

    venv_bin = tmp_path / "project" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(real)

    found = doctor.discover_consumer_pythons([tmp_path])
    assert found == [str(venv_bin / "python")], found
    assert str(real) not in found


def test_discovery_finds_nested_venvs_up_to_the_depth_limit(tmp_path: Path) -> None:
    for depth, name in [(0, "here"), (1, "one"), (2, "two")]:
        path = tmp_path.joinpath(*([name] * depth), ".venv", "bin")
        path.mkdir(parents=True)
        (path / "python").write_text("")
    found = doctor.discover_consumer_pythons([tmp_path], max_depth=1)
    assert len(found) == 2, found


def test_discovery_ignores_a_root_that_does_not_exist(tmp_path: Path) -> None:
    assert doctor.discover_consumer_pythons([tmp_path / "gone"]) == []


# ======================================================================================
# Verdicts
# ======================================================================================


def test_an_environment_without_the_package_is_not_a_finding() -> None:
    """Most virtualenvs have no business carrying the router."""
    reports = [
        doctor.ConsumerReport(
            python="/x/python", error="ModuleNotFoundError: No module named 'quota_router'"
        )
    ]
    assert doctor.check_consumers(reports, own_version="0.1.6") == []


def test_a_named_environment_without_the_package_warns_rather_than_vanishing() -> None:
    """--scan finds venvs that never carried the router; --consumer is a claim.

    The operator said this interpreter is a consumer. Dropping it printed "all
    clear" about an environment nothing had checked, which is what a mistyped
    venv path looked like.
    """
    reports = [
        doctor.ConsumerReport(
            python="/x/python", error="ModuleNotFoundError: No module named 'quota_router'"
        )
    ]
    checks = doctor.check_consumers(reports, own_version="0.1.6", named=["/x/python"])
    assert [c.status for c in checks] == ["warn"]
    assert checks[0].name == "consumer[/x/python]"
    assert "cannot import quota_router" in checks[0].detail
    assert "--consumer" in checks[0].remedy


def test_naming_one_environment_does_not_surface_a_scanned_one() -> None:
    missing = "ModuleNotFoundError: No module named 'quota_router'"
    reports = [
        doctor.ConsumerReport(python="/named/python", error=missing),
        doctor.ConsumerReport(python="/scanned/python", error=missing),
    ]
    checks = doctor.check_consumers(reports, own_version="0.1.6", named=["/named/python"])
    assert [c.name for c in checks] == ["consumer[/named/python]"]


def test_an_environment_that_could_not_be_probed_warns_rather_than_passing() -> None:
    reports = [doctor.ConsumerReport(python="/x/python", error="probe timed out after 20s")]
    checks = doctor.check_consumers(reports, own_version="0.1.6")
    assert [c.status for c in checks] == ["warn"]
    assert checks[0].remedy


def test_a_stale_environment_fails_and_the_remedy_says_it_is_ignored_not_rejected() -> None:
    reports = [
        doctor.ConsumerReport(python="/x/python", version="0.1.0", features={"reserve": False})
    ]
    checks = doctor.check_consumers(reports, own_version="0.1.6")
    assert [c.status for c in checks] == ["fail"]
    assert "quota_router.reserve" in checks[0].detail
    assert "ignored" in checks[0].remedy


def test_a_version_differing_from_this_one_is_noted_but_not_a_failure() -> None:
    reports = [
        doctor.ConsumerReport(python="/x/python", version="0.1.5", features={"reserve": True})
    ]
    checks = doctor.check_consumers(reports, own_version="0.1.6")
    assert checks[0].status == "ok"
    assert "0.1.6" in checks[0].detail


def test_no_configured_reserve_is_reported_as_nothing_held_back() -> None:
    checks = doctor.check_reserve_is_enforced(reserves=[], winner="claude")
    assert [c.status for c in checks] == ["ok"]


def test_a_reserve_that_routing_ignored_is_a_failure() -> None:
    """Nothing spendable, and yet that account won. The hold did not apply."""
    held = manual_reserve(account_id="codex", rate_per_day=0.5, remaining=0.2, days_to_reset=3.0)
    assert held.spendable == 0.0
    checks = doctor.check_reserve_is_enforced(reserves=[held], winner="codex")
    assert [c.status for c in checks] == ["fail"]
    assert "still chose it" in checks[0].detail


def test_a_reserve_that_routing_respected_passes() -> None:
    held = manual_reserve(account_id="codex", rate_per_day=0.5, remaining=0.2, days_to_reset=3.0)
    checks = doctor.check_reserve_is_enforced(reserves=[held], winner="codex_b")
    assert [c.status for c in checks] == ["ok"]


def test_an_account_with_headroom_left_may_win_with_a_reserve_in_place() -> None:
    held = manual_reserve(account_id="codex", rate_per_day=0.05, remaining=0.9, days_to_reset=2.0)
    assert held.spendable > 0
    checks = doctor.check_reserve_is_enforced(reserves=[held], winner="codex")
    assert [c.status for c in checks] == ["ok"]


@pytest.mark.parametrize(
    "statuses, expected",
    [
        ([], doctor.EXIT_OK),
        (["ok", "ok"], doctor.EXIT_OK),
        (["ok", "warn"], doctor.EXIT_WARN),
        (["ok", "warn", "fail"], doctor.EXIT_FAIL),
        (["fail"], doctor.EXIT_FAIL),
    ],
)
def test_the_exit_code_is_the_worst_status(statuses, expected) -> None:
    checks = [doctor.Check(f"c{i}", s, "") for i, s in enumerate(statuses)]
    assert doctor.worst_exit_code(checks) == expected


# ======================================================================================
# Rendering
# ======================================================================================


def test_a_clean_report_says_so_without_a_remedy_block() -> None:
    text = doctor.format_report([doctor.Check("config", "ok", "3 accounts")])
    assert "all clear" in text
    assert "  config:" not in text


def test_a_failing_report_gathers_remedies_underneath() -> None:
    text = doctor.format_report(
        [
            doctor.Check("config", "ok", "3 accounts"),
            doctor.Check("consumer[/x]", "fail", "version 0.1.0 is missing", remedy="bump the pin"),
        ]
    )
    assert "1 failed" in text
    assert "consumer[/x]: bump the pin" in text


def test_an_empty_report_is_stated_rather_than_rendered_as_success() -> None:
    assert "no checks ran" in doctor.format_report([])


# ======================================================================================
# The CLI wiring, end to end
# ======================================================================================
#
# Reuses the manual-reserve fixtures: same snapshots, same isolated config root, so
# `doctor` is asked about a machine the test built rather than the operator's.

from tests.test_manual_reserve import (  # noqa: E402
    SECOND_CODEX,
    codex,
    codex_b,
    env,  # noqa: F401 -- a pytest fixture, used by name
    write_config,
)
from tests.test_cli import run  # noqa: E402


def test_doctor_reports_a_reserve_that_routing_honoured(env) -> None:
    write_config(env, SECOND_CODEX + "\n[accounts.codex]\nmanual_rate_per_day = 0.5\n")
    # codex has 0.72 left but holds 0.5/day x 6.6d, so nothing is spendable.
    code, out, err = run(
        ["doctor"], env, snapshots=[codex(used=0.28), codex_b(used=0.0)]
    )
    assert "reserve[codex]" in out, (out, err)
    assert "FAIL" not in out, out
    # Only the local checks ran, so it warns that no consumer was probed.
    assert code == doctor.EXIT_WARN, (code, out)
    assert "none probed" in out


def test_doctor_probes_a_named_consumer_and_exits_on_its_verdict(
    env, child_imports_this_checkout
) -> None:
    write_config(env, SECOND_CODEX)
    code, out, _ = run(
        ["doctor", "--consumer", sys.executable],
        env,
        snapshots=[codex(), codex_b()],
    )
    assert sys.executable in out
    assert code == doctor.EXIT_OK, out


def test_doctor_warns_about_a_named_consumer_that_cannot_import_the_package(
    env, monkeypatch
) -> None:
    write_config(env, SECOND_CODEX)
    monkeypatch.setattr(
        doctor,
        "inspect_consumer",
        lambda python, **kw: doctor.ConsumerReport(
            python=str(python), error="ModuleNotFoundError: No module named 'quota_router'"
        ),
    )
    code, out, _ = run(
        ["doctor", "--consumer", "/x/python"], env, snapshots=[codex(), codex_b()]
    )
    assert "consumer[/x/python]" in out, out
    assert "all clear" not in out, out
    assert code == doctor.EXIT_WARN, (code, out)


def test_doctor_fails_when_a_probed_consumer_is_too_old(env, monkeypatch) -> None:
    write_config(env, SECOND_CODEX)
    monkeypatch.setattr(
        doctor,
        "inspect_consumer",
        lambda python, **kw: doctor.ConsumerReport(
            python=str(python), version="0.1.0", features={"reserve": False}
        ),
    )
    code, out, _ = run(
        ["doctor", "--consumer", "/x/python"], env, snapshots=[codex(), codex_b()]
    )
    assert code == doctor.EXIT_FAIL, out
    assert "FAIL" in out
    assert "quota_router.reserve" in out


def test_doctor_flags_a_configured_rate_that_never_reached_the_decision(env, monkeypatch) -> None:
    """The skew failure from the other side: the key is set and nothing held it.

    An old copy of this package in a CONSUMER is caught by the probe. This catches
    the same thing in the copy doing the routing right now.
    """
    write_config(env, SECOND_CODEX + "\n[accounts.codex]\nmanual_rate_per_day = 0.5\n")
    from quota_router import cli as cli_mod

    monkeypatch.setattr(
        cli_mod.reserve_mod,
        "apply_manual_reserve",
        lambda snapshots, rates, now_s: (tuple(snapshots), ()),
    )
    code, out, _ = run(["doctor"], env, snapshots=[codex(), codex_b()])
    assert code == doctor.EXIT_FAIL, out
    assert "applied no reserve" in out
