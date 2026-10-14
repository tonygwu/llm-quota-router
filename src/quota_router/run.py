"""``quotapick run``: the router executes one tool-less call and says what served it.

A client states what it needs -- a capability or an exact model, a prompt, optionally a
system prompt and a JSON schema -- and the router picks the account, runs the vendor CLI,
checks what actually served the call, and books the cost. The client never maps an
account id to a config dir and never touches a credential.

This version runs Claude subscriptions. The child is ``claude -p`` with no tools, no MCP
servers, no session file and safe mode, from an empty directory, with the prompt on
stdin. Measured 2026-10-14 on Claude Code 2.1.300: that preamble is about 1.1k input
tokens, against a 57k median for the api-shaped ``claude -p`` calls clients make today.

Ranking uses the joint projection with the job in the ``run`` stream, whatever ``[engine]
objective`` says for ``pick``: ``run`` is new, so no caller depends on its ordering, and
API credit pools (next) are invisible to the per-account objective. Without a usable
forecast the decision falls back to ``own_waste`` and says so.

What is checked rather than assumed, from the CLI's own stream-json output:

* auth: the ``init`` event's ``apiKeySource`` must be ``none`` on a subscription run;
* the served model: the requested id must appear in the result's ``modelUsage``;
* a JSON schema's answer: ``structured_output`` must be present.

Exhaustion, auth and transient failures, and a wrong served model, retry on another
account with the failed one excluded; a timeout does not retry. History gets one
``event: "run"`` line with the outcome and cost: never the prompt, never a key.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Final

from . import failure_text
from .config import BANNED_EXEC_ENV

RUN_CONTRACT_VERSION: Final[int] = 1

OUTCOMES: Final[tuple[str, ...]] = (
    "ok",
    "exhausted",
    "wrong_model",
    "auth",
    "transient",
    "timeout",
    "schema_invalid",
    "cli_error",
    "no_candidate",
    "bad_request",
)

#: Failures another account may not share. A timeout is not one: the next account
#: would most likely time out too, and the caller's deadline is already spent.
RETRYABLE: Final[frozenset[str]] = frozenset({"exhausted", "auth", "transient", "wrong_model"})

#: Removed from every child. ``BANNED_EXEC_ENV`` (proxy variables) plus anything that
#: makes Claude Code bill an API key or a cloud provider instead of the account.
STRIP_ENV: Final[tuple[str, ...]] = BANNED_EXEC_ENV + (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

CAPABILITIES: Final[tuple[str, ...]] = ("fast", "standard", "premium", "frontier")
MAX_TIMEOUT_S: Final[float] = 1800.0
DEFAULT_TIMEOUT_S: Final[float] = 300.0
DEFAULT_CLAUDE_BIN: Final[str] = "~/.local/bin/claude"


class BadRequest(ValueError):
    """A request ``run`` refuses before anything is spent."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    prompt: str
    capability: str | None = None
    model: str | None = None
    effort: str | None = None
    system: str | None = None
    json_schema: Mapping[str, Any] | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    expected_cost_usd: float | None = None
    max_attempts: int = 2
    allow_credit: bool = True
    client: str | None = None
    request_id: str | None = None


def parse_request(data: Any) -> RunRequest:
    """Validate a request object. Raises :class:`BadRequest` naming the field."""
    if not isinstance(data, Mapping):
        raise BadRequest("the request must be a JSON object")
    known = {f for f in RunRequest.__dataclass_fields__}
    unknown = sorted(set(data) - known)
    if unknown:
        raise BadRequest(f"unknown field(s): {', '.join(unknown)}")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BadRequest("prompt is required and must be a non-empty string")
    capability, model = data.get("capability"), data.get("model")
    if capability is None and model is None:
        raise BadRequest("give a capability (fast, standard, premium, frontier) or a model")
    if capability is not None and capability not in CAPABILITIES:
        raise BadRequest(f"capability must be one of {', '.join(CAPABILITIES)}")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise BadRequest("model must be a non-empty string")
    schema = data.get("json_schema")
    if schema is not None and not isinstance(schema, Mapping):
        raise BadRequest("json_schema must be a JSON object")
    timeout = data.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (
        0 < timeout <= MAX_TIMEOUT_S
    ):
        raise BadRequest(f"timeout_s must be a number in (0, {MAX_TIMEOUT_S:g}]")
    attempts = data.get("max_attempts", 2)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 3:
        raise BadRequest("max_attempts must be 1, 2 or 3")
    cost = data.get("expected_cost_usd")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))
                             or cost < 0):
        raise BadRequest("expected_cost_usd must be a non-negative number")
    for key in ("system", "effort", "client", "request_id"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise BadRequest(f"{key} must be a string")
    allow_credit = data.get("allow_credit", True)
    if not isinstance(allow_credit, bool):
        raise BadRequest("allow_credit must be true or false")
    return RunRequest(
        prompt=prompt,
        capability=capability,
        model=model.strip() if isinstance(model, str) else None,
        effort=data.get("effort"),
        system=data.get("system"),
        json_schema=dict(schema) if schema is not None else None,
        timeout_s=float(timeout),
        expected_cost_usd=float(cost) if cost is not None else None,
        max_attempts=attempts,
        allow_credit=allow_credit,
        client=data.get("client"),
        request_id=data.get("request_id"),
    )


def build_argv(
    binary: str,
    *,
    model: str,
    effort: str | None,
    system: str | None,
    json_schema: Mapping[str, Any] | None,
) -> list[str]:
    """The child's argv. Never ``--fallback-model``: it changes the served model silently.
    Never ``--max-turns``: a JSON schema answer takes two turns."""
    argv = [
        binary,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--safe-mode",
        "--model",
        model,
    ]
    if effort:
        argv += ["--effort", effort]
    if system:
        argv += ["--system-prompt", system]
    if json_schema is not None:
        argv += ["--json-schema", json.dumps(json_schema, sort_keys=True)]
    return argv


def child_env(base: Mapping[str, str], overlay: Mapping[str, str], config_dir_var: str) -> dict[str, str]:
    """``base`` without anything that could bill a key, plus the account's overlay.

    The config-dir variable is deleted when the overlay does not set it: the default
    account is chosen by its absence, never by inheriting the caller's choice.
    """
    out = {k: v for k, v in base.items() if k.strip().upper() not in STRIP_ENV}
    out = {k: v for k, v in out.items() if not k.startswith("QUOTA_ROUTER_")}
    if config_dir_var not in overlay:
        out.pop(config_dir_var, None)
    out.update({k: v for k, v in overlay.items() if k.strip().upper() not in STRIP_ENV})
    return out


@dataclass(slots=True)
class Parsed:
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    bad_lines: int = 0


def parse_stream(stdout: str) -> Parsed:
    parsed = Parsed()
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parsed.bad_lines += 1
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            parsed.init = event
        elif event.get("type") == "result":
            parsed.result = event
    return parsed


def _normalize_model(model_id: str) -> str:
    return model_id.strip().casefold().removesuffix("[1m]")


@dataclass(slots=True)
class Attempt:
    account: str
    pool_kind: str
    model: str
    outcome: str
    duration_ms: int
    cost_usd: float | None
    detail: str
    text: str | None = None
    structured: Any = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    models_seen: tuple[str, ...] = ()
    auth: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "pool_kind": self.pool_kind,
            "model": self.model,
            "outcome": self.outcome,
            "duration_ms": self.duration_ms,
            "cost_usd": self.cost_usd,
            "detail": self.detail,
        }


def judge(
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    requested_model: str,
    want_structured: bool,
    want_auth: str,
    now_s: float,
) -> tuple[str, str, Parsed]:
    """``(outcome, detail, parsed)`` for one finished child."""
    parsed = parse_stream(stdout)
    result = parsed.result
    if result is None or result.get("is_error") or returncode != 0:
        text = ""
        if result is not None and isinstance(result.get("result"), str):
            text = result["result"]
        text = (text + "\n" + stderr[-2000:]).strip()
        kind = failure_text.classify_failure_text(text, now_s).kind
        if kind in (failure_text.EXHAUSTED_WITH_DEADLINE, failure_text.EXHAUSTED_WITHOUT_DEADLINE):
            return "exhausted", text[:300], parsed
        if kind == failure_text.TRANSIENT:
            return "transient", text[:300], parsed
        lowered = text.casefold()
        if any(s in lowered for s in ("not logged in", "invalid api key", "authentication",
                                      "unauthorized", "please run /login")):
            return "auth", text[:300], parsed
        return "cli_error", (text or f"exit {returncode}, no result event")[:300], parsed
    source = (parsed.init or {}).get("apiKeySource")
    if source != want_auth:
        return "auth", f"apiKeySource is {source!r}, expected {want_auth!r}", parsed
    seen = tuple(sorted((result.get("modelUsage") or {}).keys()))
    if _normalize_model(requested_model) not in {_normalize_model(m) for m in seen}:
        return "wrong_model", f"asked for {requested_model}, served {', '.join(seen) or 'nothing'}", parsed
    if want_structured and result.get("structured_output") is None:
        return "schema_invalid", "no structured_output in the result", parsed
    return "ok", "", parsed


def _claude_binary(env: Mapping[str, str], configured: str | None) -> str:
    explicit = (env.get("QUOTA_ROUTER_CLAUDE_BIN") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    found = shutil.which(configured or "claude", path=env.get("PATH"))
    if found:
        return found
    return os.path.expanduser(DEFAULT_CLAUDE_BIN)


#: Expected cost of one call when the request gives none: generous for a tool-less
#: call (a 50k-token prompt on Opus is about $0.20), so admission keeps a margin.
DEFAULT_EXPECTED_COST_USD: Final[float] = 0.25


@dataclass(slots=True)
class _Target:
    kind: str
    account: str
    model: str
    effort: str | None
    env: dict[str, str]
    argv_extra: list[str]
    want_auth: str
    pool: Any = None
    secret: Any = None
    hold_usd: float = 0.0


def _pool_targets(
    request: RunRequest,
    *,
    config: Any,
    env: Mapping[str, str],
    now_s: float,
    excluded: Sequence[str],
    warnings: list[str],
) -> list[tuple[Any, Any, float]]:
    """Admissible credit pools: ``(pool, balance, expected_usd)``."""
    from . import credits as credits_mod

    lines, problems = credits_mod.read(env)
    warnings.extend(p for p in problems if p not in warnings)
    plan_ends = config.plan_ends()
    expected = (
        request.expected_cost_usd
        if request.expected_cost_usd is not None
        else DEFAULT_EXPECTED_COST_USD
    )
    out = []
    for pool in config.credit_pools.values():
        if not pool.enabled or pool.id in excluded:
            continue
        bal = credits_mod.balance(
            pool, lines, now_s=now_s, plan_end_s=plan_ends.get(pool.linked_account)
        )
        if not bal.available or bal.remaining_usd < expected * pool.admission_margin:
            continue
        out.append((pool, bal, expected))
    return out


def execute(
    request: RunRequest,
    *,
    env: Mapping[str, str],
    now_s: float,
    deps: Any,
    cwd: str | None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Pick, run, check, retry, book. Returns the response object."""
    from . import cli as cli_mod
    from . import credits as credits_mod
    from . import history as history_mod
    from . import joint as joint_mod
    from .config import load_config
    from .secrets import SecretError, read_api_key

    config = load_config(env=env, cwd=cwd, explicit_path=config_path)
    usd_per_pse = config.engine.usd_per_pse
    if usd_per_pse is None:
        raise BadRequest(
            "[engine] usd_per_pse is required for `run`: it converts each call's cost into "
            "PSE for the demand forecast (305 was measured on 2026-10-14)"
        )
    read_secret = getattr(deps, "read_secret", None) or read_api_key
    claude_ids = [
        account.id for account in config.enabled_accounts() if account.provider == "claude"
    ]
    request_id = request.request_id or uuid.uuid4().hex
    attempts: list[Attempt] = []
    excluded: list[str] = []
    warnings: list[str] = []
    decision_info: dict[str, Any] = {}
    started = time.monotonic()
    final: Attempt | None = None
    outcome = "no_candidate"
    binary = _claude_binary(env, config.exec.commands.get("claude"))
    state_dir = credits_mod.ledger_path(env).parent

    for index in range(request.max_attempts):
        remaining_s = request.timeout_s - (time.monotonic() - started)
        if remaining_s <= 1.0:
            outcome = "timeout"
            break
        sub_ids = [a for a in claude_ids if a not in excluded]
        args = SimpleNamespace(
            capability=request.capability,
            effort=request.effort if request.capability else None,
            model=request.model,
            only=sub_ids or claude_ids,
            exclude=None,
            min_remaining=None,
            no_sticky=False,
            timeout_ms=None,
            config=config_path,
            dry_run=False,
            json=True,
            explain=False,
            now=None,
            job_pse=None,
        )
        prepared = cli_mod._prepare(
            args, env, now_s, deps, cwd, event="run", write_state=True,
            objective="joint", job_stream=joint_mod.STREAM_RUN,
        )
        warnings.extend(w for w in prepared.warnings if w not in warnings)
        report = prepared.objective or {}
        decision = prepared.decision
        sub_choice = decision.chosen
        if sub_choice is None or prepared.from_fallback or sub_choice in excluded:
            sub_choice = None
        decision_info = {
            "reason": decision.reason,
            "objective": report.get("used"),
            "fallback": report.get("fallback"),
        }

        def model_for(account_id: str | None) -> tuple[str, str | None]:
            choices = prepared.capability.choices if prepared.capability else {}
            choice = choices.get(account_id or "") or next(iter(choices.values()), None)
            if request.model and not request.capability:
                return request.model, request.effort
            if choice is None:
                return request.model or "", None
            effort = choice.effort.get("applied") if choice.effort else None
            return choice.id, effort

        target: _Target | None = None
        if sub_choice is not None:
            account = prepared.config.account(sub_choice)
            overlay = account.exec_env() if account is not None else {}
            var = account.config_dir_env_var if account is not None else "CLAUDE_CONFIG_DIR"
            model_id, effort = model_for(sub_choice)
            target = _Target("subscription", sub_choice, model_id, effort,
                             child_env(env, overlay, var), [], "none")

        if request.allow_credit and config.credit_pools:
            if report.get("used") != "joint" or prepared.joint is None:
                note = (
                    "credit pools are ranked only by the joint projection, which this run "
                    f"could not use ({report.get('fallback') or 'no forecast'}); they sat out"
                )
                if note not in warnings:
                    warnings.append(note)
            else:
                pools = _pool_targets(request, config=config, env=env, now_s=now_s,
                                      excluded=excluded, warnings=warnings)
                if pools:
                    base = prepared.joint
                    context = joint_mod.JointContext(
                        stocks=list(base.stocks) + [
                            joint_mod.credit_stock(
                                pool.id,
                                remaining_pse=bal.remaining_usd / usd_per_pse,
                                expires_s=bal.expires_s,
                                now_s=now_s,
                            )
                            for pool, bal, _ in pools
                        ],
                        streams=base.streams,
                        now_s=now_s,
                        horizon_s=base.horizon_s,
                        step_s=base.step_s,
                        job_pse=base.job_pse,
                        stream=joint_mod.STREAM_RUN,
                        fable_ids=base.fable_ids,
                        tie_fraction=base.tie_fraction,
                    )
                    best_id, best_score = None, None
                    if sub_choice is not None:
                        best_id, best_score = sub_choice, context.ranked_score(sub_choice)
                    for pool, bal, expected in pools:
                        score = context.ranked_score(pool.id)
                        if score is not None and (best_score is None or score > best_score):
                            best_id, best_score = pool.id, score
                    chosen_pool = next(
                        ((pool, bal, expected) for pool, bal, expected in pools
                         if pool.id == best_id),
                        None,
                    )
                    if chosen_pool is not None:
                        pool, bal, expected = chosen_pool
                        result = context.score(pool.id)
                        decision_info["reason"] = (
                            f"{pool.id} wins: {result.avoided:.4f} of {result.job_pse:.4f} PSE "
                            f"projected fleet waste avoided (API credit, ${bal.remaining_usd:.2f} "
                            f"left, expires {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(bal.expires_s))})"
                        )
                        try:
                            secret = read_secret(pool.keychain_service)
                        except SecretError as exc:
                            warnings.append(f"{pool.id}: {exc}")
                            excluded.append(pool.id)
                            secret = None
                        if secret is not None:
                            home = state_dir / "credit-homes" / pool.id
                            home.mkdir(parents=True, exist_ok=True, mode=0o700)
                            overlay = {"CLAUDE_CONFIG_DIR": str(home),
                                       "ANTHROPIC_API_KEY": secret.reveal()}
                            child = child_env(env, {}, "CLAUDE_CONFIG_DIR")
                            child.update(overlay)
                            model_id, effort = model_for(pool.linked_account)
                            budget = min(bal.remaining_usd, pool.max_usd_per_call)
                            target = _Target(
                                "credit", pool.id, model_id, effort, child,
                                ["--bare", "--max-budget-usd", f"{budget:.2f}"],
                                "ANTHROPIC_API_KEY", pool=pool, secret=secret,
                                hold_usd=expected,
                            )

        if target is None:
            outcome = "no_candidate"
            break
        if not target.model:
            warnings.append(f"{target.account}: no model resolved for this request")
            outcome = "no_candidate"
            break

        run_id = f"{request_id}.{index}"
        scrub = target.secret.scrub if target.secret is not None else (lambda text: text)
        if target.kind == "credit":
            credits_mod.append(env, {"t": now_s, "pool": target.account, "type": "hold",
                                     "run_id": run_id, "usd": target.hold_usd})
        argv = build_argv(binary, model=target.model, effort=target.effort,
                          system=request.system, json_schema=request.json_schema)
        argv += target.argv_extra
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="quota-router-run-") as scratch:
            try:
                completed = deps.run(
                    argv,
                    input=request.prompt,
                    capture_output=True,
                    text=True,
                    env=target.env,
                    cwd=scratch,
                    timeout=remaining_s,
                )
            except Exception as exc:  # noqa: BLE001 - classified below, never raised
                elapsed = int((time.monotonic() - t0) * 1000)
                is_timeout = type(exc).__name__ == "TimeoutExpired"
                attempt = Attempt(
                    target.account, target.kind, target.model,
                    "timeout" if is_timeout else "cli_error", elapsed, None,
                    scrub(f"{type(exc).__name__}: {exc}")[:300],
                )
                attempts.append(attempt)
                final = attempt
                outcome = attempt.outcome
                if target.kind == "credit":
                    # A timed-out call may still have spent: book the hold as spent.
                    credits_mod.append(env, {
                        "t": now_s, "pool": target.account,
                        "type": "spend" if is_timeout else "release", "run_id": run_id,
                        **({"usd": target.hold_usd, "estimated": True} if is_timeout else {}),
                    })
                if is_timeout:
                    break
                excluded.append(target.account)
                continue
        elapsed = int((time.monotonic() - t0) * 1000)
        outcome, detail, parsed = judge(
            completed.returncode,
            scrub(completed.stdout or ""),
            scrub(completed.stderr or ""),
            requested_model=target.model,
            want_structured=request.json_schema is not None,
            want_auth=target.want_auth,
            now_s=now_s,
        )
        result = parsed.result or {}
        cost = result.get("total_cost_usd")
        cost_usd = float(cost) if isinstance(cost, (int, float)) else None
        attempt = Attempt(
            account=target.account,
            pool_kind=target.kind,
            model=target.model,
            outcome=outcome,
            duration_ms=elapsed,
            cost_usd=cost_usd,
            detail=detail,
            text=result.get("result") if isinstance(result.get("result"), str) else None,
            structured=result.get("structured_output"),
            usage=result.get("usage") or {},
            models_seen=tuple(sorted((result.get("modelUsage") or {}).keys())),
            auth=(parsed.init or {}).get("apiKeySource"),
        )
        attempts.append(attempt)
        final = attempt
        if target.kind == "credit":
            if cost_usd is not None and cost_usd > 0:
                credits_mod.append(env, {"t": now_s, "pool": target.account, "type": "spend",
                                         "run_id": run_id, "usd": cost_usd,
                                         "model": target.model, "outcome": outcome})
            else:
                credits_mod.append(env, {"t": now_s, "pool": target.account,
                                         "type": "release", "run_id": run_id})
            if outcome == "exhausted":
                credits_mod.append(env, {"t": now_s, "pool": target.account,
                                         "type": "exhausted", "signal": detail[:200]})
        if outcome == "ok" or outcome not in RETRYABLE:
            break
        excluded.append(target.account)

    ok = final is not None and final.outcome == "ok"
    cost_usd = final.cost_usd if final is not None else None
    cost_pse = cost_usd / usd_per_pse if cost_usd is not None else None
    served = None
    if final is not None:
        served = {
            "account": final.account,
            "provider": "claude" if final.pool_kind == "subscription" else "anthropic_api",
            "pool_kind": final.pool_kind,
            "model": final.models_seen[0] if len(final.models_seen) == 1 else final.model,
            "model_requested": final.model,
            "models_seen": list(final.models_seen),
            "auth": {"none": "oauth", "ANTHROPIC_API_KEY": "api_key"}.get(
                final.auth or "", final.auth
            ),
        }
    fable = bool(final is not None and "fable" in final.model.casefold())
    history_mod.append_snapshots(
        [],
        now_s=now_s,
        event="run",
        run={
            "request_id": request_id,
            "outcome": final.outcome if final is not None else outcome,
            "group": "claude",
            "pool_kind": final.pool_kind if final is not None else None,
            "account": final.account if final is not None else None,
            "model": final.model if final is not None else None,
            "cost_usd": cost_usd,
            "cost_pse": cost_pse,
            "fable": fable,
            "attempts": len(attempts),
            "client": request.client,
        },
        env=env,
    )
    return {
        "run_contract": RUN_CONTRACT_VERSION,
        "ok": ok,
        "outcome": final.outcome if final is not None else outcome,
        "text": final.text if ok and final is not None else None,
        "structured": final.structured if ok and final is not None else None,
        "served": served,
        "usage": dict(final.usage) if final is not None else {},
        "cost_usd": cost_usd,
        "cost_pse": cost_pse,
        "attempts": [a.to_dict() for a in attempts],
        "decision": decision_info,
        "warnings": warnings,
        "request_id": request_id,
        "client": request.client,
    }


def bad_request_payload(error: str, request_id: str | None = None) -> dict[str, Any]:
    return {
        "run_contract": RUN_CONTRACT_VERSION,
        "ok": False,
        "outcome": "bad_request",
        "error": error,
        "request_id": request_id,
    }
