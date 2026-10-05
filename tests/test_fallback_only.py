"""Fallback-only accounts and ``report-failure``.

An ``unmetered = "fallback"`` account has no quota reading at all, so it serves a
capability request only when no measured account can, and ``report-failure`` is how a
caller tells the router that its pool ran out.
"""

from __future__ import annotations

import json

import pytest

from quota_router import cli, report_failure
from quota_router.config import ConfigError, load_config
from quota_router.state import StateStore
from tests.test_capability import (  # noqa: F401 - fixtures
    MIN,
    FakeIO,
    _config_file,
    _refresh,
    agy_on_path,
    env,
    home,
)
from tests.test_capability_pick import _claude, _codex, _fleet, _rows
from tests.test_cli import NOW, _NO_CONFIG_FILES, run

AGY_QUOTA = "Individual quota reached. Your quota will refresh. Resets in 25m54s"
NO_DEADLINE = "You've reached your usage limit"
LOGIN_RACE = "Not logged in · Please run /login"


def _fallback_config(tmp_path, *, both=False) -> str:
    path = _config_file(tmp_path)
    text = open(path).read()
    text = text.replace(
        '[accounts.antigravity_gemini]\nprovider = "antigravity"\n',
        '[accounts.antigravity_gemini]\nprovider = "antigravity"\nunmetered = "fallback"\n',
    )
    if both:
        text = text.replace(
            '[accounts.antigravity_gemini_b]\nprovider = "antigravity"\n',
            '[accounts.antigravity_gemini_b]\nprovider = "antigravity"\nunmetered = "fallback"\n',
        )
    open(path, "w").write(text)
    return path


@pytest.fixture()
def fallback(env, tmp_path, agy_on_path):
    _refresh(env, tmp_path, FakeIO(), now_s=NOW)
    return _fallback_config(tmp_path)


def _spent():
    return _fleet(
        claude=_claude("claude", five=1.0),
        claude_b=_claude("claude_b", five=1.0),
        codex=_codex("codex", five=1.0),
        codex_b=_codex("codex_b", five=1.0),
    )


def _pick(env, config, *argv, snapshots, now_s=NOW):
    code, out, err = run(["pick", "--config", config, "--dry-run", *argv], env,
                         snapshots=snapshots, now_s=now_s, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    return json.loads(out)


# ======================================================================================
# Fallback-only (T14)
# ======================================================================================


def test_a_fallback_account_waits_while_a_measured_account_fits(env, fallback):
    payload = _pick(env, fallback, "--capability", "premium", snapshots=_fleet())
    assert payload["decision"]["provider"] in {"claude", "codex"}
    reason = _rows(payload, "excluded")["antigravity_gemini"]["reason"]
    assert reason == "fallback-only: used only when no measured account can serve premium"


def test_a_fallback_account_serves_when_nothing_measured_can(env, fallback):
    payload = _pick(env, fallback, "--capability", "premium", snapshots=_spent())

    decision = payload["decision"]
    assert decision["account"] == "antigravity_gemini"
    assert decision["model"]["id"] == "Gemini 3.1 Pro (High)"
    assert (decision["fits"], decision["meets_policy"], decision["available_at"]) == (
        True, True, None,
    )
    assert payload["ranked"][0]["account"] == "antigravity_gemini"
    assert any("chosen as fallback-only" in w for w in payload["warnings"])
    # A pool not marked fallback-only is not offered as able to serve: it keeps the
    # place today's exhausted-fallback ordering gives it, with fits=false.
    assert _rows(payload)["antigravity_gemini_b"]["fits"] is False


def test_without_a_capability_a_fallback_account_behaves_as_today(env, fallback):
    payload = _pick(env, fallback, snapshots=_spent())
    assert payload["decision"]["fits"] is False
    excluded = _rows(payload, "excluded")
    assert excluded["antigravity_gemini"]["reason"] == "no usage windows in snapshot"


def test_a_caller_floor_keeps_the_fallback_out(env, fallback):
    payload = _pick(env, fallback, "--capability", "premium", "--min-remaining", "0.1",
                    snapshots=_spent())
    assert payload["decision"]["account"] != "antigravity_gemini"
    # The floor check already rejects an unmeasurable account, with today's reason.
    assert "cannot be verified against the min-remaining floor" in (
        _rows(payload, "excluded")["antigravity_gemini"]["reason"]
    )


def test_an_unavailable_fallback_account_is_not_revived(env, fallback):
    from dataclasses import replace

    snaps = {s.id: s for s in _spent()}
    snaps["antigravity_gemini"] = replace(
        snaps["antigravity_gemini"], available=False, note="agy is not installed"
    )
    payload = _pick(env, fallback, "--capability", "premium", snapshots=tuple(snaps.values()))
    assert payload["decision"]["account"] != "antigravity_gemini"
    assert _rows(payload, "excluded")["antigravity_gemini"]["reason"].endswith("agy is not installed")


def test_frontier_has_no_antigravity_fallback(env, fallback):
    payload = _pick(env, fallback, "--capability", "frontier", snapshots=_spent())
    assert payload["decision"]["fits"] is False
    assert _rows(payload, "excluded")["antigravity_gemini"]["reason"] == (
        "capability frontier: no antigravity model for frontier"
    )


def test_unmetered_accepts_only_fallback(env, tmp_path):
    path = _config_file(tmp_path)
    text = open(path).read().replace(
        '[accounts.antigravity_gemini]\nprovider = "antigravity"\n',
        '[accounts.antigravity_gemini]\nprovider = "antigravity"\nunmetered = "yes"\n',
    )
    open(path, "w").write(text)
    with pytest.raises(ConfigError, match='must be "fallback"'):
        load_config(env=env, cwd=_NO_CONFIG_FILES, explicit_path=path)


# ======================================================================================
# report-failure (T15)
# ======================================================================================


def test_a_reported_exhaustion_benches_the_fallback_until_its_reset(env, tmp_path, agy_on_path):
    _refresh(env, tmp_path, FakeIO(), now_s=NOW)
    config = _fallback_config(tmp_path, both=True)

    code, out, err = run(["report-failure", "antigravity_gemini", "--text", AGY_QUOTA,
                          "--config", config], env, now_s=NOW, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    outcome = json.loads(out)
    assert (outcome["kind"], outcome["written"], outcome["deadline_known"]) == (
        "exhausted_with_deadline", True, True,
    )
    assert outcome["cooldown_until"] == cli._iso(NOW + 25 * 60 + 54)

    benched = _pick(env, config, "--capability", "premium", snapshots=_spent(), now_s=NOW + MIN)
    assert benched["decision"]["account"] == "antigravity_gemini_b"
    assert "cooling off until" in _rows(benched, "excluded")["antigravity_gemini"]["reason"]

    back = _pick(env, config, "--capability", "premium", snapshots=_spent(), now_s=NOW + 26 * MIN)
    assert back["decision"]["account"] == "antigravity_gemini"


def test_exhaustion_without_a_deadline_benches_for_an_hour(env, fallback):
    outcome = report_failure("antigravity_gemini", NO_DEADLINE, config=fallback, env=env,
                             now_s=NOW, cwd=_NO_CONFIG_FILES)
    assert outcome["written"] is True and outcome["deadline_known"] is False
    assert outcome["cooldown_until"] == cli._iso(NOW + 3600)


def test_a_transient_failure_records_nothing(env, fallback):
    outcome = report_failure("antigravity_gemini", LOGIN_RACE, config=fallback, env=env,
                             now_s=NOW, cwd=_NO_CONFIG_FILES)
    assert (outcome["kind"], outcome["written"], outcome["cooldown_until"]) == (
        "transient", False, None,
    )
    assert StateStore(env=env).load().selection == {}


def test_a_measured_account_is_refused_and_nothing_is_written(env, fallback):
    code, _out, err = run(["report-failure", "claude", "--text", AGY_QUOTA, "--config", fallback],
                          env, now_s=NOW, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_USAGE
    assert 'accepts only accounts with unmetered = "fallback"' in err
    assert not StateStore(env=env).path.exists()

    with pytest.raises(ValueError, match="has live usage windows"):
        report_failure("claude", AGY_QUOTA, config=fallback, env=env, now_s=NOW,
                       cwd=_NO_CONFIG_FILES)
