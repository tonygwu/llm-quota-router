"""Antigravity quota, read from ``agy -p "/usage" --output-format json``.

One ``/usage`` call serves both pools of one Google account. ``status`` (the launchd
poller) makes the call; ``pick`` serves the cached reading and never spawns agy.

Every test here injects the agy runner, or replaces the module default with a recorder.
The one real process this file starts is a Python child in the timeout test, never agy.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from quota_router import cli
from quota_router.codex_launcher import codex_accounts
from quota_router.config import load_config
from quota_router.launcher import _interactive_accounts
from quota_router.providers import antigravity as agy_mod
from quota_router.providers.antigravity import (
    USAGE_ARGS,
    AgyCompleted,
    AntigravityAdapter,
)
from quota_router.types import SOURCE_CACHE, SOURCE_LIVE, UNKNOWN_TIER_CAPACITY
from tests.test_cli import _NO_CONFIG_FILES, run

FIXTURE = Path(__file__).parent / "fixtures" / "agy_usage_raw.json"
RAW = FIXTURE.read_text(encoding="utf-8")


def _epoch(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


#: The fixture was captured when its unstarted 5-hour window reset at now + 5h.
NOW = _epoch("2026-10-12T07:09:46Z")

USAGE_ARGV = ["-p", "/usage", "--output-format", "json"]

FOUR_POOLS = (
    '[accounts.antigravity_gemini]\nprovider = "antigravity"\n'
    'identity_email = "a@example.com"\n'
    '[accounts.antigravity_claude]\nprovider = "antigravity"\n'
    'env = { AGY_MODEL = "claude" }\n'
    'identity_email = "a@example.com"\n'
    '[accounts.antigravity_gemini_b]\nprovider = "antigravity"\n'
    'command = "/usr/local/bin/agy"\nmacos_user = "tonyagents"\n'
    'identity_email = "b@example.com"\n'
    '[accounts.antigravity_claude_b]\nprovider = "antigravity"\n'
    'command = "/usr/local/bin/agy"\nmacos_user = "tonyagents"\n'
    'env = { AGY_MODEL = "claude" }\n'
    'identity_email = "b@example.com"\n'
)


# ======================================================================================
# Fixtures
# ======================================================================================


@pytest.fixture()
def env(tmp_path) -> dict[str, str]:
    """A temp HOME and state dir, a fake agy on PATH, and no real Codex binary."""
    agy = tmp_path / "bin" / "agy"
    agy.parent.mkdir(parents=True, exist_ok=True)
    agy.write_text("#!/bin/sh\nexit 99\n")
    agy.chmod(0o755)
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "QUOTA_ROUTER_CODEX_BIN": str(tmp_path / "no-codex-here"),
    }


def _config(tmp_path, text: str = FOUR_POOLS, *, name: str = "fleet.toml"):
    path = tmp_path / name
    path.write_text(
        "[accounts.claude_c]\nenabled = false\n[accounts.claude_d]\nenabled = false\n"
        "[accounts.cursor]\nenabled = false\n" + text,
        encoding="utf-8",
    )
    return path


def _accounts(env, tmp_path, text: str = FOUR_POOLS):
    config = load_config(env=env, cwd=_NO_CONFIG_FILES, explicit_path=_config(tmp_path, text))
    return config, tuple(
        account for account in config.enabled_accounts() if account.provider == "antigravity"
    )


class Recorder:
    """An agy runner that records every call and answers from a script."""

    def __init__(self, stdout: str = RAW, returncode: int = 0, error: BaseException | None = None):
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict[str, str], float]] = []
        self._lock = threading.Lock()

    def __call__(self, argv, env, timeout_s):
        with self._lock:
            self.calls.append((list(argv), dict(env), timeout_s))
        if self.error is not None:
            raise self.error
        return AgyCompleted(self.returncode, self.stdout, "")


def _adapter(env, accounts, runner, *, live=True, **kwargs) -> AntigravityAdapter:
    return AntigravityAdapter(
        accounts=accounts, live=live, run_agy=runner, env=env,
        which=lambda name: "/fake/bin/agy", **kwargs,
    )


def _by_id(snapshots):
    return {snapshot.id: snapshot for snapshot in snapshots}


def _windows(snapshot):
    return {
        w.key: (round(w.used_fraction, 12), w.length_s, w.resets_at_s) for w in snapshot.windows
    }


def _mutated(mutate) -> str:
    payload = json.loads(RAW)
    mutate(payload)
    return json.dumps(payload)


# ======================================================================================
# Mapping (requirement 1)
# ======================================================================================


def test_the_fixture_maps_each_group_to_its_pool(env, tmp_path):
    _config_obj, accounts = _accounts(env, tmp_path)
    runner = Recorder()
    adapter = _adapter(env, accounts[:2], runner)  # the calling user's Google account

    snaps = _by_id(adapter.snapshot(NOW))

    assert len(runner.calls) == 1, "one /usage call serves both pools of one account"
    assert _windows(snaps["antigravity_gemini"]) == {
        "7d": (0.0, 604_800.0, _epoch("2026-10-19T05:07:06Z")),
        "5h": (0.0, 18_000.0, _epoch("2026-10-12T12:09:46Z")),
    }
    assert _windows(snaps["antigravity_claude"]) == {
        "7d": (round(1 - 0.9782077670097351, 12), 604_800.0, _epoch("2026-10-13T16:01:20Z")),
        "5h": (0.0, 18_000.0, _epoch("2026-10-12T12:09:46Z")),
    }
    for snapshot in snaps.values():
        assert snapshot.source == SOURCE_LIVE
        assert snapshot.confidence == 1.0
        assert snapshot.available is True
        assert all(w.observed_at_s == NOW for w in snapshot.windows)
        # No tier mapping exists for Antigravity: the neutral unknown-tier capacity.
        assert snapshot.tier == "unknown"
        assert snapshot.capacity == UNKNOWN_TIER_CAPACITY
    assert adapter.warnings == ()


def test_a_tier_set_in_config_is_used(env, tmp_path):
    text = FOUR_POOLS.replace(
        '[accounts.antigravity_gemini]\nprovider = "antigravity"\n',
        '[accounts.antigravity_gemini]\nprovider = "antigravity"\ntier = "pro"\n',
    )
    _config_obj, accounts = _accounts(env, tmp_path, text)
    snaps = _by_id(_adapter(env, accounts[:2], Recorder()).snapshot(NOW))
    assert snaps["antigravity_gemini"].tier == "pro"
    assert snaps["antigravity_claude"].tier == "unknown"


def test_four_pools_on_two_google_accounts_take_two_reads_at_once(env, tmp_path):
    _config_obj, accounts = _accounts(env, tmp_path)
    # Both reads must be in flight together, or the barrier breaks.
    barrier = threading.Barrier(2, timeout=5)
    recorder = Recorder()

    def runner(argv, child_env, timeout_s):
        barrier.wait()
        return recorder(argv, child_env, timeout_s)

    snaps = _by_id(_adapter(env, accounts, runner).snapshot(NOW))

    assert len(recorder.calls) == 2
    assert {s.id for s in snaps.values() if s.windows} == {
        "antigravity_gemini", "antigravity_claude",
        "antigravity_gemini_b", "antigravity_claude_b",
    }


def test_the_second_account_is_read_through_its_launch_plan(env, tmp_path):
    """The argv is the launch-plan recipe plus the /usage arguments, all three parts."""
    config, accounts = _accounts(env, tmp_path)
    recorder = Recorder()
    _adapter({**env, "HOME": "/Users/caller"}, accounts, recorder).snapshot(NOW)

    wrapped = [(argv, child) for argv, child, _t in recorder.calls if argv[0] == "sudo"]
    assert len(wrapped) == 1, "one read for the second account's two pools"
    argv, child = wrapped[0]
    assert argv == [
        "sudo", "-n", "/usr/local/libexec/agy-as-user", "tonyagents",
        "/usr/local/bin/agy", "-p", "/usage", "--output-format", "json",
    ]
    plan = cli._launch_plan(config.account("antigravity_gemini_b"))
    assert argv[: len(plan["argv_prefix"])] == plan["argv_prefix"]
    assert plan["unset_env"] == ["HOME"]
    assert "HOME" not in child, "an inherited HOME points agy at the caller's profile"
    for key, value in plan["env"].items():
        assert child[key] == value

    plain = [argv for argv, _child, _t in recorder.calls if argv[0] != "sudo"]
    assert plain == [["/fake/bin/agy", *USAGE_ARGV]]
    assert list(USAGE_ARGS) == USAGE_ARGV


# ======================================================================================
# Strict mapping: never guess (requirement 1)
# ======================================================================================


def test_an_unknown_group_is_named_and_its_pool_gets_no_windows(env, tmp_path):
    def rename(payload):
        payload["command"]["data"]["groups"][1]["name"] = "Claude, GPT and Grok models"

    _config_obj, accounts = _accounts(env, tmp_path)
    adapter = _adapter(env, accounts[:2], Recorder(_mutated(rename)))
    snaps = _by_id(adapter.snapshot(NOW))

    assert snaps["antigravity_gemini"].windows, "the known group still maps"
    assert snaps["antigravity_claude"].windows == ()
    text = "\n".join(adapter.warnings)
    assert "'Claude, GPT and Grok models'" in text, text
    assert "'Claude and GPT models'" in text, text
    assert "antigravity_claude" in text


def test_a_group_with_no_name_is_a_warning(env, tmp_path):
    def unname(payload):
        del payload["command"]["data"]["groups"][0]["name"]

    _config_obj, accounts = _accounts(env, tmp_path)
    adapter = _adapter(env, accounts[:2], Recorder(_mutated(unname)))
    snaps = _by_id(adapter.snapshot(NOW))

    assert snaps["antigravity_gemini"].windows == ()
    assert snaps["antigravity_claude"].windows
    assert any("no name" in w for w in adapter.warnings), adapter.warnings


def test_an_unknown_bucket_window_is_a_warning_not_a_silent_drop(env, tmp_path):
    def daily(payload):
        payload["command"]["data"]["groups"][0]["buckets"].append(
            {"id": "gemini-daily", "window": "daily", "remaining_fraction": 0.0,
             "reset_time": "2026-10-13T00:00:00Z"}
        )

    _config_obj, accounts = _accounts(env, tmp_path)
    adapter = _adapter(env, accounts[:2], Recorder(_mutated(daily)))
    snaps = _by_id(adapter.snapshot(NOW))

    # Dropping the window would make the pool look healthier than it is.
    assert snaps["antigravity_gemini"].windows == ()
    assert any("'daily'" in w and "antigravity_gemini" in w for w in adapter.warnings)
    assert snaps["antigravity_claude"].windows


# ======================================================================================
# Fail loud on identity and shape (requirement 2)
# ======================================================================================


@pytest.mark.parametrize(
    ("runner", "cause"),
    [
        (Recorder(_mutated(lambda p: p.update(status="ERROR"))), "status"),
        (Recorder(_mutated(lambda p: p.update(num_turns=1))), "num_turns"),
        (Recorder(_mutated(lambda p: p.pop("num_turns"))), "num_turns"),
        (Recorder(_mutated(lambda p: p["command"].update(name="chat"))), "command.name"),
        (Recorder(returncode=75, stdout=""), "exited 75"),
        (Recorder(stdout="You are not logged into Antigravity"), "not JSON"),
    ],
    ids=["status", "num_turns", "no-num_turns", "command", "exit", "not-json"],
)
def test_a_response_that_is_not_the_usage_command_yields_no_windows(env, tmp_path, runner, cause):
    _config_obj, accounts = _accounts(env, tmp_path)
    adapter = _adapter(env, accounts[:2], runner)
    snaps = _by_id(adapter.snapshot(NOW))

    assert all(s.windows == () for s in snaps.values())
    assert all(s.confidence == 0.0 for s in snaps.values())
    joined = "\n".join(adapter.warnings)
    assert cause in joined, joined
    assert "antigravity_gemini" in joined and "antigravity_claude" in joined


def test_a_failure_learned_deadline_still_benches_a_measured_pool(env, tmp_path):
    _config_obj, accounts = _accounts(env, tmp_path)
    adapter = _adapter(
        env, accounts[:2], Recorder(), exhausted_until={"antigravity_gemini": NOW + 600}
    )
    snaps = _by_id(adapter.snapshot(NOW))
    assert snaps["antigravity_gemini"].available is False
    assert snaps["antigravity_claude"].available is True


def test_a_timeout_yields_no_windows_and_names_it(env, tmp_path):
    _config_obj, accounts = _accounts(env, tmp_path)
    runner = Recorder(error=subprocess.TimeoutExpired(["agy"], 10.0))
    adapter = _adapter(env, accounts[:2], runner, timeout_s=10.0)
    snaps = _by_id(adapter.snapshot(NOW))

    assert all(s.windows == () for s in snaps.values())
    assert runner.calls[0][2] == 10.0, "the adapter passes its timeout to the runner"
    assert any("timed out after 10s" in w for w in adapter.warnings), adapter.warnings


def test_the_default_runner_kills_the_whole_process_group_on_timeout(tmp_path):
    """sudo runs agy as a grandchild, so killing only the direct child is not enough."""
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        agy_mod.default_run_agy([sys.executable, "-c", script], dict(os.environ), 1.5)
    assert time.monotonic() - started < 6.0

    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(grandchild, 9)
        pytest.fail("the grandchild outlived the timeout")


# ======================================================================================
# The cache, and the pick path (requirement 3)
# ======================================================================================


def explode(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(f"agy was spawned: {args!r}")


def test_without_live_reads_the_adapter_serves_the_cache_and_spawns_nothing(env, tmp_path):
    _config_obj, accounts = _accounts(env, tmp_path)
    _adapter(env, accounts[:2], Recorder()).snapshot(NOW)  # status writes the cache

    adapter = _adapter(env, accounts[:2], explode, live=False)
    snaps = _by_id(adapter.snapshot(NOW + 90))

    for snapshot in snaps.values():
        assert snapshot.source == SOURCE_CACHE
        assert snapshot.windows
        assert snapshot.staleness_s(NOW + 90) == 90
        assert "90s old" in (snapshot.note or "")


def test_without_a_cache_the_pick_path_reports_no_reading(env, tmp_path):
    _config_obj, accounts = _accounts(env, tmp_path)
    adapter = _adapter(env, accounts[:2], explode, live=False)
    snaps = _by_id(adapter.snapshot(NOW))
    assert all(s.windows == () for s in snaps.values())
    assert any("quotapick status" in w for w in adapter.warnings), adapter.warnings


def test_a_second_status_inside_the_ttl_serves_the_cache(env, tmp_path):
    _config_obj, accounts = _accounts(env, tmp_path)
    first = Recorder()
    _adapter(env, accounts[:2], first).snapshot(NOW)
    second = Recorder()
    snaps = _by_id(_adapter(env, accounts[:2], second).snapshot(NOW + 60))
    assert second.calls == []
    assert {s.source for s in snaps.values()} == {SOURCE_CACHE}

    third = Recorder()
    snaps = _by_id(_adapter(env, accounts[:2], third).snapshot(NOW + 121))
    assert len(third.calls) == 1
    assert {s.source for s in snaps.values()} == {SOURCE_LIVE}


def test_a_cached_reading_from_another_launch_recipe_is_not_served(env, tmp_path):
    """Moving a pool to another macOS user must not serve the old account's numbers."""
    _config_obj, accounts = _accounts(env, tmp_path)
    _adapter(env, accounts[:2], Recorder()).snapshot(NOW)

    moved = FOUR_POOLS.replace(
        '[accounts.antigravity_gemini]\nprovider = "antigravity"\n',
        '[accounts.antigravity_gemini]\nprovider = "antigravity"\nmacos_user = "other"\n',
    )
    _config_obj, accounts = _accounts(env, tmp_path, moved)
    adapter = _adapter(env, accounts[:2], explode, live=False)
    snaps = _by_id(adapter.snapshot(NOW + 30))
    assert snaps["antigravity_gemini"].windows == ()
    assert snaps["antigravity_claude"].windows
    assert any("launch recipe" in w for w in adapter.warnings), adapter.warnings


def test_a_failed_live_read_reaches_the_pick_path_too(env, tmp_path):
    _config_obj, accounts = _accounts(env, tmp_path)
    _adapter(env, accounts[:2], Recorder()).snapshot(NOW)
    _adapter(env, accounts[:2], Recorder(returncode=75, stdout="")).snapshot(NOW + 200)

    adapter = _adapter(env, accounts[:2], explode, live=False)
    snaps = _by_id(adapter.snapshot(NOW + 230))
    assert all(s.windows == () for s in snaps.values())
    assert any("exited 75" in w for w in adapter.warnings), adapter.warnings


def test_the_offline_flag_keeps_status_from_spawning(env, tmp_path):
    _config_obj, accounts = _accounts(env, tmp_path)
    adapter = _adapter({**env, "QUOTA_ROUTER_USAGE_OFFLINE": "1"}, accounts[:2], explode)
    adapter.snapshot(NOW)


def test_pick_never_spawns_agy_and_status_does(env, tmp_path, monkeypatch):
    """Through the loader the CLI really runs: only `status` may wait on agy."""
    recorder = Recorder()
    monkeypatch.setattr(agy_mod, "default_run_agy", recorder)
    config = str(_config(tmp_path, FOUR_POOLS.split("[accounts.antigravity_gemini_b]")[0]))
    deps = cli.Deps()

    for argv in (
        ["pick", "--dry-run", "--json"],
        ["pick", "--dry-run", "--json", "--capability", "standard"],
        ["explain"],
    ):
        code, _out, err = run([argv[0], "--config", config, *argv[1:]], env, deps=deps,
                              now_s=NOW, cwd=_NO_CONFIG_FILES)
        assert code in (cli.EXIT_OK, 1), (argv, err)
        assert recorder.calls == [], f"{argv[0]} spawned agy"

    code, out, err = run(["status", "--config", config, "--json"], env, deps=deps,
                         now_s=NOW, cwd=_NO_CONFIG_FILES)
    assert code == cli.EXIT_OK, err
    assert len(recorder.calls) == 1, "status reads the account once for both pools"
    rows = {row["id"]: row for row in json.loads(out)["accounts"]}
    assert rows["antigravity_gemini"]["source"] == SOURCE_LIVE
    assert {w["key"] for w in rows["antigravity_claude"]["windows"]} == {"5h", "7d"}

    code, out, err = run(["pick", "--config", config, "--dry-run", "--json"], env,
                         deps=deps, now_s=NOW + 30, cwd=_NO_CONFIG_FILES)
    assert len(recorder.calls) == 1, "pick spawned agy"
    payload = json.loads(out)
    seen = {row["account"] for row in payload["ranked"] + payload["excluded"]}
    assert {"antigravity_gemini", "antigravity_claude"} <= seen


# ======================================================================================
# Routing (requirement 5)
# ======================================================================================


def test_cl_and_cdx_never_offer_an_antigravity_pool(env, tmp_path):
    config, _accounts_ = _accounts(env, tmp_path)
    assert not [a for a in _interactive_accounts(config) if a.startswith("antigravity")]
    assert not [a for a in codex_accounts(config) if a.startswith("antigravity")]
