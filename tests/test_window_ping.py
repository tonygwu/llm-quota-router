"""Idle-window decisions use observation time and never send from a dry run."""
from dataclasses import replace
import json

import pytest

from quota_router.types import AccountSnapshot, Window, NotRunningWindow
from quota_router.providers.claude_oauth import parse_usage_payload
from quota_router.history import record_to_snapshots
from quota_router.window_ping import waiting_reason, plan_pings, run_pings
from quota_router.config import AccountConfig, Config

NOW = 1_800_000_000.0
H5 = 18000.0
D7 = 604800.0


def win(key="5h", *, used=0, reset=None, observed=NOW, scoped=None):
    length = H5 if key == "5h" else D7
    return Window(key=key, used_fraction=used, length_s=length,
                  resets_at_s=observed + length if reset is None else reset,
                  observed_at_s=observed, applies_to=scoped)


def snap(account="claude", windows=None, **kw):
    return AccountSnapshot(id=account, source="live", windows=(win(),) if windows is None else windows, **kw)


def cfg(*ids):
    return Config(accounts={id: AccountConfig(id=id, config_dir="/synthetic/"+id,
                  env={"AGY_MODEL": "claude"} if "antigravity_claude" in id else {}) for id in ids})


def rec(*snapshots, t=NOW):
    return {"t": t, "event": "status", "accounts": [s.to_dict() for s in snapshots]}


def test_idle_marker_survives_history_without_fake_window():
    idle = []
    windows, warnings = parse_usage_payload({"limits": [{"kind": "session", "percent": 0,
        "is_active": False, "resets_at": None}]}, observed_at_s=NOW, not_running=idle)
    assert not warnings and not windows
    assert idle == [NotRunningWindow("5h", H5, NOW)]
    s = snap(windows=(), not_running=tuple(idle))
    assert record_to_snapshots(rec(s))[0].not_running == s.not_running


@pytest.mark.parametrize("provider", ["claude", "codex", "antigravity_gemini"])
def test_same_waiting_test_for_all_providers(provider):
    assert plan_pings(cfg(provider), [rec(snap(provider))], [], NOW)[0]["action"] == "ping"
    assert plan_pings(cfg(provider), [rec(snap(provider, available=False))], [], NOW)[0]["action"] == "skip"


def test_waiting_uses_observation_not_record_time_and_excludes_scoped():
    assert waiting_reason(win(observed=NOW-1000), []) == "reset_near_observation_plus_length"
    assert waiting_reason(win(reset=NOW+H5+241), []) is None
    assert waiting_reason(win(scoped=frozenset({"fable"})), []) is None
    assert waiting_reason(win(used=.001), []) is None


def test_fixed_fresh_window_after_missed_first_poll():
    old = win(used=.9, observed=NOW-4000, reset=NOW-3000)
    new = win(reset=old.resets_at_s+H5)
    assert waiting_reason(new, [old]) == "previous_reset_plus_length"
    assert waiting_reason(new, []) is None


def test_success_cooldown_is_shared_across_windows_and_early_resets():
    s = snap(windows=(win(), win("7d")))
    events = [{"event": "result", "pool": "claude", "at_s": NOW-6*3600,
               "outcome": "succeeded", "attempt_id": "x", "windows": []}]
    p = plan_pings(cfg("claude"), [rec(s)], events, NOW)[0]
    assert p["action"] == "ping"
    assert [w["key"] for w in p["eligible_windows"]] == ["5h"]
    events[0]["at_s"] = NOW-1000
    assert plan_pings(cfg("claude"), [rec(s)], events, NOW)[0]["action"] == "skip"


def test_dry_run_sends_and_writes_nothing(tmp_path):
    path = tmp_path / "missing" / "pings.jsonl"
    def forbidden(*args):
        pytest.fail("dry run spawned")
    result = run_pings(cfg("claude"), [rec(snap())], path, now_s=NOW,
                       dry_run=True, env={}, send=forbidden)
    assert result["would_ping"] == 1
    assert result["attempted"] == result["succeeded"] == result["failed"] == 0
    assert not path.parent.exists()


def test_separate_antigravity_pools_and_no_credit_pool():
    ids = ("antigravity_gemini", "antigravity_claude")
    p = plan_pings(cfg(*ids), [rec(*(snap(id) for id in ids), snap("anthropic_api_x"))], [], NOW)
    assert [r["pool"] for r in p if r["action"] == "ping"] == list(ids)


def test_stale_read_does_not_trigger():
    p = plan_pings(cfg("claude"), [rec(snap(windows=(win(observed=NOW-601),)))], [], NOW)
    assert p[0]["action"] == "skip"


def success(*args):
    return {"outcome": "succeeded", "kind": None, "model_served": "claude-haiku-5-5",
            "tokens": {"input_tokens": 1, "output_tokens": 1}}


def test_failed_ping_retries_after_backoff_and_success_blocks(tmp_path):
    path = tmp_path / "pings.jsonl"
    failed = lambda *args: {"outcome": "failed", "kind": "timeout"}
    args = dict(config=cfg("claude"), records=[rec(snap())], path=path, now_s=NOW,
                dry_run=False, env={}, clock=lambda: NOW)
    report = run_pings(**args, send=failed)
    assert (report["attempted"], report["succeeded"], report["failed_by_kind"]) == (1, 0, {"timeout": 1})
    report = run_pings(**args, send=success)
    assert report["attempted"] == 0
    later = NOW+1800
    args.update(now_s=later, records=[rec(snap(windows=(win(observed=later),)), t=later)], clock=lambda: later)
    assert run_pings(**args, send=success)["succeeded"] == 1
    assert run_pings(**args, send=success)["attempted"] == 0


def test_failed_verification_blocks_only_one_window_and_survives_restart(tmp_path):
    path = tmp_path / "pings.jsonl"
    config = cfg("claude")
    run_pings(config, [rec(snap())], path, now_s=NOW, dry_run=False, env={}, send=success, clock=lambda: NOW)
    later = NOW+601
    # Five-hour window still slides; weekly became waiting later and was not a trigger.
    records = [rec(snap(windows=(win(observed=later), win("7d", observed=later))), t=later)]
    report = run_pings(config, records, path, now_s=later, dry_run=False, env={}, send=success, clock=lambda: later)
    assert report["verification"][0]["outcome"] == "blocked"
    assert report["warnings"]
    much_later = NOW+D7+1
    records = [rec(snap(windows=(win(observed=much_later), win("7d", observed=much_later))), t=much_later)]
    report = run_pings(config, records, path, now_s=much_later, dry_run=True, env={})
    assert report["would_ping"] == 1
    assert [w["key"] for w in report["plans"][0]["eligible_windows"]] == ["7d"]
    resumed = run_pings(config, records, path, now_s=much_later, dry_run=False, env={}, resume="claude:5h")
    assert resumed["attempted"] == 0 and not resumed["warnings"]


def test_cached_read_is_not_verification_and_rounded_zero_waits_for_anchor(tmp_path):
    path = tmp_path / "pings.jsonl"
    config = cfg("claude")
    run_pings(config, [rec(snap())], path, now_s=NOW, dry_run=False, env={}, send=success, clock=lambda: NOW)
    assert not run_pings(config, [rec(snap(), t=NOW+120)], path, now_s=NOW+120, env={})["verification"]
    records = [rec(snap(windows=(win(reset=NOW+H5, observed=NOW+120),)), t=NOW+120)]
    assert not run_pings(config, records, path, now_s=NOW+120, env={})["verification"]
    records = [rec(snap(windows=(win(reset=NOW+H5, observed=NOW+360),)), t=NOW+360)]
    r = run_pings(config, records, path, now_s=NOW+360, env={})
    assert r["verification"][0]["outcome"] == "verified"


def test_bad_journal_and_concurrent_writer_fail_closed(tmp_path):
    import fcntl
    path = tmp_path / "pings.jsonl"
    path.write_text('{"event": "result"}\n')
    with pytest.raises((ValueError, KeyError)):
        run_pings(cfg("claude"), [rec(snap())], path, now_s=NOW, env={}, send=success)
    path.unlink()
    with path.with_suffix(".lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="lock"):
            run_pings(cfg("claude"), [rec(snap())], path, now_s=NOW, dry_run=False, env={}, send=success)
    assert not path.exists()


def test_disabled_expired_and_exhausted_companion_are_skipped():
    config = cfg("claude", "codex")
    config = replace(config, accounts={"claude": replace(config.accounts["claude"], plan_ends_s=NOW)})
    assert plan_pings(config, [rec(snap())], [], NOW)[0]["action"] == "skip"
    p = plan_pings(cfg("claude"), [rec(snap(windows=(win(), win("7d", used=1))))], [], NOW)[0]
    assert p["reason"] == "another_pool_window_exhausted"


def test_cli_dry_run_from_history_and_launchd_default(tmp_path):
    import io
    import subprocess
    from quota_router.cli import main
    from quota_router.history import history_path
    env = {"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    path = history_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(rec(snap()))+"\n")
    out = io.StringIO()
    assert main(["window-ping", "--dry-run", "--json"], env=env, now_s=NOW, stdout=out) == 0
    assert json.loads(out.getvalue())["would_ping"] == 1
    assert sorted(p.name for p in path.parent.iterdir()) == ["history.jsonl"]
    result = subprocess.run(["bash", "ops/install-launchd.sh", "--print"], env={**env, "PATH": "/usr/bin:/bin", "QUOTAPICK_BIN": "/usr/bin/true"}, capture_output=True, text=True)
    import plistlib
    plist = plistlib.loads(result.stdout.encode())
    assert plist["ProgramArguments"][-1] == "dry-run"
    assert 'window-ping "--$5" --json' in plist["ProgramArguments"][2]
    assert plist["StartInterval"] == 120


def test_first_conclusive_reading_cannot_be_replaced_by_later_recovery(tmp_path):
    path = tmp_path / "pings.jsonl"
    config = cfg("claude")
    run_pings(config, [rec(snap())], path, now_s=NOW, dry_run=False, env={}, send=success, clock=lambda: NOW)
    bad = snap(windows=(), not_running=(NotRunningWindow("5h", H5, NOW+120),))
    recovered = snap(windows=(win(used=.01, reset=NOW+H5, observed=NOW+360),))
    report = run_pings(config, [rec(bad, t=NOW+120), rec(recovered, t=NOW+360)], path, now_s=NOW+360, env={})
    assert report["verification"][0]["outcome"] == "blocked"


def test_corrupt_latest_history_is_not_skipped(tmp_path):
    from quota_router.history import iter_records
    p = tmp_path / "history.jsonl"
    p.write_text(json.dumps(rec(snap()))+'\n{"broken":\n')
    with pytest.raises(ValueError):
        list(iter_records(p, strict=True))


def test_idle_marker_does_not_accept_a_clamped_negative_percentage():
    idle = []
    _, warnings = parse_usage_payload({"limits": [{"kind": "session", "percent": -3,
        "is_active": False, "resets_at": None}]}, observed_at_s=NOW, not_running=idle)
    assert not idle and warnings
