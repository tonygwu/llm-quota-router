import json
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


def test_codex_usage_only_is_not_served_model_evidence():
    a = AccountConfig(id="codex", config_dir="/synthetic/codex")
    events = [{"type": "thread.started", "thread_id": "synthetic"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "2"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1}}]
    response = SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, events)), stderr="model: gpt-6-luna")
    parsed = parse_response(a, response)
    assert parsed["kind"] == "model_unverified"
    assert parsed["tokens"]["input_tokens"] == 5


def test_agy_selected_label_is_not_a_served_stamp():
    a = AccountConfig(id="antigravity_gemini", identity_email="synthetic@example.test")
    response = completed({"status": "SUCCESS", "num_turns": 1, "response": "2",
        "usage": {"input_tokens": 5, "output_tokens": 1, "thinking_tokens": 0, "cache_read_tokens": 0}},
        'OAuth: authenticated successfully as synthetic@example.test\n'
        'Propagating selected model override to backend: label="Gemini 3.8 Flash (Low)"')
    assert parse_response(a, response)["kind"] == "model_unverified"
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


def test_agy_wrapper_and_separate_pool_models():
    a = AccountConfig(id="antigravity_claude_b", provider="antigravity", macos_user="synthetic-user",
        launch_wrapper="/synthetic/wrapper", identity_email="synthetic@example.test", env={"AGY_MODEL": "claude"})
    argv, env = command_for(a, {"HOME": "/wrong", "PATH": "/bin"}, "/tmp/synthetic")
    assert argv[:5] == ["sudo", "-n", "/synthetic/wrapper", "synthetic-user", "/usr/local/bin/agy"]
    assert "HOME" not in env
    assert argv[argv.index("--model")+1] == "gpt-oss-120b-medium"
    assert model_for(AccountConfig(id="antigravity_gemini")) == "gemini-3.8-flash-low"
