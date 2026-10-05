"""Capability tiers: resolution, debounce, the table, and the refresher.

``pick --capability`` is tested in ``test_capability_pick.py``. Everything here runs on
temp directories and injected I/O: no process is spawned and no real account is read.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from quota_router import capability as cap
from quota_router import capability_refresh as refresh_mod
from quota_router import cli
from quota_router.config import ConfigError, load_config
from tests.test_cli import NOW, _NO_CONFIG_FILES, run

REPO = Path(__file__).resolve().parents[1]
ORG_A = "23554013-0000-4000-8000-00000000000a"
ORG_B = "c6cdaae0-0000-4000-8000-00000000000b"

CATALOG_MODELS = [
    ("claude-opus-5-5", "main"),
    ("claude-fable-5-1", "main"),
    ("claude-sonnet-5-5", "main"),
    ("claude-haiku-4-5-20251001", "main"),
    ("claude-sonnet-5", "overflow"),
    ("claude-opus-5", "overflow"),
    ("claude-fable-5", "overflow"),
    ("claude-opus-4-8", "overflow"),
]

CODEX_LIST = [
    ("gpt-6-astra", False),
    ("gpt-6-sol", False),
    ("gpt-6-luna", False),
    ("gpt-reserve", True),
    ("gpt-5.6-sol", False),
    ("gpt-5.6-terra", False),
    ("gpt-5.6-luna", False),
    ("gpt-5.5", False),
    ("codex-auto-review", True),
]

AGY_OUTPUT = (
    "Fetching available models...\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
    "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
    "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
    "gemini-3.1-pro-low\tGemini 3.1 Pro (Low)\n"
    "claude-opus-5-5-high\tClaude Opus 5.5 (High)\n"
)

MIN = 60.0


# ======================================================================================
# Fixtures
# ======================================================================================


def _write_catalog(config_dir: Path, org: str, models, fetched_s: float, suffix="aaaa") -> Path:
    path = config_dir / "cache" / "model-catalog" / f"{org}-{suffix}-cc.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "fetchedAt": int(fetched_s * 1000),
                "catalog": {
                    "surface": "cc",
                    "config": {"models": [{"id": i, "section": s} for i, s in models]},
                },
            }
        )
    )
    return path


def _identity(path: Path, org: str, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": email, "organizationUuid": org}}))


@pytest.fixture()
def home(tmp_path) -> Path:
    """Account A in Claude Code's real layout, plus claude_b, two Codex homes."""
    root = tmp_path / "home"
    # Account A: identity BESIDE the directory, catalog inside it, and the stray
    # scaffold file inside it with no oauthAccount (exactly what this machine has).
    _identity(root / ".claude.json", ORG_A, "a@example.com")
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / ".claude.json").write_text(json.dumps({"userID": "x", "projects": {}}))
    _write_catalog(root / ".claude", ORG_A, CATALOG_MODELS, NOW - 600)
    # claude_b: identity inside its directory.
    _identity(root / ".claude-b" / ".claude.json", ORG_B, "b@example.com")
    _write_catalog(root / ".claude-b", ORG_B, CATALOG_MODELS, NOW - 600)
    for codex_home in (".codex", ".codex-b"):
        (root / codex_home).mkdir(parents=True, exist_ok=True)
    (root / ".codex" / "config.toml").write_text('model = "gpt-6.1-sol"\n')
    return root


@pytest.fixture()
def env(tmp_path, home) -> dict[str, str]:
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": str(tmp_path / "bin") + ":/usr/bin:/bin",
    }


def _config_file(tmp_path: Path, extra: str = "") -> str:
    path = tmp_path / "fleet.toml"
    path.write_text(
        """
[accounts.claude_c]
enabled = false
[accounts.claude_d]
enabled = false
[accounts.cursor]
enabled = false
[accounts.claude_b]
config_dir = "~/.claude-b"
[accounts.codex_b]
provider = "codex"
config_dir = "~/.codex-b"
[accounts.antigravity_gemini]
provider = "antigravity"
[accounts.antigravity_gemini_b]
provider = "antigravity"
macos_user = "tonyagents"
[accounts.antigravity_claude]
provider = "antigravity"
env = { AGY_MODEL = "claude" }
"""
        + extra
    )
    return str(path)


def _load(env, tmp_path, extra=""):
    return load_config(env=env, cwd=_NO_CONFIG_FILES, explicit_path=_config_file(tmp_path, extra))


class FakeIO:
    """Injected ``model/list`` and process runner, recording every call."""

    def __init__(self, codex=None, agy=AGY_OUTPUT, agy_rc=0, codex_error=None):
        self.codex = codex if codex is not None else {"codex": CODEX_LIST, "codex_b": CODEX_LIST}
        self.agy = agy
        self.agy_rc = agy_rc
        self.codex_error = codex_error
        self.codex_calls: list[str] = []
        self.runs: list[tuple[list[str], dict]] = []

    def codex_model_list(self, account, env, timeout_s):
        self.codex_calls.append(account.account_id)
        if self.codex_error:
            raise self.codex_error
        return {
            "data": [{"id": slug, "hidden": hidden} for slug, hidden in self.codex[account.account_id]],
            "nextCursor": None,
        }

    def run(self, argv, env, timeout_s):
        self.runs.append((list(argv), dict(env)))
        return refresh_mod.Completed(self.agy_rc, self.agy if self.agy_rc == 0 else "", "boom")

    def deps(self):
        return refresh_mod.RefreshDeps(codex_model_list=self.codex_model_list, run=self.run)


@pytest.fixture()
def agy_on_path(tmp_path) -> Path:
    binary = tmp_path / "bin" / "agy"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return binary


def _refresh(env, tmp_path, io, now_s=NOW, extra="", force=False):
    return refresh_mod.refresh(
        _load(env, tmp_path, extra), env=env, now_s=now_s, deps=io.deps(), force=force
    )


def _committed(env, account, capability):
    table, error = cap.read_table(cap.table_path(env))
    assert error is None
    return table["accounts"][account]["capabilities"][capability]["committed"]


# ======================================================================================
# Choosers (pure)
# ======================================================================================


def test_claude_chooser_takes_the_main_section_model_of_each_family():  # T5
    assert cap.choose_claude(CATALOG_MODELS, "haiku")[0] == "claude-haiku-4-5-20251001"
    assert cap.choose_claude(CATALOG_MODELS, "sonnet")[0] == "claude-sonnet-5-5"
    assert cap.choose_claude(CATALOG_MODELS, "opus")[0] == "claude-opus-5-5"
    assert cap.choose_claude(CATALOG_MODELS, "fable")[0] == "claude-fable-5-1"


def test_claude_chooser_never_falls_back_to_the_overflow_section():
    overflow_only = [("claude-opus-5", "overflow")]
    assert cap.choose_claude(overflow_only, "opus") == (None, [])


def test_claude_chooser_warns_when_main_holds_two_of_a_family():
    chosen, warnings = cap.choose_claude(
        [("claude-opus-5", "main"), ("claude-opus-5-5", "main")], "opus"
    )
    assert chosen == "claude-opus-5-5"
    assert warnings and "several opus" in warnings[0]


def test_codex_chooser_follows_sizes_and_ignores_hidden_and_app_slugs():  # T7
    visible = [s for s, hidden in CODEX_LIST if not hidden] + ["gpt-6.1-sol-wm", "gpt-reserve"]
    assert cap.choose_codex(visible, "sol") == ("gpt-6-sol", None)
    assert cap.choose_codex(visible, "astra") == ("gpt-6-astra", None)
    assert cap.choose_codex(visible, "luna") == ("gpt-6-luna", None)
    chosen, note = cap.choose_codex(visible, "terra")
    assert chosen == "gpt-5.6-terra"
    assert note == "no gpt-6 terra; newest terra is gpt-5.6-terra"
    assert cap.choose_codex([*visible, "gpt-6.1-sol"], "sol") == ("gpt-6.1-sol", None)


def test_gemini_chooser_takes_the_newest_high_label_of_a_line():
    entries = [tuple(line.split("\t")) for line in AGY_OUTPUT.splitlines()[1:]]
    assert cap.choose_gemini(entries, "flash") == "Gemini 3.8 Flash (High)"
    assert cap.choose_gemini(entries, "pro") == "Gemini 3.1 Pro (High)"
    assert cap.choose_gemini(entries, "ultra") is None


# ======================================================================================
# Config
# ======================================================================================


def test_a_capabilities_section_is_known_and_merges_over_the_defaults(env, tmp_path):  # T17
    config = _load(env, tmp_path, '[capabilities.premium]\ncodex = "gpt-6-sol"\n')
    assert not [w for w in config.warnings if "capabilities" in w]
    assert config.capabilities["premium"] == {
        "claude": "opus", "codex": "gpt-6-sol", "antigravity": "pro",
    }
    assert config.capabilities["frontier"]["antigravity"] == "none"


@pytest.mark.parametrize(
    "extra, message",
    [
        ('[capabilities.ultra]\nclaude = "opus"\n', "not a capability"),
        ('[capabilities.premium]\ncursor = "x"\n', "unknown provider"),
        ('[capabilities.premium]\ncodex = "opus"\n', "is a claude selector"),
        ('[capabilities.premium]\nclaude = ""\n', "non-empty string"),
    ],
)
def test_a_bad_capabilities_section_fails_loudly(env, tmp_path, extra, message):  # T17
    with pytest.raises(ConfigError, match=message):
        _load(env, tmp_path, extra)


# ======================================================================================
# The refresher
# ======================================================================================


def test_refresh_resolves_every_provider_and_skips_claude_flavour_antigravity(
    env, tmp_path, agy_on_path
):
    io = FakeIO()
    report = _refresh(env, tmp_path, io)

    summary = report.to_dict()
    assert (summary["attempted"], summary["succeeded"], summary["failed"]) == (6, 6, 0)
    skipped = [o for o in report.outcomes if o["outcome"] == "skipped"]
    assert [o["account"] for o in skipped] == ["antigravity_claude"]

    assert _committed(env, "claude", "premium")["id"] == "claude-opus-5-5"
    assert _committed(env, "claude_b", "frontier")["id"] == "claude-fable-5-1"
    assert _committed(env, "antigravity_gemini", "premium")["id"] == "Gemini 3.1 Pro (High)"
    assert _committed(env, "antigravity_gemini", "frontier")["reason"] == (
        "no antigravity model for frontier"
    )


def test_account_a_resolves_from_claude_json_beside_its_dir_without_claude_config_dir(
    env, tmp_path, home, agy_on_path, monkeypatch
):  # T5
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    io = FakeIO()
    _refresh(env, tmp_path, io)

    entry = _committed(env, "claude", "fast")
    assert entry["id"] == "claude-haiku-4-5-20251001"
    assert entry["source"] == "claude_catalog"
    # Nothing was spawned for Claude, and nothing set the variable that would scaffold a
    # fresh empty account inside ~/.claude.
    assert "CLAUDE_CONFIG_DIR" not in os.environ
    assert all("CLAUDE_CONFIG_DIR" not in child_env for _argv, child_env in io.runs)
    # The stray scaffold file is still there and was not what identified the account.
    assert (home / ".claude" / ".claude.json").exists()


def test_codex_premium_uses_the_config_default_when_the_list_lacks_it(env, tmp_path, agy_on_path):
    _refresh(env, tmp_path, FakeIO())  # T7, T8

    codex = _committed(env, "codex", "premium")
    assert (codex["id"], codex["source"], codex["listed"]) == (
        "gpt-6.1-sol", "codex_config_default", False,
    )
    # codex_b has no config.toml: per account, not per provider.
    codex_b = _committed(env, "codex_b", "premium")
    assert (codex_b["id"], codex_b["source"], codex_b["listed"]) == (
        "gpt-6-sol", "codex_model_list", True,
    )
    assert _committed(env, "codex", "standard")["note"] == (
        "no gpt-6 terra; newest terra is gpt-5.6-terra"
    )


def test_a_macos_user_account_lists_models_through_its_launch_wrapper(
    env, tmp_path, agy_on_path
):
    io = FakeIO()
    _refresh(env, tmp_path, io)

    wrapped = [(argv, child) for argv, child in io.runs if argv[0] == "sudo"]
    assert len(wrapped) == 1
    argv, child = wrapped[0]
    assert argv == ["sudo", "-n", "/usr/local/libexec/agy-as-user", "tonyagents",
                    "/usr/local/bin/agy", "models"]
    assert "HOME" not in child
    plain = [argv for argv, _child in io.runs if argv[0] != "sudo"]
    assert plain == [[str(agy_on_path), "models"]]


def test_network_sources_are_read_at_most_every_15_minutes(env, tmp_path, agy_on_path):
    io = FakeIO()
    _refresh(env, tmp_path, io)
    _refresh(env, tmp_path, io, now_s=NOW + 14 * MIN)
    assert io.codex_calls == ["codex", "codex_b"]
    assert len(io.runs) == 2

    _refresh(env, tmp_path, io, now_s=NOW + 15 * MIN)
    assert io.codex_calls == ["codex", "codex_b"] * 2
    assert len(io.runs) == 4


def test_a_failed_model_list_falls_back_to_a_fresh_models_cache(env, tmp_path, home, agy_on_path):
    (home / ".codex-b" / "models_cache.json").write_text(json.dumps({
        "fetched_at": "2026-08-14T20:00:00Z",
        "models": [{"slug": "gpt-6-sol", "visibility": "list"}],
    }))
    io = FakeIO(codex_error=RuntimeError("app-server exited"))
    report = _refresh(env, tmp_path, io, now_s=1_786_744_800.0)  # 2026-08-14T21:20Z

    entry = _committed(env, "codex_b", "premium")
    assert (entry["id"], entry["source"]) == ("gpt-6-sol", "codex_models_cache")
    assert any("model/list failed" in w and "models_cache.json" in w for w in report.warnings)
    # codex has no cache file at all: a reported failure, not a guessed id.
    failed = {o["account"]: o["kind"] for o in report.failed}
    assert failed == {"codex": "rpc_failed_no_cache"}


def test_a_stale_models_cache_is_a_failure_not_an_answer(env, tmp_path, home, agy_on_path):
    (home / ".codex-b" / "models_cache.json").write_text(json.dumps({
        "fetched_at": "2026-08-01T00:00:00Z",
        "models": [{"slug": "gpt-6-sol", "visibility": "list"}],
    }))
    io = FakeIO(codex_error=RuntimeError("timeout"))
    report = _refresh(env, tmp_path, io, now_s=1_786_744_800.0)
    kinds = {o["account"]: o["kind"] for o in report.failed}
    assert kinds["codex_b"] == "rpc_failed_cache_stale"


@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_an_unreadable_catalog_fails_that_account_and_names_the_path(
    env, tmp_path, home, agy_on_path, damage
):  # T9
    catalog_dir = home / ".claude-b" / "cache" / "model-catalog"
    for path in catalog_dir.iterdir():
        if damage == "missing":
            path.unlink()
        else:
            path.write_text("{not json")
    report = _refresh(env, tmp_path, FakeIO())

    failure = {o["account"]: o for o in report.failed}["claude_b"]
    assert failure["kind"] == ("no_catalog" if damage == "missing" else "malformed")
    assert str(catalog_dir) in failure["detail"]
    lookup = cap.lookup(
        cap.read_table(cap.table_path(env))[0], "claude_b", "premium",
        now_s=NOW, path=cap.table_path(env),
    )
    assert isinstance(lookup, cap.Unresolved)
    assert "no entry in the capability table" in lookup.reason


def test_agy_failure_is_reported_with_its_kind(env, tmp_path, agy_on_path):
    report = _refresh(env, tmp_path, FakeIO(agy_rc=1))
    kinds = {o["account"]: o["kind"] for o in report.failed}
    assert kinds == {"antigravity_gemini": "agy_failed", "antigravity_gemini_b": "agy_failed"}


def test_a_refresh_holding_the_lock_makes_a_second_one_skip(env, tmp_path, agy_on_path):
    import fcntl

    path = cap.table_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(".capabilities.lock"), "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = _refresh(env, tmp_path, FakeIO())
    assert report.locked
    assert not path.exists()


# ======================================================================================
# Debounce (T6, T16)
# ======================================================================================


def _codex_lists(io: FakeIO, slugs_b):
    io.codex["codex_b"] = [(s, False) for s in slugs_b]


def test_a_model_that_appears_disappears_and_reappears_moves_once_after_debounce(
    env, tmp_path, agy_on_path
):  # T16
    base = ["gpt-6-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-terra"]
    io = FakeIO()
    _codex_lists(io, base)
    _refresh(env, tmp_path, io, now_s=NOW)
    assert _committed(env, "codex_b", "premium")["id"] == "gpt-6-sol"

    timeline = [
        (15, [*base, "gpt-6.1-sol"]),  # appears
        (30, base),                    # disappears
        (45, [*base, "gpt-6.1-sol"]),  # reappears: the clock starts over here
        (60, [*base, "gpt-6.1-sol"]),  # seen twice, but only 15 minutes
        (74, [*base, "gpt-6.1-sol"]),  # re-read inside 15 min: no new data
    ]
    for minute, slugs in timeline:
        _codex_lists(io, slugs)
        report = _refresh(env, tmp_path, io, now_s=NOW + minute * MIN)
        assert _committed(env, "codex_b", "premium")["id"] == "gpt-6-sol", minute
        assert not report.moves, minute

    _codex_lists(io, [*base, "gpt-6.1-sol"])
    report = _refresh(env, tmp_path, io, now_s=NOW + 75 * MIN)
    assert _committed(env, "codex_b", "premium")["id"] == "gpt-6.1-sol"
    assert [(m["account"], m["capability"], m["from"], m["to"], m["cause"]) for m in report.moves] == [
        ("codex_b", "premium", "gpt-6-sol", "gpt-6.1-sol", "debounced")
    ]
    log = cap.moves_path(cap.table_path(env)).read_text().splitlines()
    assert len(log) == 1 and json.loads(log[0])["to"] == "gpt-6.1-sol"


def test_one_empty_read_does_not_drop_a_working_model(env, tmp_path, agy_on_path):
    io = FakeIO()
    _codex_lists(io, ["gpt-6-sol"])
    _refresh(env, tmp_path, io, now_s=NOW)
    _codex_lists(io, ["gpt-6-astra"])  # sol vanished from one read
    _refresh(env, tmp_path, io, now_s=NOW + 15 * MIN)

    table, _ = cap.read_table(cap.table_path(env))
    entry = table["accounts"]["codex_b"]["capabilities"]["premium"]
    assert entry["committed"]["id"] == "gpt-6-sol"
    assert entry["candidate"]["reason"].startswith("no gpt-*-sol model")


def test_a_claude_release_moves_the_capability_by_itself(env, tmp_path, home, agy_on_path):  # T6
    old = [("claude-opus-5", "main"), ("claude-fable-5", "main"), ("claude-haiku-4-5-20251001", "main"),
           ("claude-sonnet-5", "main")]
    catalog = home / ".claude-b" / "cache" / "model-catalog"
    for path in catalog.iterdir():
        path.unlink()
    _write_catalog(home / ".claude-b", ORG_B, old, NOW - 60)
    _refresh(env, tmp_path, FakeIO(), now_s=NOW)
    assert _committed(env, "claude_b", "premium")["id"] == "claude-opus-5"

    for step, minute in enumerate((10, 40)):
        _write_catalog(home / ".claude-b", ORG_B, CATALOG_MODELS, NOW + minute * MIN)
        _refresh(env, tmp_path, FakeIO(), now_s=NOW + minute * MIN + 1)
    assert _committed(env, "claude_b", "premium")["id"] == "claude-opus-5-5"
    assert _committed(env, "claude_b", "frontier")["id"] == "claude-fable-5-1"


def test_a_config_selector_change_commits_at_once(env, tmp_path, agy_on_path):
    _refresh(env, tmp_path, FakeIO(), now_s=NOW)
    report = _refresh(
        env, tmp_path, FakeIO(), now_s=NOW + MIN,
        extra='[capabilities.premium]\nclaude = "claude-opus-4-8"\n',
    )
    entry = _committed(env, "claude", "premium")
    assert (entry["id"], entry["source"], entry["listed"]) == ("claude-opus-4-8", "config_pin", True)
    assert {m["cause"] for m in report.moves} == {"config"}


# ======================================================================================
# The table, read by pick (T22)
# ======================================================================================


def test_lookup_without_a_table_names_the_path_and_the_fix(tmp_path):
    result = cap.lookup(None, "claude", "premium", now_s=NOW, path=tmp_path / "t.json")
    assert isinstance(result, cap.Unresolved)
    assert str(tmp_path / "t.json") in result.reason
    assert "quotapick capabilities --refresh" in result.reason


def test_lookup_refuses_an_entry_past_its_ttl(env, tmp_path, agy_on_path):
    _refresh(env, tmp_path, FakeIO(), now_s=NOW)
    table, _ = cap.read_table(cap.table_path(env))
    fresh = cap.lookup(table, "claude", "premium", now_s=NOW + 59 * MIN, path=Path("t"))
    assert isinstance(fresh, cap.ModelChoice) and fresh.id == "claude-opus-5-5"
    stale = cap.lookup(table, "claude", "premium", now_s=NOW + 61 * MIN, path=Path("t"))
    assert isinstance(stale, cap.Unresolved)
    assert "1h01m old (TTL 1h00m); is the usage poller running?" in stale.reason


def test_an_unreadable_table_is_an_error_not_an_empty_table(tmp_path):
    path = tmp_path / "capabilities.json"
    path.write_text("{")
    table, error = cap.read_table(path)
    assert table is None and "not valid JSON" in error


# ======================================================================================
# The CLI command
# ======================================================================================


def test_capabilities_command_exits_1_without_a_table_and_0_after_refresh(
    env, tmp_path, agy_on_path
):
    config = _config_file(tmp_path)
    code, out, _ = run(["capabilities", "--config", config], env, cwd=_NO_CONFIG_FILES)
    assert code == 1
    assert "no capability table" in out

    deps = cli.Deps(capability_refresh=FakeIO().deps())
    code, out, err = run(
        ["capabilities", "--refresh", "--json", "--config", config], env, deps=deps,
        cwd=_NO_CONFIG_FILES,
    )
    assert code == 0, err
    assert out.count("\n") == 1  # one line per run: the poller's log is read with tail -1
    payload = json.loads(out)
    assert payload["refresh"]["failed"] == 0
    rows = {row["account"]: row for row in payload["accounts"]}
    assert rows["codex"]["capabilities"]["premium"]["model"]["id"] == "gpt-6.1-sol"
    assert rows["antigravity_claude"]["capabilities"]["premium"]["reason"] == (
        "Claude-flavour Antigravity is out of capability routing"
    )


# ======================================================================================
# Hygiene (T19) and the poller (T24)
# ======================================================================================


def test_capability_modules_are_covered_by_the_token_rotation_scan():
    from tests import test_no_token_rotation as scan

    names = {path.name for path in scan._source_files()}
    assert {"capability.py", "capability_refresh.py"} <= names


def test_the_poller_logs_the_refresh_separately_and_keeps_status_exit_code(tmp_path):  # T24
    fake = tmp_path / "quotapick"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = status ]; then echo \'{"status": true}\'; exit 3; fi\n'
        'if [ "$1" = capabilities ]; then echo "refresh ran $*"; echo oops >&2; exit 1; fi\n'
        "exit 9\n"
    )
    fake.chmod(0o755)
    printed = subprocess.run(
        [str(REPO / "ops" / "install-launchd.sh"), "--print"],
        env={**os.environ, "QUOTAPICK_BIN": str(fake)}, capture_output=True, check=True,
    ).stdout
    program = plistlib.loads(printed)["ProgramArguments"]
    assert program[:2] == ["/bin/sh", "-c"]
    status_log, refresh_log = tmp_path / "usage-poll.log", tmp_path / "capability-refresh.log"
    argv = [*program[:3], str(fake), str(status_log), program[5], str(refresh_log)]

    with open(status_log, "a") as out:  # launchd's StandardOutPath
        done = subprocess.run(argv, stdout=out, stderr=out, check=False)

    assert done.returncode == 3
    assert status_log.read_text() == '{"status": true}\n'
    assert refresh_log.read_text() == "refresh ran capabilities --refresh --json\noops\n"


# ======================================================================================
# doctor
# ======================================================================================


def _doctor(env, now_s):
    from quota_router import doctor

    table, error = cap.read_table(cap.table_path(env))
    accounts = [("claude", "claude"), ("claude_b", "claude"), ("codex", "codex"),
                ("codex_b", "codex"), ("antigravity_gemini", "antigravity")]
    checks = doctor.check_capabilities(
        table=table, table_error=error, path=str(cap.table_path(env)),
        accounts=accounts, now_s=now_s,
    )
    return {check.name: check for check in checks}


def test_doctor_says_not_set_up_without_a_table(env):
    checks = _doctor(env, NOW)
    assert list(checks) == ["capabilities"]
    assert checks["capabilities"].status == "ok"
    assert "not set up" in checks["capabilities"].detail


def test_doctor_reports_each_capability_and_flags_codex_disagreement(env, tmp_path, agy_on_path):
    _refresh(env, tmp_path, FakeIO(), now_s=NOW)
    checks = _doctor(env, NOW + MIN)

    assert checks["capability[fast]"].status == "ok"
    premium = checks["capability[premium]"]
    assert premium.status == "warn"
    assert "codex accounts disagree (gpt-6-sol, gpt-6.1-sol)" in premium.detail
    assert "claude: claude-opus-5-5 (claude, claude_b)" in premium.detail


def test_doctor_fails_once_the_table_is_past_its_ttl(env, tmp_path, agy_on_path):
    _refresh(env, tmp_path, FakeIO(), now_s=NOW)
    checks = _doctor(env, NOW + 61 * MIN)
    assert {c.status for c in checks.values()} == {"fail"}
    assert "ops/install-launchd.sh" in checks["capability[premium]"].remedy


def test_doctor_warns_about_a_recent_move(env, tmp_path, agy_on_path):
    io = FakeIO()
    _refresh(env, tmp_path, io, now_s=NOW)
    io.codex["codex_b"] = [*CODEX_LIST, ("gpt-6.1-sol", False)]
    for minute in (15, 30, 45):
        _refresh(env, tmp_path, io, now_s=NOW + minute * MIN)
    premium = _doctor(env, NOW + 46 * MIN)["capability[premium]"]
    assert "codex_b moved gpt-6-sol -> gpt-6.1-sol" in premium.detail
