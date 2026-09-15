"""Tests for :mod:`quota_router.codex_launcher` -- the ``cdx`` launcher.

``cdx`` is ``cl`` for Codex. What it shares with ``cl`` (a capped pick, a launch on
every failure path, an honest banner) is pinned the same way. What is Codex-specific
is pinned by the tests in the last two sections:

1. **Nothing is injected.** The operator's own callers pass ``--sandbox read-only``,
   ``--approve-for-me`` and ``--dangerously-bypass-approvals-and-sandbox`` by design;
   in clap the last flag wins, so any injected default would silently override the
   most restrictive of them.

2. **A session is routed by the home that holds it.** ``codex`` can only resume a
   transcript under its own ``CODEX_HOME``; a router that scored the other account
   would resume nothing, or worse, the wrong "latest" session.

Nothing here spawns a process or reads the operator's real machine.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest

from quota_router import codex_launcher as cdx
from quota_router.api import Selection
from quota_router.config import AccountConfig, Config


# ======================================================================================
# Builders
# ======================================================================================


def _row(account: str, *, fits: bool, score: float = 1.0) -> dict:
    return {
        "account": account,
        "fits": fits,
        "score": score,
        "eligible": True,
        "remaining": 0.5 if fits else 0.0,
    }


def selection(*ranked: dict, chosen: str | None = None) -> Selection:
    rows = list(ranked)
    if chosen is None and rows:
        chosen = rows[0]["account"]
    fits = next((r["fits"] for r in rows if r["account"] == chosen), False)
    return Selection.from_payload(
        {
            "contract_version": 1,
            "decision": {"account": chosen, "provider": "codex", "fits": fits},
            "exec": {"env": {}, "argv_prefix": [], "unset_env": []},
            "ranked": rows,
            "excluded": [],
            "warnings": [],
        }
    )


class Exec:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def __call__(self, path, argv, env):
        self.calls.append((path, list(argv), dict(env)))

    @property
    def path(self) -> str:
        return self.calls[-1][0]

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][1]

    @property
    def env(self) -> dict[str, str]:
        return self.calls[-1][2]


@pytest.fixture()
def home(tmp_path) -> Path:
    root = tmp_path / "home"
    (root / ".codex").mkdir(parents=True)
    (root / ".codex-b").mkdir(parents=True)
    return root


@pytest.fixture()
def env(home) -> dict[str, str]:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "CDX_CODEX_BIN": "/usr/bin/true"}


@pytest.fixture()
def config(home) -> Config:
    """Two Codex accounts plus a Claude one that must never be a candidate."""
    return Config(
        accounts={
            "claude": AccountConfig(id="claude", config_dir=str(home / ".claude")),
            "codex": AccountConfig(id="codex", config_dir=str(home / ".codex")),
            "codex_b": AccountConfig(
                id="codex_b", config_dir=str(home / ".codex-b"), provider="codex"
            ),
        }
    )


def launch(argv, env, config, *, answer=None, error=None, select=None):
    executor = Exec()
    err = io.StringIO()
    calls: list[dict] = []

    if select is None:
        def select(**kwargs):
            calls.append(kwargs)
            if error is not None:
                raise error
            return answer if answer is not None else selection(_row("codex", fits=True))

    code = cdx.main(
        argv,
        env=env,
        stderr=err,
        deps=cdx.Deps(select=select, exec_=executor, load_config=lambda **_kw: config),
    )
    return code, executor, err.getvalue(), calls


def session_file(home_dir: Path, session_id: str, *, archived: bool = False) -> Path:
    name = f"rollout-2026-09-13T10-29-48-{session_id}.jsonl"
    path = (home_dir / "archived_sessions" / name) if archived else (
        home_dir / "sessions" / "2026" / "09" / "13" / name
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


UUID_A = "01a09bd1-6e0a-7e03-8558-88040a4bb486"
UUID_B = "01a09bd1-6e12-75e2-b503-74766b0931ad"


# ======================================================================================
# The ordinary launch
# ======================================================================================


def test_the_winning_account_selects_its_home_through_codex_home(env, home, config) -> None:
    code, executor, err, _ = launch(
        ["exec", "hi"], env, config, answer=selection(_row("codex_b", fits=True))
    )
    assert code == 0
    assert executor.env["CODEX_HOME"] == str(home / ".codex-b")
    assert "cdx → codex_b" in err


def test_only_codex_accounts_are_candidates(env, config) -> None:
    _, _, _, calls = launch(["exec", "hi"], env, config)
    assert calls[0]["only"] == ["codex", "codex_b"]


def test_the_pick_books_no_quota(env, config) -> None:
    _, _, _, calls = launch(["exec", "hi"], env, config)
    assert calls[0]["record"] is False


def test_an_inherited_codex_home_never_survives_into_the_child(env, home, config) -> None:
    env["CODEX_HOME"] = str(home / ".codex-b")
    _, executor, _, _ = launch(["exec", "hi"], env, config, answer=selection(_row("codex", fits=True)))
    assert executor.env["CODEX_HOME"] == str(home / ".codex")


def test_a_ranked_winner_that_does_not_fit_is_skipped(env, home, config) -> None:
    answer = selection(_row("codex", fits=False, score=9.0), _row("codex_b", fits=True))
    _, executor, _, _ = launch(["exec", "hi"], env, config, answer=answer)
    assert executor.env["CODEX_HOME"] == str(home / ".codex-b")


def test_the_real_binary_is_invoked_by_absolute_path(env, config) -> None:
    del env["CDX_CODEX_BIN"]
    _, executor, _, _ = launch(["exec", "hi"], env, config)
    assert executor.path == cdx.DEFAULT_CODEX_BIN
    assert executor.path.startswith("/")


def test_a_missing_binary_is_reported_with_the_override_to_set(env, config) -> None:
    def boom(path, argv, env_):
        raise OSError(2, "No such file or directory")

    err = io.StringIO()
    code = cdx.main(
        ["exec", "hi"],
        env=env,
        stderr=err,
        deps=cdx.Deps(select=lambda **kw: selection(_row("codex", fits=True)), exec_=boom,
                      load_config=lambda **_kw: config),
    )
    assert code == cdx.EXIT_CANNOT_EXEC
    assert "CDX_CODEX_BIN" in err.getvalue()


# ======================================================================================
# Failure paths still launch, and say so
# ======================================================================================


def test_a_raising_picker_lands_on_the_first_account_under_a_not_routed_banner(env, home, config) -> None:
    code, executor, err, _ = launch(["exec", "hi"], env, config, error=RuntimeError("boom"))
    assert code == 0
    assert executor.env["CODEX_HOME"] == str(home / ".codex")
    assert "ROUTER FAILED" in err and "NOT ROUTED" in err and "boom" in err


def test_a_hanging_picker_falls_back_within_the_timeout(env, config) -> None:
    release = threading.Event()

    def hang(**_kwargs):
        release.wait(5.0)
        return selection(_row("codex_b", fits=True))

    env["CDX_PICK_TIMEOUT_S"] = "0.2"
    started = time.monotonic()
    code, executor, err, _ = launch(["exec", "hi"], env, config, select=hang)
    release.set()
    assert code == 0
    assert time.monotonic() - started < 2.0
    assert "pick exceeded 0.2s" in err
    assert executor.env["CODEX_HOME"].endswith("/.codex")


def test_nothing_fits_is_reported_differently_from_nothing_read(env, config) -> None:
    spent = selection(_row("codex", fits=False), _row("codex_b", fits=False), chosen="codex")
    _, _, err_spent, _ = launch(["exec", "hi"], env, config, answer=spent)
    assert "every one is spent" in err_spent

    unread = Selection.from_payload(
        {"contract_version": 1, "decision": {"account": None, "fits": False},
         "exec": {"env": {}}, "ranked": [], "excluded": [], "warnings": []}
    )
    _, _, err_unread, _ = launch(["exec", "hi"], env, config, answer=unread)
    assert "could not be read for any of codex, codex_b" in err_unread


def test_cdx_only_names_the_fleet_by_hand(env, home, config) -> None:
    env["CDX_ONLY"] = "codex_b"
    _, executor, _, calls = launch(["exec", "hi"], env, config, answer=selection(_row("codex_b", fits=True)))
    assert calls[0]["only"] == ["codex_b"]


# ======================================================================================
# Nothing is injected
# ======================================================================================


@pytest.mark.parametrize(
    "argv",
    [
        ["exec", "--sandbox", "read-only", "--json", "hi"],
        ["exec", "--approve-for-me", "-C", "/tmp/x", "-"],
        ["exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "-"],
        [],
    ],
)
def test_argv_passes_through_byte_for_byte(env, config, argv) -> None:
    _, executor, _, _ = launch(argv, env, config)
    assert executor.argv == [env["CDX_CODEX_BIN"], *argv]


# ======================================================================================
# Sessions are routed by ownership
# ======================================================================================


@pytest.mark.parametrize(
    "argv",
    [
        ["exec", "resume", UUID_A, "continue"],
        ["resume", UUID_A],
        ["exec", "fork", UUID_A, "branch"],
        ["fork", UUID_A],
        ["exec", "--json", "resume", "--skip-git-repo-check", UUID_A],
    ],
)
def test_a_session_is_run_in_the_home_that_holds_it_whatever_the_router_says(
    env, home, config, argv
) -> None:
    session_file(home / ".codex-b", UUID_A)
    code, executor, err, calls = launch(
        argv, env, config, answer=selection(_row("codex", fits=True))
    )
    assert code == 0
    assert executor.env["CODEX_HOME"] == str(home / ".codex-b")
    assert calls == [], "ownership decides; the router is not consulted"
    assert "holds session" in err


def test_an_archived_session_is_still_found(env, home, config) -> None:
    session_file(home / ".codex", UUID_B, archived=True)
    _, executor, _, _ = launch(["resume", UUID_B], env, config, answer=selection(_row("codex_b", fits=True)))
    assert executor.env["CODEX_HOME"] == str(home / ".codex")


def test_a_session_no_home_holds_is_refused_naming_where_it_looked(env, home, config) -> None:
    code, executor, err, _ = launch(["exec", "resume", UUID_A], env, config)
    assert code == cdx.EXIT_AMBIGUOUS
    assert executor.calls == []
    assert UUID_A in err and str(home / ".codex") in err and str(home / ".codex-b") in err


@pytest.mark.parametrize(
    "argv",
    [["exec", "resume", "--last", "go"], ["resume"], ["resume", "my-thread-name"]],
)
def test_the_latest_session_is_ambiguous_across_two_homes_and_refused(env, config, argv) -> None:
    code, executor, err, _ = launch(argv, env, config)
    assert code == cdx.EXIT_AMBIGUOUS
    assert executor.calls == []
    assert "CDX_ONLY" in err


def test_cdx_only_resolves_the_latest_session_ambiguity(env, home, config) -> None:
    env["CDX_ONLY"] = "codex_b"
    code, executor, _, _ = launch(
        ["exec", "resume", "--last"], env, config, answer=selection(_row("codex_b", fits=True))
    )
    assert code == 0
    assert executor.env["CODEX_HOME"] == str(home / ".codex-b")


def test_a_single_codex_account_never_hits_the_ambiguity_rule(env, home) -> None:
    single = Config(accounts={"codex": AccountConfig(id="codex", config_dir=str(home / ".codex"))})
    code, executor, _, _ = launch(["exec", "resume", "--last"], env, single)
    assert code == 0
    assert executor.env["CODEX_HOME"] == str(home / ".codex")


# ======================================================================================
# A cold home is named, with the command that seeds it
# ======================================================================================


def test_an_account_with_no_reading_gets_the_seed_command(env, home, config) -> None:
    only_codex_was_read = selection(_row("codex", fits=True))
    _, _, err, _ = launch(["exec", "hi"], env, config, answer=only_codex_was_read)
    assert "codex_b has no usage reading yet" in err
    assert f"CODEX_HOME={home / '.codex-b'} codex exec" in err


def test_a_read_account_gets_no_seed_command(env, config) -> None:
    both = selection(_row("codex", fits=True), _row("codex_b", fits=True))
    _, _, err, _ = launch(["exec", "hi"], env, config, answer=both)
    assert "no usage reading" not in err


def test_the_seed_hint_is_not_silenced_by_quiet(env, config) -> None:
    env["CDX_QUIET"] = "1"
    _, _, err, _ = launch(["exec", "hi"], env, config, answer=selection(_row("codex", fits=True)))
    assert "cdx → codex" not in err
    assert "codex_b has no usage reading yet" in err
