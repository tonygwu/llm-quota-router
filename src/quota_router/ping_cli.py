"""Bounded vendor-CLI pings. No credential reader, HTTP client or API fallback.

A model-selection echo is not served-model evidence. In particular older Codex
JSONL and agy JSON omit the served name: those calls fail telemetry validation,
retain their token counts, and never claim success. See docs/window-pings.md.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .capability import antigravity_flavour
from .providers.antigravity import AgyMissing, agy_argv_env, default_run_agy, signed_in_as
from .providers.codex_app_server import CODEX_BIN_ENV, DEFAULT_CODEX_BIN
from .run import child_env, DEFAULT_CLAUDE_BIN, STRIP_ENV

PROMPT = "1 + 1 = ?"
PING_TIMEOUT_S = 60.0
MODELS = {"claude": "claude-haiku-5-5", "codex": "gpt-6-luna",
          "gemini": "gemini-3.8-flash-low", "claude_gpt": "gpt-oss-120b-medium"}
PING_STRIP_ENV = frozenset(STRIP_ENV) | {"OPENAI_API_KEY", "OPENAI_BASE_URL",
    "CODEX_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"}


class PingFailure(ValueError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def model_for(account):
    if account.provider == "antigravity":
        return MODELS["claude_gpt" if antigravity_flavour(account) == "claude" else "gemini"]
    return MODELS[account.provider]


def _subscription_env(env):
    return {k: v for k, v in env.items() if k.strip().upper() not in PING_STRIP_ENV}


def command_for(account, env, workspace):
    model = model_for(account)
    child = _subscription_env(child_env(env, account.exec_env(), account.config_dir_env_var))
    # API/provider overrides cannot turn a subscription wake-up into credit spend.
    if account.provider in {"claude", "codex"} and not account.config_dir:
        raise PingFailure("configuration", "account has no config directory")
    if account.provider == "claude":
        binary = os.path.expanduser(env.get("CL_CLAUDE_BIN") or DEFAULT_CLAUDE_BIN)
        return [binary, "-p", PROMPT, "--model", model, "--output-format", "json",
                "--tools", "", "--no-session-persistence", "--safe-mode", "--strict-mcp-config"], child
    if account.provider == "codex":
        binary = os.path.expanduser(env.get(CODEX_BIN_ENV) or DEFAULT_CODEX_BIN)
        return [binary, "exec", "--model", model, "--sandbox", "read-only",
                "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--ephemeral",
                "-C", workspace, "-c", 'model_reasoning_effort="low"',
                "-c", 'model_provider="openai"', "--json", PROMPT], child
    if not account.identity_email:
        raise PingFailure("configuration", "Antigravity requires identity_email")
    # Use the exact account-switch recipe used by /usage, including the wrapper.
    argv, child = agy_argv_env(account, ["-p", PROMPT, "--model", model, "--output-format", "json",
        "--log-file", "/dev/stderr", "--mode", "plan", "--sandbox", "--disable-slash-commands",
        "--print-timeout", "60s"], child)
    # The provider helper overlays the account environment again; sanitize last.
    return argv, _subscription_env(child)


def _tokens(raw, keys):
    result = {}
    for key in keys:
        n = raw[key]
        if type(n) is not int or n < 0:
            raise PingFailure("telemetry", f"invalid token count: {key}")
        result[key] = n
    return result


def parse_response(account, completed):
    """Return audit fields even when identity validation fails after a real call."""
    model = model_for(account)
    audit = {"model_asked": model, "model_served": None, "tokens": None,
             "outcome": "failed", "kind": None, "detail": None}
    try:
        if completed.returncode != 0:
            raise PingFailure("cli_exit", f"vendor CLI exited {completed.returncode}")
        if account.provider == "codex":
            events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
            results = [e for e in events if e["type"] == "turn.completed"]
            if len(results) != 1 or any(e["type"] in {"error", "turn.failed"} for e in events):
                raise PingFailure("cli_result", "expected one successful Codex turn")
            result = results[0]
            audit["tokens"] = _tokens(result["usage"], ("input_tokens", "cached_input_tokens", "output_tokens"))
            # Only a response/turn's served stamp is acceptable, never thread config,
            # a startup banner or turn_context.model (all are requested-model echoes).
            served = result.get("model")
            answer = "\n".join(e["item"]["text"] for e in events if e["type"] == "item.completed"
                               and e["item"]["type"] == "agent_message")
        else:
            result = json.loads(completed.stdout)
            if account.provider == "claude":
                if result["type"] != "result" or result["subtype"] != "success" or result["is_error"]:
                    raise PingFailure("cli_result", "Claude did not return a successful result")
                audit["tokens"] = _tokens(result["usage"], ("input_tokens", "output_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens"))
                used = result["modelUsage"]
                served = next(iter(used)) if len(used) == 1 else None
                answer = result["result"]
            else:
                if result["status"] != "SUCCESS" or result["num_turns"] != 1:
                    raise PingFailure("cli_result", "agy did not return one successful turn")
                audit["tokens"] = _tokens(result["usage"], ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens"))
                identities = signed_in_as(completed.stderr)
                if tuple(i.casefold() for i in identities) != (account.identity_email.casefold(),):
                    raise PingFailure("account_identity", "agy sign-in telemetry did not match identity_email")
                # A response model stamp may qualify future CLI versions. Current
                # selected/propagated model overrides do not establish served identity.
                served = result.get("model")
                audit["model_selected"] = sorted(set(re.findall(
                    r'Propagating selected model override to backend: label="([^"]*)"', completed.stderr)))
                answer = result["response"]
        audit["model_served"] = served
        if not served:
            raise PingFailure("model_unverified", "call telemetry contains no unambiguous served model")
        if served != model:
            raise PingFailure("wrong_model", f"asked for {model}, served {served}")
        # Native arithmetic probes returned plain 2, an equation with bold 2, and
        # a bold Answer label. Accept those exact presentation forms, no extra prose.
        if not isinstance(answer, str) or not re.fullmatch(
                r"\s*(?:\*\*Answer:\*\*\s*)?(?:1\s*\+\s*1\s*=\s*)?(?:2|\*\*2\*\*)[.!]?\s*", answer):
            raise PingFailure("answer", "expected the arithmetic answer 2")
        audit.update(outcome="succeeded")
    except PingFailure as exc:
        audit.update(kind=exc.kind, detail=str(exc))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        audit.update(kind="telemetry", detail=f"invalid vendor response: {exc}")
    return audit


def send_ping(account, env):
    """One CLI, one prompt, no retry here. Caller journals and backs off failures."""
    try:
        with tempfile.TemporaryDirectory(prefix="quotapick-ping-", dir="/tmp") as workspace:
            # The configured agy wrapper may switch OS users. The empty workspace
            # is traversable by that user, contains no secrets, and permits no writes.
            os.chmod(workspace, 0o755)
            argv, child = command_for(account, env, workspace)
            # agy_argv_env preserves the vendor's user switch. default_run_agy also
            # terminates the process group on timeout (including the wrapper child).
            # Run every CLI from an empty workspace to avoid project instructions.
            completed = default_run_agy(argv, child, PING_TIMEOUT_S, cwd=workspace)
        return parse_response(account, completed)
    except subprocess.TimeoutExpired:
        kind, detail = "timeout", f"vendor CLI exceeded {PING_TIMEOUT_S:g}s"
    except PingFailure as exc:
        kind, detail = exc.kind, str(exc)
    except (OSError, AgyMissing) as exc:
        kind, detail = "spawn", str(exc)
    return {"outcome": "failed", "kind": kind, "detail": detail,
            "model_asked": model_for(account), "model_served": None, "tokens": None}
