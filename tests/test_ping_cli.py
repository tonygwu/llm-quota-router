import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from quota_router.config import AccountConfig
from quota_router.ping_cli import command_for, parse_response, model_for


def completed(payload, stderr="", code=0):
    return SimpleNamespace(stdout=json.dumps(payload), stderr=stderr, returncode=code)


def claude_result(model="claude-haiku-5-5"):
    return {"type": "result", "subtype": "success", "is_error": False, "result": "2",
        "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "modelUsage": {model: {"inputTokens": 1, "outputTokens": 1}}}


def test_claude_requires_model_tokens_and_answer():
    a = AccountConfig(id="claude", config_dir="/synthetic/.claude")
    assert parse_response(a, completed(claude_result()))["outcome"] == "succeeded"
    assert parse_response(a, completed(claude_result("claude-opus-5-5")))["kind"] == "wrong_model"
    r = claude_result(); del r["usage"]["input_tokens"]
    assert parse_response(a, completed(r))["kind"] == "telemetry"
    r = claude_result(); r["result"] = "/tmp/answer.txt"
    assert parse_response(a, completed(r))["kind"] == "answer"
    assert parse_response(a, completed(claude_result(), code=1))["kind"] == "cli_exit"


@pytest.mark.parametrize("answer", ["1 + 1 = **2**", "**Answer:**\u202f1\u202f+\u202f1\u202f=\u202f**2**.\n"])
def test_observed_markdown_arithmetic_answers(answer):
    a = AccountConfig(id="claude", config_dir="/synthetic/.claude")
    result = claude_result()
    result["result"] = answer
    assert parse_response(a, completed(result))["outcome"] == "succeeded"


@pytest.mark.parametrize("answer", ["12", "2 or 3", "**3**", "2\nI read your files", "answer.txt"])
def test_arithmetic_validation_rejects_other_content(answer):
    a = AccountConfig(id="claude", config_dir="/synthetic/.claude")
    result = claude_result()
    result["result"] = answer
    assert parse_response(a, completed(result))["kind"] == "answer"


def test_codex_usage_only_is_not_served_model_evidence():
    a = AccountConfig(id="codex", config_dir="/synthetic/codex")
    events = [{"type": "thread.started", "thread_id": "synthetic"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "2"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1}}]
    response = SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, events)), stderr="model: gpt-6-luna")
    parsed = parse_response(a, response)
    assert parsed["outcome"] == "succeeded"
    assert parsed["model_evidence"] == "selected_model_only"
    assert parsed["model_served"] is None
    assert parsed["tokens"]["input_tokens"] == 5


def test_agy_selected_label_is_not_a_served_stamp():
    a = AccountConfig(id="antigravity_gemini", identity_email="synthetic@example.test")
    response = completed({"status": "SUCCESS", "num_turns": 1, "response": "2",
        "usage": {"input_tokens": 5, "output_tokens": 1, "thinking_tokens": 0, "cache_read_tokens": 0}},
        'OAuth: authenticated successfully as synthetic@example.test\n'
        'Propagating selected model override to backend: label="Gemini 3.8 Flash (Low)"')
    parsed = parse_response(a, response)
    assert parsed["outcome"] == "succeeded"
    assert parsed["model_evidence"] == "selected_model_only"
    assert parsed["model_served"] is None
    response.stderr = 'OAuth: authenticated successfully as wrong@example.test'
    assert parse_response(a, response)["kind"] == "account_identity"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_launchd_binary_resolution_and_account_isolation(provider):
    a = AccountConfig(id=provider, config_dir="/synthetic/.claude" if provider == "claude" else "/synthetic/.codex")
    argv, env = command_for(a, {"PATH": "/bin", "CL_CLAUDE_BIN": "/vendor/claude",
        "QUOTA_ROUTER_CODEX_BIN": "/vendor/codex", "CLAUDE_CONFIG_DIR": "/wrong", "CODEX_HOME": "/wrong",
        "ANTHROPIC_API_KEY": "secret", "OPENAI_API_KEY": "secret"}, "/tmp/synthetic")
    assert argv[0] == "/vendor/"+provider
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    if provider == "claude":
        assert "CLAUDE_CONFIG_DIR" not in env
        assert "--safe-mode" in argv
    else:
        assert env["CODEX_HOME"] == "/synthetic/.codex"
        assert argv[argv.index("--sandbox")+1] == "read-only"
        assert 'model_reasoning_effort="low"' in argv


def test_codex_env_node_shim_runs_with_launchd_path(tmp_path):
    # Execute a synthetic env-node shim, without a real Codex call or Node dependency.
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    node = vendor / "node"
    node.write_text('#!/bin/sh\nexec /bin/sh "$@"\n')
    node.chmod(0o755)
    binary = vendor / "codex"
    binary.write_text("#!/usr/bin/env node\nprintf 'synthetic-codex-shim\\n'\n")
    binary.chmod(0o755)
    launchd_path = f"{tmp_path}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    base = {"HOME": str(tmp_path), "PATH": launchd_path,
            "QUOTA_ROUTER_CODEX_BIN": str(binary)}
    account = AccountConfig(id="codex_b", config_dir=str(tmp_path / ".codex-b"))
    argv, child = command_for(account, base, str(tmp_path))
    result = subprocess.run(argv, env=child, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "synthetic-codex-shim\n"
    assert child["PATH"].split(os.pathsep) == [str(vendor), *launchd_path.split(os.pathsep)]
    assert child["CODEX_HOME"] == account.config_dir
    assert base["PATH"] == launchd_path


@pytest.mark.parametrize("path", [None, "", "/usr/bin:/vendor:/bin:/vendor"])
def test_codex_binary_directory_leads_path_once(path):
    env = {"QUOTA_ROUTER_CODEX_BIN": "/vendor/codex"}
    if path is not None:
        env["PATH"] = path
    _, child = command_for(AccountConfig(id="codex", config_dir="/synthetic/.codex"),
                           env, "/tmp/synthetic")
    assert child["PATH"] == ("/vendor:/usr/bin:/bin" if path else "/vendor")


def test_agy_wrapper_and_separate_pool_models():
    a = AccountConfig(id="antigravity_claude_b", provider="antigravity", macos_user="synthetic-user",
        launch_wrapper="/synthetic/wrapper", identity_email="synthetic@example.test", env={"AGY_MODEL": "claude"})
    argv, env = command_for(a, {"HOME": "/wrong", "PATH": "/bin"}, "/tmp/synthetic")
    assert argv[:5] == ["sudo", "-n", "/synthetic/wrapper", "synthetic-user", "/usr/local/bin/agy"]
    assert "HOME" not in env
    assert argv[argv.index("--model")+1] == "claude-sonnet-5-5-low"
    assert model_for(AccountConfig(id="antigravity_gemini")) == "gemini-3.8-flash-low"


def test_agy_final_environment_cannot_restore_credit_credentials():
    banned = {"ANTHROPIC_API_KEY": "synthetic", "GEMINI_API_KEY": "synthetic",
              "GOOGLE_API_KEY": "synthetic", "OPENAI_BASE_URL": "https://invalid.test",
              "OPENAI_API_KEY": "synthetic", "CODEX_API_KEY": "synthetic"}
    a = AccountConfig(id="antigravity_claude_b", macos_user="synthetic-user",
        launch_wrapper="/synthetic/wrapper", identity_email="synthetic@example.test",
        env={**banned, "AGY_MODEL": "claude"})
    _, env = command_for(a, {**banned, "PATH": "/bin"}, "/tmp/synthetic")
    assert not (env.keys() & banned.keys())
    assert env["AGY_MODEL"] == "claude"


def native_response(provider, *, stamp=None, answer="2", missing_tokens=False):
    usage = {"input_tokens": 5, "output_tokens": 1}
    if missing_tokens:
        del usage["output_tokens"]
    if provider == "codex":
        usage["cached_input_tokens"] = 0
        result = {"type": "turn.completed", "usage": usage}
        if stamp is not None:
            result["model"] = stamp
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": answer}}, result]
        return SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, events)), stderr="")
    usage.update(thinking_tokens=0, cache_read_tokens=0)
    result = {"status": "SUCCESS", "num_turns": 1, "usage": usage, "response": answer}
    if stamp is not None:
        result["model"] = stamp
    return completed(result, "OAuth: authenticated successfully as synthetic@example.test")


@pytest.mark.parametrize("provider", ["codex", "antigravity_gemini"])
def test_selected_only_still_rejects_bad_answer_tokens_and_model(provider):
    account = AccountConfig(id=provider, identity_email="synthetic@example.test")
    assert parse_response(account, native_response(provider, answer="3"))["kind"] == "answer"
    assert parse_response(account, native_response(provider, missing_tokens=True))["kind"] == "telemetry"
    assert parse_response(account, native_response(provider, stamp="different-model"))["kind"] == "wrong_model"
    for invalid_stamp in ("", [], 0, False):
        assert parse_response(account, native_response(provider, stamp=invalid_stamp))["kind"] == "telemetry"
    parsed = parse_response(account, native_response(provider, stamp=model_for(account)))
    assert parsed["outcome"] == "succeeded"
    assert parsed["model_evidence"] == "served_model"


def test_claude_multiple_served_models_cannot_be_called_selected_only():
    account = AccountConfig(id="claude")
    response = claude_result()
    response["modelUsage"]["different-model"] = {"inputTokens": 1}
    assert parse_response(account, completed(response))["kind"] == "wrong_model"


def test_claude_without_served_stamp_records_selection_only():
    response = claude_result()
    del response["modelUsage"]
    parsed = parse_response(AccountConfig(id="claude"), completed(response))
    assert parsed["outcome"] == "succeeded"
    assert parsed["model_evidence"] == "selected_model_only"
    assert parsed["model_served"] is None


@pytest.mark.parametrize("canonical,kind", [
    ("claude-haiku-5-5", None), ("claude-sonnet-5-5", "wrong_model"),
    ("", "telemetry"), (False, "telemetry"), (None, "telemetry"), ([], "telemetry"),
])
def test_claude_canonical_model_stamp_is_validated_and_preserved(canonical, kind):
    response = claude_result()
    response["modelUsage"]["claude-haiku-5-5"]["canonicalModel"] = canonical
    parsed = parse_response(AccountConfig(id="claude"), completed(response))
    assert parsed["kind"] == kind
    assert parsed["outcome"] == ("succeeded" if kind is None else "failed")
    assert parsed["canonical_models_served"] == {"claude-haiku-5-5": canonical}
    assert parsed["tokens"]["input_tokens"] == 1


@pytest.mark.parametrize("entry", [None, [], "claude-haiku-5-5"])
def test_claude_model_usage_entry_must_be_an_object(entry):
    response = claude_result()
    response["modelUsage"]["claude-haiku-5-5"] = entry
    parsed = parse_response(AccountConfig(id="claude"), completed(response))
    assert parsed["outcome"] == "failed"
    assert parsed["kind"] == "telemetry"
    assert parsed["tokens"]["input_tokens"] == 1


@pytest.mark.parametrize("stamp,evidence", [
    (None, None), ("different-model", "selected_model_only"),
    ("different-model", "served_model"), ("gpt-6-luna", "selected_model_only"),
])
def test_journal_rejects_unlabelled_or_conflicting_success(tmp_path, stamp, evidence):
    from quota_router.window_ping import _journal
    path = tmp_path / "pings.jsonl"
    path.write_text(json.dumps({"event": "result", "pool": "codex", "at_s": 1800000000,
        "started_at_s": 1800000000, "attempt_id": "synthetic", "windows": [],
        "outcome": "succeeded", "model_asked": "gpt-6-luna", "model_served": stamp,
        "model_evidence": evidence, "tokens": {"input_tokens": 5, "output_tokens": 1}}) + "\n")
    with pytest.raises(ValueError, match="identity/token"):
        _journal(path)


@pytest.mark.parametrize("provider", ["codex", "antigravity_gemini"])
def test_selected_only_success_survives_journal_reload_and_sets_full_cooldown(tmp_path, provider):
    from quota_router.config import Config
    from quota_router.types import AccountSnapshot, Window
    from quota_router.window_ping import run_pings
    now = 1_800_000_000.0
    account = AccountConfig(id=provider, identity_email="synthetic@example.test")
    config = Config(accounts={provider: account})
    path = tmp_path / "pings.jsonl"
    def records(at):
        window = Window("5h", 0, 18000, at + 18000, at)
        return [{"event": "status", "t": at, "accounts": [
            AccountSnapshot(id=provider, source="live", windows=(window,)).to_dict()]}]
    report = run_pings(config, records(now), path, now_s=now, dry_run=False, env={},
        clock=lambda: now, send=lambda *_: parse_response(account, native_response(provider)))
    assert (report["attempted"], report["succeeded"], report["failed"]) == (1, 1, 0)
    # Resolve verification separately so the cooldown, not a pending check, blocks.
    with path.open("a") as out:
        out.write(json.dumps({"event": "verification", "pool": provider, "at_s": now + 1,
            "attempt_id": report["results"][0]["attempt_id"], "key": "5h", "outcome": "verified"}) + "\n")
    later = now + 1801  # Past failure backoff, still inside the successful 5h cooldown.
    again = run_pings(config, records(later), path, now_s=later, env={})
    assert again["plans"][0]["reason"] == "successful_ping_cooldown"
    assert again["attempted"] == again["would_ping"] == 0
