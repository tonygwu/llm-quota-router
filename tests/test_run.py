"""``quotapick run``: the router executes one tool-less call and reports what served it.

The fake runner below returns stream-json shaped exactly like a real response captured
on 2026-10-14 (Claude Code 2.1.300): an ``init`` event naming ``apiKeySource``, and a
``result`` event with ``result``, ``structured_output``, ``total_cost_usd``, ``usage``
and ``modelUsage`` keyed by the served model. What must hold:

* The child runs ``claude -p`` with no tools, no MCP, no session file, safe mode, an
  exact model id, the prompt on stdin, and never ``--fallback-model``.
* Its environment can never bill an API key: ``ANTHROPIC_API_KEY``, OAuth-token and
  cloud-provider variables are removed, and the account is chosen by config dir alone
  (the default account by the variable's absence).
* The served model and the auth are checked, not assumed.
* Exhaustion, auth and transient failures retry on another account; a timeout does not.
* History records the outcome and never the prompt.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quota_router import cli
from tests.test_cli import FIVE_HOURS, NOW, _NO_CONFIG_FILES, account, run, window

HOUR = 3600.0
MODEL = "claude-haiku-5-5"


def _stream(*, model=MODEL, text="ok", structured=None, cost=0.0003, is_error=False,
            api_key_source="none", subtype="success"):
    events = [
        {"type": "system", "subtype": "init", "apiKeySource": api_key_source, "model": model,
         "tools": [], "mcp_servers": []},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": subtype, "is_error": is_error, "num_turns": 1,
         "result": text, "structured_output": structured, "total_cost_usd": cost,
         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1140,
                   "cache_read_input_tokens": 0, "output_tokens": 5},
         "modelUsage": {model: {"costUSD": cost}}},
    ]
    return "".join(json.dumps(e) + "\n" for e in events)


class FakeRunner:
    """Stands in for subprocess.run; replies from a queue, records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        code, out, err = reply
        return subprocess.CompletedProcess(argv, code, out, err)


def _snapshots():
    def acct(account_id, weekly, reset_h):
        return account(
            account_id,
            window("5h", 0.1, length_s=FIVE_HOURS, resets_in_s=2 * HOUR),
            window("7d", weekly, resets_in_s=reset_h * HOUR),
        )
    return (acct("claude", 0.3, 30), acct("claude_b", 0.3, 20))


@pytest.fixture()
def env(tmp_path):
    xdg = tmp_path / "config" / "quota-router"
    xdg.mkdir(parents=True)
    (xdg / "config.toml").write_text("[engine]\nusd_per_pse = 305\n", encoding="utf-8")
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
        "QUOTA_ROUTER_CLAUDE_BIN": "/opt/test/claude",
        "ANTHROPIC_API_KEY": "sk-ant-should-never-reach-a-subscription-run",
        "CLAUDE_CODE_OAUTH_TOKEN": "also-never",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CONFIG_DIR": "/somewhere/inherited",
    }


def _run(env, request, runner, *, snapshots=None):
    deps = cli.Deps(load_snapshots=lambda **kw: (list(snapshots or _snapshots()), []),
                    run=runner)
    code, out, err = run(["run"], env, deps=deps, cwd=_NO_CONFIG_FILES, stdin=json.dumps(request))
    return code, (json.loads(out) if out.strip() else None), err


def _request(**kw):
    return {"model": MODEL, "prompt": "Say ok.", "system": "Be terse.", **kw}


def test_a_call_runs_claude_with_no_tools_and_an_exact_model(env) -> None:
    runner = FakeRunner((0, _stream(), ""))
    code, payload, err = _run(env, _request(), runner)
    assert code == cli.EXIT_OK, err
    argv = runner.calls[0]["argv"]
    assert argv[0] == "/opt/test/claude"
    for flag in ("-p", "--no-session-persistence", "--strict-mcp-config", "--safe-mode"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--model") + 1] == MODEL
    assert argv[argv.index("--system-prompt") + 1] == "Be terse."
    assert "--fallback-model" not in argv
    assert "--max-turns" not in argv
    assert runner.calls[0]["input"] == "Say ok."
    assert payload["run_contract"] == 1
    assert payload["ok"] is True and payload["outcome"] == "ok"
    assert payload["text"] == "ok"
    assert payload["served"]["model"] == MODEL
    assert payload["served"]["pool_kind"] == "subscription"
    assert payload["served"]["auth"] == "oauth"
    assert payload["cost_usd"] == pytest.approx(0.0003)
    assert payload["cost_pse"] == pytest.approx(0.0003 / 305)


def test_the_child_can_never_bill_an_api_key(env) -> None:
    runner = FakeRunner((0, _stream(), ""))
    _code, payload, _err = _run(env, _request(), runner)
    child = runner.calls[0]["env"]
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                 "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        assert name not in child
    served = payload["served"]["account"]
    if served == "claude":
        assert "CLAUDE_CONFIG_DIR" not in child  # the default account, by absence
    else:
        assert child["CLAUDE_CONFIG_DIR"].endswith(".claude-b")
    assert runner.calls[0]["cwd"] != _NO_CONFIG_FILES


def test_a_json_schema_returns_structured_output(env) -> None:
    schema = {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]}
    runner = FakeRunner((0, _stream(text='{"word":"ok"}', structured={"word": "ok"}), ""))
    _code, payload, _err = _run(env, _request(json_schema=schema), runner)
    argv = runner.calls[0]["argv"]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema
    assert payload["structured"] == {"word": "ok"}


def test_a_missing_structured_output_is_schema_invalid(env) -> None:
    runner = FakeRunner((0, _stream(text="ok", structured=None), ""))
    _code, payload, _err = _run(env, _request(json_schema={"type": "object"}), runner)
    assert payload["ok"] is False and payload["outcome"] == "schema_invalid"
    assert len(payload["attempts"]) == 1


def test_a_different_served_model_retries_elsewhere(env) -> None:
    runner = FakeRunner((0, _stream(model="claude-sonnet-5-5"), ""), (0, _stream(), ""))
    _code, payload, _err = _run(env, _request(), runner)
    assert [a["outcome"] for a in payload["attempts"]] == ["wrong_model", "ok"]
    first, second = (a["account"] for a in payload["attempts"])
    assert first != second
    assert payload["ok"] is True


def test_exhaustion_retries_on_another_account(env) -> None:
    exhausted = _stream(is_error=True, subtype="error_during_execution",
                        text="Claude AI usage limit reached|1792030000")
    runner = FakeRunner((1, exhausted, ""), (0, _stream(), ""))
    _code, payload, _err = _run(env, _request(), runner)
    assert [a["outcome"] for a in payload["attempts"]] == ["exhausted", "ok"]


def test_a_timeout_is_not_retried(env) -> None:
    runner = FakeRunner(subprocess.TimeoutExpired(cmd="claude", timeout=5), (0, _stream(), ""))
    _code, payload, _err = _run(env, _request(timeout_s=5), runner)
    assert payload["outcome"] == "timeout"
    assert len(runner.calls) == 1


def test_an_api_key_on_a_subscription_run_is_an_auth_failure(env) -> None:
    runner = FakeRunner((0, _stream(api_key_source="ANTHROPIC_API_KEY"), ""))
    _code, payload, _err = _run(env, _request(max_attempts=1), runner)
    assert payload["outcome"] == "auth"
    assert "apiKeySource" in payload["attempts"][0]["detail"]


def test_attempts_stop_at_the_cap(env) -> None:
    bad = (0, _stream(model="claude-other"), "")
    runner = FakeRunner(bad, bad, bad)
    _code, payload, _err = _run(env, _request(max_attempts=2), runner)
    assert len(payload["attempts"]) == 2
    assert payload["ok"] is False


def test_history_records_the_outcome_and_never_the_prompt(env, tmp_path) -> None:
    runner = FakeRunner((0, _stream(), ""))
    _run(env, _request(prompt="TOP-SECRET-PROMPT-TEXT", client="test"), runner)
    history = Path(env["XDG_STATE_HOME"]) / "quota-router" / "history.jsonl"
    text = history.read_text()
    assert "TOP-SECRET-PROMPT-TEXT" not in text
    outcome = [json.loads(line) for line in text.splitlines() if '"run"' in line][-1]
    assert outcome["event"] == "run"
    assert outcome["run"]["outcome"] == "ok"
    assert outcome["run"]["pool_kind"] == "subscription"
    assert outcome["run"]["cost_pse"] == pytest.approx(0.0003 / 305)
    assert outcome["run"]["client"] == "test"


@pytest.mark.parametrize(
    "request_, fragment",
    [
        ({"model": MODEL}, "prompt"),
        ({"prompt": "x"}, "capability"),
        ({"model": MODEL, "prompt": "x", "timeout_s": 99999}, "timeout_s"),
        ({"model": MODEL, "prompt": "x", "max_attempts": 9}, "max_attempts"),
        ({"model": MODEL, "prompt": "x", "json_schema": "not an object"}, "json_schema"),
    ],
)
def test_a_bad_request_exits_2_with_a_reason(env, request_, fragment) -> None:
    code, payload, _err = _run(env, request_, FakeRunner())
    assert code == cli.EXIT_USAGE
    assert payload["outcome"] == "bad_request"
    assert fragment in payload["error"]


def test_run_needs_usd_per_pse(env, tmp_path) -> None:
    (Path(env["XDG_CONFIG_HOME"]) / "quota-router" / "config.toml").write_text("")
    code, payload, _err = _run(env, _request(), FakeRunner())
    assert code == cli.EXIT_USAGE
    assert "usd_per_pse" in payload["error"]


def test_the_library_returns_what_the_cli_prints(env) -> None:
    from quota_router import run_call

    runner = FakeRunner((0, _stream(), ""))
    deps = cli.Deps(load_snapshots=lambda **kw: (list(_snapshots()), []), run=runner)
    result = run_call(_request(), env=env, now_s=NOW, deps=deps, cwd=_NO_CONFIG_FILES)
    assert result["ok"] is True
    assert result["run_contract"] == 1


def test_the_response_names_the_effort_that_ran(env) -> None:
    # A client checks identity, not liveness: the effort it was served must be readable
    # from the response, and must be the one the child was given.
    runner = FakeRunner((0, _stream(), ""))
    _code, payload, err = _run(env, _request(effort="low"), runner)
    argv = runner.calls[0]["argv"]
    assert argv[argv.index("--effort") + 1] == "low", err
    assert payload["served"]["effort"] == "low"
    assert payload["attempts"][0]["effort"] == "low"


def test_no_effort_is_reported_as_none(env) -> None:
    runner = FakeRunner((0, _stream(), ""))
    _code, payload, _err = _run(env, _request(), runner)
    assert "--effort" not in runner.calls[0]["argv"]
    assert payload["served"]["effort"] is None
    assert payload["attempts"][0]["effort"] is None
