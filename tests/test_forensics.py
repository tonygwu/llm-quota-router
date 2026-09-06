"""Tests for the credential forensics sampler.

Everything here runs against injected fakes. Nothing reads the real Keychain, the
real process table, or the operator's real config directories -- a test that
touched a live credential would be the exact hazard this package exists to avoid.

Note on literals: this file names the renewal credential field freely. The
credential guard in ``tests/test_no_token_rotation.py`` scans only
``src/quota_router``, and the point of several of these tests is that the sampler
records that field's length without its own source ever naming it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quota_router import forensics
from quota_router.forensics import (
    STATE_BLANK,
    STATE_EXPIRED,
    STATE_HEALTHY,
    STATE_MISSING,
    STATE_UNREADABLE,
    CredentialSample,
    Sample,
    classify_entry,
    detect_transitions,
    list_holders,
    read_credential,
    run_once,
)
from quota_router.providers.claude_oauth import keychain_service_for

NOW = 1_760_000_000.0
ISO = "2025-10-09T09:33:20+00:00"


def _blob(access: str = "a" * 108, refresh: str = "r" * 108, expires_ms: float | None = None) -> str:
    oauth: dict[str, object] = {"accessToken": access, "refreshToken": refresh}
    if expires_ms is not None:
        oauth["expiresAt"] = expires_ms
    oauth["subscriptionType"] = "max"
    return json.dumps({"claudeAiOauth": oauth})


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr="")


# ======================================================================================
# classify_entry
# ======================================================================================


def test_healthy_entry_classifies_healthy() -> None:
    raw = _blob(expires_ms=(NOW + 3600) * 1000)
    assert classify_entry(raw, now_s=NOW, expires_at_s=NOW + 3600) == STATE_HEALTHY


def test_blank_access_token_is_the_rotation_casualty_signature() -> None:
    """The observed outage shape: entry present, access token zeroed in place."""
    raw = _blob(access="")
    assert classify_entry(raw, now_s=NOW, expires_at_s=None) == STATE_BLANK


def test_expired_is_not_blank() -> None:
    """An eight-hour token reaching its end is normal and must not read as a loss."""
    raw = _blob(expires_ms=(NOW - 1) * 1000)
    assert classify_entry(raw, now_s=NOW, expires_at_s=NOW - 1) == STATE_EXPIRED
    assert STATE_EXPIRED not in forensics.LOST_STATES


def test_absent_and_empty_payloads_are_missing() -> None:
    assert classify_entry(None, now_s=NOW, expires_at_s=None) == STATE_MISSING
    assert classify_entry("   ", now_s=NOW, expires_at_s=None) == STATE_MISSING


def test_non_json_and_wrong_shape_are_unreadable() -> None:
    assert classify_entry("not json", now_s=NOW, expires_at_s=None) == STATE_UNREADABLE
    assert classify_entry("[]", now_s=NOW, expires_at_s=None) == STATE_UNREADABLE
    assert classify_entry(json.dumps({"other": {}}), now_s=NOW, expires_at_s=None) == STATE_UNREADABLE


# ======================================================================================
# Generic field measurement
# ======================================================================================


def test_field_lengths_capture_every_string_including_the_unnamed_one() -> None:
    """The module never names these fields; it must still measure both."""
    lengths = forensics._field_lengths(_blob(access="x" * 10, refresh="y" * 20))
    assert lengths["accessToken"] == 10
    assert lengths["refreshToken"] == 20
    assert lengths["subscriptionType"] == 3


def test_field_lengths_distinguish_the_two_loss_shapes() -> None:
    """Access blanked with renewal intact is a different failure from both gone."""
    partial = forensics._field_lengths(_blob(access="", refresh="r" * 108))
    both_gone = forensics._field_lengths(_blob(access="", refresh=""))
    assert partial["accessToken"] == 0 and partial["refreshToken"] == 108
    assert both_gone["accessToken"] == 0 and both_gone["refreshToken"] == 0
    assert partial != both_gone


def test_sampler_source_never_names_the_renewal_field() -> None:
    """Guard the guard: the generic measurement must not regress into a literal."""
    source = Path(forensics.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    # Strip docstrings, which are allowed to discuss the hazard by name.
    without_docs = code.split('"""')
    executable = "".join(without_docs[::2])
    assert "refreshToken" not in executable


# ======================================================================================
# read_credential
# ======================================================================================


def _keychain_runner(payloads: dict[str, str | None]):
    """Fake ``security`` that answers per service name."""

    def runner(argv, **kwargs):
        service = argv[argv.index("-s") + 1]
        if service not in payloads:
            return _completed(returncode=44)
        value = payloads[service]
        if value is None:
            return _completed(returncode=44)
        return _completed(stdout=value)

    return runner


def test_read_credential_reports_blank_with_field_lengths() -> None:
    # The service name is the SHA-256 of the ABSOLUTE config-dir path, so hardcoding
    # one pins the test to whoever's home directory produced it. Derive it from the
    # same function the code uses, against an explicit home.
    home = Path("/Users/somebody")
    service = keychain_service_for("~/.claude-b", home=home)[0]
    runner = _keychain_runner({service: _blob(access="")})
    sample = read_credential("claude_b", "~/.claude-b", now_s=NOW, runner=runner, home=home)
    assert sample.state == STATE_BLANK
    assert sample.lost is True
    assert sample.field_lengths["accessToken"] == 0
    assert sample.field_lengths["refreshToken"] == 108


def test_read_credential_reports_missing_when_no_entry_exists() -> None:
    sample = read_credential("claude_b", "~/.claude-b", now_s=NOW, runner=_keychain_runner({}))
    assert sample.state == STATE_MISSING
    assert sample.service is None


def test_a_failed_security_call_is_unreadable_not_missing() -> None:
    """A Keychain that would not answer must never be reported as logged out."""

    def runner(argv, **kwargs):
        raise FileNotFoundError("security")

    sample = read_credential("claude_b", "~/.claude-b", now_s=NOW, runner=runner)
    assert sample.state == STATE_UNREADABLE
    assert sample.problem is not None


# ======================================================================================
# list_holders
# ======================================================================================


def _ps_runner(table: str, envs: dict[int, str]):
    def runner(argv, **kwargs):
        if argv[:2] == ["ps", "-Ao"]:
            return _completed(stdout=table)
        if argv[:2] == ["ps", "eww"]:
            pid = int(argv[argv.index("-p") + 1])
            return _completed(stdout=envs.get(pid, ""))
        return _completed(returncode=1)

    return runner


def test_holders_are_keyed_by_config_dir_and_default_when_unset() -> None:
    table = (
        "  101 10:00:00 /Users/o/.local/bin/claude --dangerously-skip-permissions\n"
        "  102 01-02:00:00 /Users/o/.local/bin/claude -p build\n"
    )
    envs = {
        101: "  101 ?? S 0:01 claude CLAUDE_CONFIG_DIR=/Users/o/.claude-d HOME=/Users/o",
        102: "  102 ?? S 0:01 claude HOME=/Users/o",
    }
    holders = list_holders(
        default_config_dir="/Users/o/.claude", runner=_ps_runner(table, envs)
    )
    assert [p.pid for p in holders["/Users/o/.claude-d"]] == [101]
    assert [p.pid for p in holders["/Users/o/.claude"]] == [102]
    assert holders["/Users/o/.claude-d"][0].elapsed == "10:00:00"


def test_desktop_helper_processes_are_not_holders() -> None:
    table = (
        "  201 05:00 /Applications/Claude.app/Contents/MacOS/Claude Helper --type=utility\n"
        "  202 05:00 /Applications/Claude.app/Contents/Frameworks/Claude Helper.app/x\n"
    )
    assert list_holders(default_config_dir="/Users/o/.claude", runner=_ps_runner(table, {})) == {}


def test_argv_mentioning_the_variable_cannot_shadow_the_real_environment() -> None:
    """The env is printed after the command, so the LAST match is the real one."""
    table = "  301 10:00 /Users/o/.local/bin/claude -p echo CLAUDE_CONFIG_DIR=/decoy\n"
    envs = {
        301: "  301 ?? S 0:01 claude -p echo CLAUDE_CONFIG_DIR=/decoy "
        "CLAUDE_CONFIG_DIR=/Users/o/.claude-c"
    }
    holders = list_holders(default_config_dir="/Users/o/.claude", runner=_ps_runner(table, envs))
    assert "/Users/o/.claude-c" in holders
    assert "/decoy" not in holders


# ======================================================================================
# detect_transitions
# ======================================================================================


def _sample(at_s: float, states: dict[str, str]) -> Sample:
    return Sample(
        at=str(at_s),
        at_s=at_s,
        credentials={
            aid: CredentialSample(account_id=aid, config_dir=f"/{aid}", service="s", state=st)
            for aid, st in states.items()
        },
        holders={},
    )


def test_first_sample_reports_no_transitions() -> None:
    assert detect_transitions(None, _sample(NOW, {"claude": STATE_BLANK})) == []


def test_healthy_to_blank_is_a_loss_with_the_gap_recorded() -> None:
    prev = _sample(NOW, {"claude_d": STATE_HEALTHY})
    cur = _sample(NOW + 60, {"claude_d": STATE_BLANK})
    (t,) = detect_transitions(prev, cur)
    assert (t.previous_state, t.current_state, t.lost) == (STATE_HEALTHY, STATE_BLANK, True)
    assert t.gap_s == 60.0


def test_recovery_is_recorded_but_is_not_a_loss() -> None:
    prev = _sample(NOW, {"claude_b": STATE_BLANK})
    cur = _sample(NOW + 60, {"claude_b": STATE_HEALTHY})
    (t,) = detect_transitions(prev, cur)
    assert t.lost is False


def test_expiry_is_a_transition_but_never_a_loss() -> None:
    prev = _sample(NOW, {"claude": STATE_HEALTHY})
    cur = _sample(NOW + 60, {"claude": STATE_EXPIRED})
    (t,) = detect_transitions(prev, cur)
    assert t.lost is False


def test_unchanged_state_yields_nothing() -> None:
    prev = _sample(NOW, {"claude": STATE_HEALTHY})
    assert detect_transitions(prev, _sample(NOW + 60, {"claude": STATE_HEALTHY})) == []


# ======================================================================================
# run_once
# ======================================================================================


def _full_runner(payload: str | None):
    table = "  401 10:00:00 /Users/o/.local/bin/claude --resume x\n"
    envs = {401: "  401 ?? S 0:01 claude CLAUDE_CONFIG_DIR=/Users/o/.claude-d"}

    def runner(argv, **kwargs):
        if argv[0] == "security":
            return _completed(stdout=payload) if payload is not None else _completed(returncode=44)
        if argv[:2] == ["ps", "-Ao"]:
            return _completed(stdout=table)
        if argv[:2] == ["ps", "eww"]:
            return _completed(stdout=envs.get(int(argv[argv.index("-p") + 1]), ""))
        return _completed(returncode=1)

    return runner


def test_first_run_writes_state_but_no_forensic_record(tmp_path: Path) -> None:
    state, log = tmp_path / "s.json", tmp_path / "f.jsonl"
    _, transitions = run_once(
        [("claude_d", "/Users/o/.claude-d")],
        now_s=NOW, now_iso=ISO, default_config_dir="/Users/o/.claude",
        state_path=state, log_path=log, runner=_full_runner(_blob()),
    )
    assert transitions == []
    assert state.exists() and not log.exists()


def test_loss_writes_a_record_carrying_the_lead_up_and_the_holders(tmp_path: Path) -> None:
    state, log = tmp_path / "s.json", tmp_path / "f.jsonl"
    args = dict(
        accounts=[("claude_d", "/Users/o/.claude-d")],
        default_config_dir="/Users/o/.claude", state_path=state, log_path=log,
    )
    run_once(**args, now_s=NOW, now_iso=ISO, runner=_full_runner(_blob()))
    _, transitions = run_once(
        **args, now_s=NOW + 60, now_iso=ISO, runner=_full_runner(_blob(access=""))
    )

    assert [t.lost for t in transitions] == [True]
    record = json.loads(log.read_text(encoding="utf-8").strip())
    assert record["transitions"][0]["previous_state"] == STATE_HEALTHY
    assert record["transitions"][0]["current_state"] == STATE_BLANK
    assert record["transitions"][0]["gap_s"] == 60.0
    # The healthy sample that preceded the loss, with who held the directory.
    assert record["lead_up"][-1]["credentials"]["claude_d"]["state"] == STATE_HEALTHY
    assert record["lead_up"][-1]["holders"]["/Users/o/.claude-d"][0]["pid"] == 401
    assert record["current"]["credentials"]["claude_d"]["field_lengths"]["accessToken"] == 0


def test_history_is_trimmed_to_the_configured_depth(tmp_path: Path) -> None:
    state, log = tmp_path / "s.json", tmp_path / "f.jsonl"
    for i in range(6):
        run_once(
            [("claude_d", "/Users/o/.claude-d")],
            now_s=NOW + i, now_iso=ISO, default_config_dir="/Users/o/.claude",
            state_path=state, log_path=log, runner=_full_runner(_blob()), history_depth=3,
        )
    assert len(json.loads(state.read_text(encoding="utf-8"))["samples"]) == 3


def test_corrupt_state_file_does_not_stop_sampling(tmp_path: Path) -> None:
    """Losing history costs one record's context; raising would cost every future one."""
    state, log = tmp_path / "s.json", tmp_path / "f.jsonl"
    state.write_text("{ not json", encoding="utf-8")
    sample, transitions = run_once(
        [("claude_d", "/Users/o/.claude-d")],
        now_s=NOW, now_iso=ISO, default_config_dir="/Users/o/.claude",
        state_path=state, log_path=log, runner=_full_runner(_blob()),
    )
    assert transitions == []
    assert sample.credentials["claude_d"].state == STATE_HEALTHY
    assert json.loads(state.read_text(encoding="utf-8"))["samples"]
