"""The live Codex source: ``codex app-server`` answering ``account/rateLimits/read``.

Session transcripts only exist for sessions that wrote one. A caller running
``codex exec --ephemeral`` writes none, so its spend never reached the router: one
account's transcript reading was fifteen hours old and said 0% while the account had
spent 1%. The app server asks the backend directly, so it is the live reading, and the
transcript tails stay as the fallback.

No test here runs the real ``codex`` binary. Each one writes a small Python program that
speaks the app server's newline-delimited JSON-RPC and is spawned in its place. Its
behaviour is read from ``scenario.json`` inside the ``CODEX_HOME`` it is started with,
so two homes can answer differently and a test can see which home a child was given.
Every payload is synthetic.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from quota_router.types import SOURCE_CACHE, SOURCE_LIVE, TIER_PRO

#: 2026-09-15T00:00:00Z. The reset instants below sit a few days after it.
NOW = 1789430400.0
WEEKLY_RESETS_AT = 1790047672
SPARK_5H_RESETS_AT = 1789441200
SPARK_7D_RESETS_AT = 1789900000

ACCOUNT_UUID = "00000000-0000-4000-8000-00000000c0de"


def _module():
    """Imported per test, so a missing module fails each test on its own line."""
    from quota_router.providers import codex_app_server

    return codex_app_server


# --------------------------------------------------------------------------------------
# The fake app server
# --------------------------------------------------------------------------------------

FAKE_SERVER = r'''
import json, os, signal, subprocess, sys, time

home = os.environ["CODEX_HOME"]
with open(os.path.join(home, "scenario.json")) as handle:
    scenario = json.load(handle)

def log(entry):
    with open(os.path.join(home, "fake.log"), "a") as handle:
        handle.write(json.dumps(entry) + "\n")

log({"pid": os.getpid(), "argv": sys.argv[1:], "path": os.environ.get("PATH", "")})

if scenario.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if scenario.get("grandchild"):
    child = subprocess.Popen(
        [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]
    )
    log({"grandchild": child.pid})
if scenario.get("exit_now") is not None:
    sys.stderr.write(scenario.get("stderr", ""))
    sys.stderr.flush()
    sys.exit(scenario["exit_now"])

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method = message.get("method")
    log({"method": method, "id": message.get("id")})
    if method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "fake/0.0.0"}})
        continue
    if "id" not in message:
        continue
    if method == "account/rateLimits/read":
        if scenario.get("hang"):
            time.sleep(60)
        time.sleep(scenario.get("delay_s", 0))
        for notification in scenario.get("before", []):
            send(notification)
        if "raw" in scenario:
            sys.stdout.write(scenario["raw"] + "\n")
            sys.stdout.flush()
        elif "error" in scenario:
            send({"id": message["id"], "error": scenario["error"]})
        else:
            send({"id": message["id"], "result": scenario["result"]})
        continue
    send({"id": message["id"], "error": {"code": -32600, "message": "Invalid request: unknown variant `" + str(method) + "`"}})
'''


def write_fake_server(root: Path) -> Path:
    """An executable stand-in for ``codex``, run by the test interpreter."""
    path = root / "bin" / "codex"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{FAKE_SERVER}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def window(used: int, minutes: int | None, resets_at: int | None) -> dict[str, Any]:
    return {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": resets_at}


def rate_limits_result(*, used: int = 1, plan: str | None = "pro") -> dict[str, Any]:
    """The verified response shape, with synthetic numbers."""
    codex = {
        "limitId": "codex",
        "limitName": None,
        "primary": window(used, 10080, WEEKLY_RESETS_AT),
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": None},
        "planType": plan,
        "rateLimitReachedType": None,
    }
    spark = {
        "limitId": "codex_bengalfox",
        "limitName": "GPT-5.3-Codex-Spark",
        "primary": window(0, 300, SPARK_5H_RESETS_AT),
        "secondary": window(4, 10080, SPARK_7D_RESETS_AT),
        "credits": None,
        "planType": plan,
        "rateLimitReachedType": None,
    }
    return {
        "rateLimits": codex,
        "rateLimitsByLimitId": {"codex": codex, "codex_bengalfox": spark},
        "accountId": ACCOUNT_UUID,
        "rateLimitResetCredits": None,
        "rateLimitUpsell": None,
    }


def fake_id_token(email: str, account_id: str, plan: str) -> str:
    def encode(payload: dict[str, Any]) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return raw.rstrip("=")

    claims = {
        "email": email,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id, "chatgpt_plan_type": plan},
    }
    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


def make_home(root: Path, name: str, scenario: dict[str, Any], *, email: str | None = None) -> Path:
    home = root / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    if email is not None:
        token = fake_id_token(email, ACCOUNT_UUID, "pro")
        (home / "auth.json").write_text(
            json.dumps({"tokens": {"id_token": token, "access_token": "not-a-token"}}),
            encoding="utf-8",
        )
    return home


def fake_log(home: Path) -> list[dict[str, Any]]:
    path = home / "fake.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def child_pids(home: Path) -> list[int]:
    return [
        entry[key] for entry in fake_log(home) for key in ("pid", "grandchild") if key in entry
    ]


def assert_all_gone(pids: list[int], *, within_s: float = 3.0) -> None:
    """Every pid has exited. A reparented grandchild is reaped by init, so poll."""
    deadline = time.monotonic() + within_s
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        still = []
        for pid in alive:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:  # pragma: no cover - pid reused by another user
                continue
            still.append(pid)
        alive = still
        if alive:
            time.sleep(0.05)
    assert not alive, f"left running: {alive}"


def adapter_for(tmp_path: Path, homes: dict[str, Path], **kwargs: Any):
    module = _module()
    binary = kwargs.pop("codex_bin", None) or write_fake_server(tmp_path)
    return module.CodexAppServerAdapter(
        codex_accounts=[
            module.CodexAccountConfig(account_id=account_id, codex_home=home)
            for account_id, home in homes.items()
        ],
        codex_bin=str(binary),
        env={"PATH": "/usr/bin:/bin"},
        **kwargs,
    )


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def test_the_live_read_produces_the_transcript_adapters_window_shape(tmp_path: Path) -> None:
    """Same keys as the transcript adapter, so status columns and routing keep their shape."""
    home = make_home(tmp_path, "codex", {"result": rate_limits_result(used=1)}, email="a@example.com")
    adapter = adapter_for(tmp_path, {"codex": home})

    snapshots = adapter.snapshot(NOW)

    assert [s.id for s in snapshots] == ["codex"], adapter.warnings
    snapshot = snapshots[0]
    assert snapshot.source == SOURCE_LIVE
    assert snapshot.tier == TIER_PRO
    assert snapshot.confidence == pytest.approx(1.0)
    windows = {w.key: w for w in snapshot.windows}
    assert set(windows) == {"7d", "codex_bengalfox", "codex_bengalfox#secondary"}

    weekly = windows["7d"]
    assert weekly.used_fraction == pytest.approx(0.01)
    assert weekly.length_s == pytest.approx(7 * 86400)
    assert weekly.resets_at_s == pytest.approx(WEEKLY_RESETS_AT)
    assert weekly.observed_at_s == pytest.approx(NOW)
    assert weekly.applies_to is None

    spark = windows["codex_bengalfox"]
    assert spark.length_s == pytest.approx(5 * 3600)
    assert spark.resets_at_s == pytest.approx(SPARK_5H_RESETS_AT)
    assert {"codex_bengalfox", "bengalfox", "gpt-5.3-codex-spark"} <= spark.applies_to
    spark_weekly = windows["codex_bengalfox#secondary"]
    assert spark_weekly.used_fraction == pytest.approx(0.04)
    assert spark_weekly.length_s == pytest.approx(7 * 86400)

    assert snapshot.identity is not None
    assert snapshot.identity.email == "a@example.com"
    assert snapshot.identity.organization_uuid == ACCOUNT_UUID


def test_the_child_is_spawned_as_app_server_in_the_accounts_home(tmp_path: Path) -> None:
    """``CODEX_HOME`` selects the account; the protocol order is initialize, initialized, read.

    The binary's own directory leads the child's PATH. The Homebrew ``codex`` is a
    ``#!/usr/bin/env node`` shim, and launchd's minimal PATH has no ``node``: measured,
    the shim exits 127 there.
    """
    home = make_home(tmp_path, "codex", {"result": rate_limits_result()})
    binary = write_fake_server(tmp_path)
    adapter = adapter_for(tmp_path, {"codex": home}, codex_bin=binary)

    assert adapter.snapshot(NOW), adapter.warnings

    log = fake_log(home)
    assert log[0]["argv"] == ["app-server"]
    assert log[0]["path"].split(os.pathsep)[0] == str(binary.parent)
    methods = [entry["method"] for entry in log if "method" in entry]
    assert methods == ["initialize", "initialized", "account/rateLimits/read"]
    assert_all_gone(child_pids(home))


def test_notifications_before_the_response_are_skipped(tmp_path: Path) -> None:
    before = [
        {"method": "remoteControl/status/changed", "params": {"status": "disabled", "host": "example-host"}},
        {"method": "account/rateLimits/updated", "params": {"rateLimits": {"limitId": "codex"}}},
    ]
    home = make_home(tmp_path, "codex", {"before": before, "result": rate_limits_result(used=7)})
    adapter = adapter_for(tmp_path, {"codex": home})

    snapshots = adapter.snapshot(NOW)

    assert [s.id for s in snapshots] == ["codex"], adapter.warnings
    assert {w.key: w for w in snapshots[0].windows}["7d"].used_fraction == pytest.approx(0.07)
    assert not any("example-host" in warning for warning in adapter.warnings)


def test_a_limit_with_no_window_is_named_and_contributes_nothing(tmp_path: Path) -> None:
    result = rate_limits_result()
    result["rateLimitsByLimitId"]["premium"] = {
        "limitId": "premium",
        "limitName": None,
        "primary": None,
        "secondary": None,
        "planType": "pro",
    }
    home = make_home(tmp_path, "codex", {"result": result})
    adapter = adapter_for(tmp_path, {"codex": home})

    snapshots = adapter.snapshot(NOW)

    assert {w.key for w in snapshots[0].windows} == {"7d", "codex_bengalfox", "codex_bengalfox#secondary"}
    assert any("codex" in w and "premium" in w for w in adapter.warnings), adapter.warnings


# --------------------------------------------------------------------------------------
# Fail loud: no live snapshot, a warning naming the account and the cause
# --------------------------------------------------------------------------------------


def _assert_no_snapshot_and_warned(adapter, account_id: str, *needles: str) -> None:
    snapshots = adapter.snapshot(NOW)
    assert snapshots == [], snapshots
    matching = [w for w in adapter.warnings if account_id in w]
    assert matching, adapter.warnings
    for needle in needles:
        assert any(needle in w for w in matching), (needle, adapter.warnings)


def test_an_error_response_yields_no_snapshot_and_a_warning(tmp_path: Path) -> None:
    home = make_home(
        tmp_path, "codex_b", {"error": {"code": -32603, "message": "not signed in to ChatGPT"}}
    )
    adapter = adapter_for(tmp_path, {"codex_b": home})
    _assert_no_snapshot_and_warned(adapter, "codex_b", "not signed in to ChatGPT")
    assert_all_gone(child_pids(home))


def test_an_unknown_method_error_yields_no_snapshot_and_a_warning(tmp_path: Path) -> None:
    """An older binary without ``account/rateLimits/read`` rejects it as an unknown variant."""
    home = make_home(
        tmp_path,
        "codex",
        {"error": {"code": -32600, "message": "Invalid request: unknown variant `account/rateLimits/read`"}},
    )
    adapter = adapter_for(tmp_path, {"codex": home})
    _assert_no_snapshot_and_warned(adapter, "codex", "unknown variant")


@pytest.mark.parametrize(
    ("label", "scenario", "needle"),
    [
        ("not json", {"raw": "this is not json"}, "malformed"),
        ("no rateLimits", {"result": {"accountId": ACCOUNT_UUID}}, "rateLimits"),
        (
            "fractional percent",
            {"result": {"rateLimits": {"limitId": "codex", "primary": window(1, 10080, WEEKLY_RESETS_AT) | {"usedPercent": "1"}}}},
            "usedPercent",
        ),
        (
            "null duration",
            {"result": {"rateLimits": {"limitId": "codex", "primary": window(1, None, WEEKLY_RESETS_AT)}}},
            "windowDurationMins",
        ),
        (
            "null reset",
            {"result": {"rateLimits": {"limitId": "codex", "primary": window(1, 10080, None)}}},
            "resetsAt",
        ),
        (
            "no window at all",
            {"result": {"rateLimits": {"limitId": "codex", "primary": None, "secondary": None}}},
            "no window",
        ),
    ],
)
def test_a_malformed_payload_yields_no_snapshot_and_names_the_cause(
    tmp_path: Path, label: str, scenario: dict[str, Any], needle: str
) -> None:
    """Never guess a window length or a reset: the transcript reading serves instead."""
    home = make_home(tmp_path, "codex", scenario)
    adapter = adapter_for(tmp_path, {"codex": home})
    _assert_no_snapshot_and_warned(adapter, "codex", needle)


def test_a_child_that_exits_early_is_reported_with_its_stderr(tmp_path: Path) -> None:
    home = make_home(tmp_path, "codex", {"exit_now": 127, "stderr": "env: node: No such file or directory"})
    adapter = adapter_for(tmp_path, {"codex": home})
    _assert_no_snapshot_and_warned(adapter, "codex", "exited", "node: No such file")


def test_a_timeout_kills_the_child_and_warns(tmp_path: Path) -> None:
    """A hung server costs the budget once and leaves nothing behind, grandchildren included.

    The Homebrew shim runs the native server as its own child and cannot forward
    SIGKILL, so the whole process group is signalled. This child also ignores SIGTERM.
    """
    home = make_home(tmp_path, "codex", {"hang": True, "ignore_term": True, "grandchild": True})
    adapter = adapter_for(tmp_path, {"codex": home}, timeout_s=0.8)

    started = time.monotonic()
    _assert_no_snapshot_and_warned(adapter, "codex", "timed out")
    elapsed = time.monotonic() - started

    assert elapsed < 2.5, f"the timeout did not bound the read: {elapsed:.2f}s"
    pids = child_pids(home)
    assert len(pids) == 2, fake_log(home)
    assert_all_gone(pids)


def test_a_missing_binary_warns_and_spawns_nothing(tmp_path: Path) -> None:
    home = make_home(tmp_path, "codex", {"result": rate_limits_result()})
    missing = tmp_path / "nowhere" / "codex"
    adapter = adapter_for(tmp_path, {"codex": home}, codex_bin=missing)

    _assert_no_snapshot_and_warned(adapter, "codex", str(missing), "QUOTA_ROUTER_CODEX_BIN")
    assert fake_log(home) == []


def test_the_binary_is_never_looked_up_on_path(tmp_path: Path) -> None:
    """Resolution is explicit: argument, then ``QUOTA_ROUTER_CODEX_BIN``, then the default."""
    module = _module()
    explicit = module.CodexAppServerAdapter(codex_bin="/x/codex", env={"QUOTA_ROUTER_CODEX_BIN": "/y/codex"})
    from_env = module.CodexAppServerAdapter(env={"QUOTA_ROUTER_CODEX_BIN": "/y/codex", "PATH": str(tmp_path)})
    default = module.CodexAppServerAdapter(env={"PATH": str(tmp_path)})

    assert explicit.codex_bin() == "/x/codex"
    assert from_env.codex_bin() == "/y/codex"
    assert default.codex_bin() == module.DEFAULT_CODEX_BIN
    assert os.path.isabs(module.DEFAULT_CODEX_BIN)


def test_offline_mode_spawns_nothing(tmp_path: Path) -> None:
    """``QUOTA_ROUTER_USAGE_OFFLINE`` means touch nothing slow, as for the Claude reader."""
    module = _module()
    home = make_home(tmp_path, "codex", {"result": rate_limits_result()})
    adapter = module.CodexAppServerAdapter(
        codex_accounts=[module.CodexAccountConfig(account_id="codex", codex_home=home)],
        codex_bin=str(write_fake_server(tmp_path)),
        env={"PATH": "/usr/bin:/bin", "QUOTA_ROUTER_USAGE_OFFLINE": "1"},
    )
    assert adapter.snapshot(NOW) == []
    assert fake_log(home) == []


# --------------------------------------------------------------------------------------
# Several accounts
# --------------------------------------------------------------------------------------


def test_two_homes_are_read_concurrently_with_distinct_results(tmp_path: Path) -> None:
    delay = 0.7
    a = make_home(tmp_path, "codex", {"delay_s": delay, "result": rate_limits_result(used=30)}, email="a@example.com")
    b = make_home(tmp_path, "codex_b", {"delay_s": delay, "result": rate_limits_result(used=2)}, email="b@example.com")
    adapter = adapter_for(tmp_path, {"codex": a, "codex_b": b}, timeout_s=5.0)

    started = time.monotonic()
    snapshots = adapter.snapshot(NOW)
    elapsed = time.monotonic() - started

    assert [s.id for s in snapshots] == ["codex", "codex_b"], adapter.warnings
    by_id = {s.id: {w.key: w for w in s.windows} for s in snapshots}
    assert by_id["codex"]["7d"].used_fraction == pytest.approx(0.30)
    assert by_id["codex_b"]["7d"].used_fraction == pytest.approx(0.02)
    assert {s.id: s.identity.email for s in snapshots} == {"codex": "a@example.com", "codex_b": "b@example.com"}
    assert elapsed < 2 * delay, f"read in sequence, not concurrently: {elapsed:.2f}s"
    assert_all_gone(child_pids(a) + child_pids(b))


def test_one_failing_home_does_not_hide_the_other(tmp_path: Path) -> None:
    a = make_home(tmp_path, "codex", {"result": rate_limits_result(used=30)})
    b = make_home(tmp_path, "codex_b", {"error": {"code": -32603, "message": "boom"}})
    adapter = adapter_for(tmp_path, {"codex": a, "codex_b": b})

    assert [s.id for s in adapter.snapshot(NOW)] == ["codex"]
    assert any("codex_b" in w and "boom" in w for w in adapter.warnings), adapter.warnings


# --------------------------------------------------------------------------------------
# The merge and the CLI
# --------------------------------------------------------------------------------------


def _transcript_home(home: Path, used_percent: float) -> None:
    day = home / "sessions" / "2026" / "09" / "14"
    day.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": "2026-09-14T09:00:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "limit_name": None,
                "primary": {"used_percent": used_percent, "window_minutes": 10080, "resets_at": WEEKLY_RESETS_AT},
                "secondary": None,
                "plan_type": "pro",
            },
        },
    }
    (day / "rollout.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_the_live_snapshot_wins_the_merge_over_the_transcript(tmp_path: Path) -> None:
    from quota_router.providers import CodexSessionsAdapter, collect_snapshots

    module = _module()
    home = make_home(tmp_path, "codex_b", {"result": rate_limits_result(used=1)})
    _transcript_home(home, used_percent=0.0)
    accounts = [module.CodexAccountConfig(account_id="codex_b", codex_home=home)]

    snapshots, warnings = collect_snapshots(
        [
            CodexSessionsAdapter(codex_accounts=accounts, read_identity=False),
            adapter_for(tmp_path, {"codex_b": home}),
        ],
        NOW,
    )

    assert [s.id for s in snapshots] == ["codex_b"]
    assert snapshots[0].source == SOURCE_LIVE, warnings
    assert {w.key: w for w in snapshots[0].windows}["7d"].used_fraction == pytest.approx(0.01)

    # And when the live read fails, the transcript reading still serves.
    (home / "scenario.json").write_text(json.dumps({"error": {"code": 1, "message": "down"}}))
    fallback, warnings = collect_snapshots(
        [
            adapter_for(tmp_path, {"codex_b": home}),
            CodexSessionsAdapter(codex_accounts=accounts, read_identity=False),
        ],
        NOW,
    )
    assert [s.source for s in fallback] == [SOURCE_CACHE]
    assert any("codex_b" in w and "down" in w for w in warnings), warnings


def _codex_config(tmp_path: Path, homes: dict[str, Path]):
    from quota_router.config import AccountConfig, Config

    return Config(
        accounts={
            account_id: AccountConfig(id=account_id, config_dir=str(home), provider="codex")
            for account_id, home in homes.items()
        }
    )


def test_build_default_adapters_puts_the_live_reader_before_the_transcripts(tmp_path: Path) -> None:
    from quota_router.providers import CodexSessionsAdapter, build_default_adapters

    module = _module()
    a = make_home(tmp_path, "codex", {"result": rate_limits_result()})
    b = make_home(tmp_path, "codex_b", {"result": rate_limits_result()})
    adapters = build_default_adapters(config=_codex_config(tmp_path, {"codex": a, "codex_b": b}), env={}, home=tmp_path)

    kinds = [type(adapter) for adapter in adapters]
    assert module.CodexAppServerAdapter in kinds
    assert kinds.index(module.CodexAppServerAdapter) < kinds.index(CodexSessionsAdapter)
    live = adapters[kinds.index(module.CodexAppServerAdapter)]
    assert [(acc.account_id, acc.codex_home) for acc in live.accounts()] == [("codex", a), ("codex_b", b)]


def test_the_cli_rebuild_keeps_the_accounts_and_caps_the_timeout(tmp_path: Path) -> None:
    """The oracle timeout (5s by default) may shorten the live read, never stretch it
    past the adapter's own cap, which exists to fit the 3s launcher deadline."""
    from quota_router import cli, providers

    module = _module()
    a = make_home(tmp_path, "codex", {"result": rate_limits_result()})
    b = make_home(tmp_path, "codex_b", {"result": rate_limits_result()})
    config = _codex_config(tmp_path, {"codex": a, "codex_b": b})

    def rebuilt(timeout_s):
        adapters = cli._configure_adapters(
            providers,
            providers.build_default_adapters(),
            config=config,
            env={"HOME": str(tmp_path)},
            run=None,
            timeout_s=timeout_s,
        )
        return next(x for x in adapters if isinstance(x, module.CodexAppServerAdapter))

    live = rebuilt(5.0)
    assert [acc.account_id for acc in live.accounts()] == ["codex", "codex_b"]
    assert live.timeout_s == pytest.approx(module.DEFAULT_TIMEOUT_S)
    assert rebuilt(0.5).timeout_s == pytest.approx(0.5)
    assert module.DEFAULT_TIMEOUT_S < 3.0


def test_status_json_reports_source_live_for_the_codex_account(tmp_path: Path) -> None:
    """End to end through ``quotapick status --json``, with only the Codex adapters
    running (the Claude reader would touch this machine's Keychain)."""
    import io

    from quota_router import cli, providers

    module = _module()
    home = make_home(tmp_path, "codex_b", {"result": rate_limits_result(used=1)})
    _transcript_home(home, used_percent=0.0)
    binary = write_fake_server(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[accounts.codex_b]\nprovider = "codex"\nconfig_dir = "{home}"\n', encoding="utf-8"
    )
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": "/usr/bin:/bin",
        "QUOTA_ROUTER_CONFIG": str(config_path),
        "QUOTA_ROUTER_CODEX_BIN": str(binary),
    }

    def load_snapshots(**kwargs):
        codex_only = [
            adapter
            for adapter in providers.build_default_adapters()
            if isinstance(adapter, (module.CodexAppServerAdapter, providers.CodexSessionsAdapter))
        ]
        adapters = cli._configure_adapters(
            providers,
            codex_only,
            config=kwargs["config"],
            env=kwargs["env"],
            run=None,
            timeout_s=kwargs["timeout_s"],
        )
        return providers.collect_snapshots(adapters, kwargs["now_s"])

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        ["status", "--json"],
        env=env,
        stdout=out,
        stderr=err,
        now_s=NOW,
        deps=cli.Deps(load_snapshots=load_snapshots),
    )
    assert code == cli.EXIT_OK, err.getvalue()
    payload = json.loads(out.getvalue())
    text = json.dumps(payload)
    assert '"codex_b"' in text, text
    sources = _sources_for(payload, "codex_b")
    assert sources and set(sources) == {SOURCE_LIVE}, (sources, text[:2000])
    assert_all_gone(child_pids(home))


def _sources_for(node: Any, account_id: str) -> list[str]:
    """Every ``source`` recorded next to ``account_id`` anywhere in the payload."""
    found: list[str] = []
    if isinstance(node, dict):
        if account_id in (node.get("account"), node.get("id")) and isinstance(node.get("source"), str):
            found.append(node["source"])
        for value in node.values():
            found.extend(_sources_for(value, account_id))
    elif isinstance(node, list):
        for value in node:
            found.extend(_sources_for(value, account_id))
    return found
