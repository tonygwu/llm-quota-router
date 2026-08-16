"""Tests for :mod:`quota_router.launcher` -- the ``cl`` interactive launcher.

``cl`` is the reference consumer of this router: it is the worked example handed to
other teams for "how do I spend from the right account", so a bug here is copied
outward rather than merely suffered locally. It had no tests at all while it lived
as a loose shell script in ``~/.local/bin``, and two of the three bugs found on
2026-08-15 were in it.

The three regressions pinned below, in the order they cost the operator time:

1. **A ranked winner is not necessarily a usable one.** The router optimises for
   quota about to expire, so an account whose five-hour window is fully spent can
   legitimately rank first. Correct for the router; useless for an interactive
   session, which lands in a shell that cannot do anything for hours. The launcher
   must walk the ranked list for the top entry that ``fits``.

2. **Transcripts contain sentinel "models".** ``"<synthetic>"`` marks a locally
   generated turn. Taking the *last* model in file order let twelve sentinels
   outvote six hundred real records and routed a Fable conversation as an unknown
   class -- against the wrong weekly allowance.

3. **The default account is selected by ABSENCE.** Account A's config lives at
   ``~/.claude.json``, outside ``~/.claude``; setting ``CLAUDE_CONFIG_DIR`` to the
   directory makes Claude Code scaffold a brand-new empty account. This is the one
   that looks like untidy code and is actually the contract.

Nothing here spawns a process or reads the operator's real machine: ``main()`` takes
its environment, its router and its exec call as parameters.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from quota_router import launcher
from quota_router.api import Selection


# ======================================================================================
# Builders
# ======================================================================================


def _row(account: str, *, fits: bool, score: float = 1.0) -> dict:
    """One ``ranked`` row, with the keys the launcher actually reads."""
    return {"account": account, "fits": fits, "score": score, "eligible": True}


def selection(*ranked: dict, chosen: str | None = None, fits: bool | None = None) -> Selection:
    """A router answer built from the ``pick --json`` payload shape.

    Built from a payload rather than by hand so these tests exercise the same
    ``Selection.from_payload`` path the real API does; ``test_the_launcher_consumes_a
    _real_router_answer`` then pins that the payload shape itself is real.
    """
    rows = list(ranked)
    if chosen is None and rows:
        chosen = rows[0]["account"]
    if fits is None:
        fits = next((r["fits"] for r in rows if r["account"] == chosen), False)
    return Selection.from_payload(
        {
            "contract_version": 1,
            "decision": {"account": chosen, "provider": "claude", "fits": fits},
            "exec": {"env": {}},
            "ranked": rows,
            "excluded": [],
            "degraded": [],
            "warnings": [],
        }
    )


class Exec:
    """Stand-in for ``os.execve``: records the launch instead of becoming it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def __call__(self, path, argv, env):
        self.calls.append((path, list(argv), dict(env)))

    # -- conveniences, so assertions read as statements about the launch ---------------
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
    (root / ".claude").mkdir(parents=True)
    return root


@pytest.fixture()
def env(home) -> dict[str, str]:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin"}


def launch(argv, env, *, answer=None, error=None, select=None, stderr=None):
    """Run ``main()`` against a canned router answer and return ``(code, Exec, stderr)``."""
    import io

    executor = Exec()
    err = io.StringIO() if stderr is None else stderr

    if select is None:
        def select(**_kwargs):
            if error is not None:
                raise error
            return answer if answer is not None else selection(_row("claude", fits=True))

    code = launcher.main(
        argv,
        env=env,
        stderr=err,
        deps=launcher.Deps(select=select, exec_=executor),
    )
    return code, executor, err.getvalue()


def transcript(home: Path, session_id: str, models: list[str]) -> Path:
    """Write a session transcript whose assistant turns carry ``models``, in order."""
    project = home / ".claude" / "projects" / "-Users-op-Code-thing"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for model in models:
            handle.write(json.dumps({"type": "assistant", "message": {"model": model}}) + "\n")
    return path


# ======================================================================================
# Regression 1 -- a top-ranked account that cannot serve the call
# ======================================================================================


def test_a_ranked_winner_that_does_not_fit_is_skipped(env) -> None:
    """The router's winner is its best *score*, not a promise it can serve you.

    Reverting the ``fits`` walk puts the session on claude_c, whose window is spent:
    an interactive shell that cannot answer a single prompt until the window resets.
    """
    answer = selection(
        _row("claude_c", fits=False, score=9.0),
        _row("claude_b", fits=True, score=4.0),
        _row("claude", fits=True, score=1.0),
    )
    code, executor, _ = launch([], env, answer=answer)

    assert code == 0
    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-b")


def test_the_first_fitting_account_wins_not_merely_any_fitting_one(env) -> None:
    """A *walk*, not a filter-and-shrug: ranked order still decides among survivors."""
    answer = selection(
        _row("claude_c", fits=False, score=9.0),
        _row("claude_d", fits=True, score=4.0),
        _row("claude_b", fits=True, score=3.0),
    )
    _, executor, _ = launch([], env, answer=answer)

    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-d")


def test_a_fitting_decision_is_taken_without_consulting_the_ranked_list(env) -> None:
    answer = selection(
        _row("claude_b", fits=True, score=9.0),
        _row("claude", fits=True, score=1.0),
    )
    _, executor, _ = launch([], env, answer=answer)

    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-b")


def test_no_account_fits_warns_on_stderr_and_still_launches(env) -> None:
    """Refusing to launch would be the worse failure: the operator wants a shell."""
    answer = selection(
        _row("claude_c", fits=False),
        _row("claude_b", fits=False),
    )
    code, executor, err = launch([], env, answer=answer)

    assert code == 0
    assert executor.calls, "cl must still start a session when the fleet is exhausted"
    assert "CLAUDE_CONFIG_DIR" not in executor.env
    assert "no account" in err.lower()
    assert "quotapick status" in err


def test_the_launcher_consumes_a_real_router_answer(tmp_path) -> None:
    """Guard against contract drift: the keys read above are the keys really emitted.

    Every other test in this file builds its own payload, so a rename of ``fits`` or
    ``ranked`` would leave them all green and the launcher broken. This one takes the
    answer from :func:`quota_router.select_account` itself, over the committed
    fixture in which every account is exhausted.
    """
    from quota_router import cli, select_account
    from tests.test_cli import NOW, real_capture

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    snaps = real_capture()

    def select(**kwargs):
        return select_account(
            **{**kwargs, "deps": cli.Deps(load_snapshots=lambda **kw: (list(snaps), []))},
        )

    answer = select(env=env, now_s=NOW, record=False)
    assert answer.ranked, "the fixture must produce a ranked list for this to prove anything"
    assert all("fits" in row for row in answer.ranked)

    # claude_c is the exhausted account in the fixture (100% on every window), so
    # whatever the ranking says, the launcher must not put the session there.
    code, executor, _ = launch([], env, select=lambda **kw: select(**{**kw, "now_s": NOW}))
    assert code == 0
    assert not executor.env.get("CLAUDE_CONFIG_DIR", "").endswith("/.claude-c")


# ======================================================================================
# Regression 2 -- sentinel models in a resumed session's transcript
# ======================================================================================


def test_sentinel_models_cannot_outvote_the_real_one(home, env) -> None:
    """628 real records beat 12 trailing ``<synthetic>`` sentinels.

    The numbers are the ones from the live failure. Taking the last model in file
    order routed that conversation as an unknown class, against the general pool
    rather than Fable's separate weekly allowance.
    """
    session = "9d1f7c22-0000-4000-8000-000000000001"
    transcript(home, session, ["claude-fable-5"] * 628 + ["<synthetic>"] * 12)

    assert launcher.detect_model(["--resume", session], home=home) == "claude-fable-5"


def test_every_bracketed_sentinel_is_ignored_not_just_synthetic(home) -> None:
    """The rule is "``<`` means sentinel", not a blocklist of the ones seen so far."""
    session = "9d1f7c22-0000-4000-8000-000000000002"
    transcript(home, session, ["<synthetic>", "<none>", "<unknown>", "claude-opus-5"])

    assert launcher.detect_model(["--resume", session], home=home) == "claude-opus-5"


def test_a_transcript_of_only_sentinels_yields_no_model(home) -> None:
    """No answer is honest here; an invented one routes against the wrong window."""
    session = "9d1f7c22-0000-4000-8000-000000000003"
    transcript(home, session, ["<synthetic>"] * 5)

    assert launcher.detect_model(["--resume", session], home=home) is None


def test_a_corrupt_transcript_line_does_not_stop_detection(home) -> None:
    session = "9d1f7c22-0000-4000-8000-000000000004"
    path = transcript(home, session, ["claude-fable-5"] * 3)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")

    assert launcher.detect_model(["--resume", session], home=home) == "claude-fable-5"


def test_the_detected_model_is_what_the_router_is_asked_about(home, env) -> None:
    """Detection that never reaches the router is decoration.

    Fable draws on a separate weekly allowance, so an account can be wide open on the
    general pool and exhausted on Fable. Routing without the model class asks the
    wrong question.
    """
    session = "9d1f7c22-0000-4000-8000-000000000005"
    transcript(home, session, ["claude-fable-5"] * 10)
    seen: dict = {}

    def select(**kwargs):
        seen.update(kwargs)
        return selection(_row("claude", fits=True))

    launch(["--resume", session], env, select=select)
    assert seen["model"] == "claude-fable-5"


# ======================================================================================
# Regression 3 -- the default account is selected by the ABSENCE of the variable
# ======================================================================================


def test_the_default_account_unsets_claude_config_dir(env) -> None:
    """Never ``CLAUDE_CONFIG_DIR=~/.claude``: that scaffolds a fresh empty account.

    Run from a shell already pointed at account B -- which is exactly how the
    operator uses it -- so an overlay that merely *omits* the variable would leave
    the session on B and silently spend the wrong account's quota.
    """
    poisoned = {**env, "CLAUDE_CONFIG_DIR": str(Path(env["HOME"]) / ".claude-b")}
    _, executor, _ = launch([], poisoned, answer=selection(_row("claude", fits=True)))

    assert "CLAUDE_CONFIG_DIR" not in executor.env


@pytest.mark.parametrize(
    ("account", "directory"),
    [
        ("claude_b", ".claude-b"),
        ("claude_c", ".claude-c"),
        ("claude_d", ".claude-d"),
    ],
)
def test_the_lettered_accounts_map_to_their_own_directories(env, account, directory) -> None:
    _, executor, _ = launch([], env, answer=selection(_row(account, fits=True)))

    assert executor.env["CLAUDE_CONFIG_DIR"] == str(Path(env["HOME"]) / directory)


def test_an_unknown_account_id_falls_back_to_the_default_rather_than_guessing(env) -> None:
    """A directory guessed from an id would be a fresh empty account, silently."""
    _, executor, err = launch([], env, answer=selection(_row("codex", fits=True)))

    assert "CLAUDE_CONFIG_DIR" not in executor.env
    assert executor.calls


# ======================================================================================
# The picker must never be able to wedge a shell
# ======================================================================================


def test_a_hanging_picker_falls_back_within_the_timeout(env) -> None:
    """A Keychain prompt with nobody to answer it must not block every shell launch."""
    released = threading.Event()

    def select(**_kwargs):
        released.wait(30)
        return selection(_row("claude_b", fits=True))

    started = time.monotonic()
    try:
        code, executor, err = launch(
            [], {**env, "CL_PICK_TIMEOUT_S": "0.1"}, select=select
        )
        elapsed = time.monotonic() - started

        assert code == 0
        assert elapsed < 5.0, f"cl waited {elapsed:.1f}s on a hung picker"
        assert executor.calls, "a timed-out pick must still start a session"
        assert "CLAUDE_CONFIG_DIR" not in executor.env
    finally:
        released.set()


def test_a_raising_picker_still_starts_a_session(env) -> None:
    """A quota router that cannot answer must never be why you cannot work."""
    code, executor, _ = launch([], env, error=RuntimeError("oracle exploded"))

    assert code == 0
    assert executor.calls
    assert "CLAUDE_CONFIG_DIR" not in executor.env


def test_the_pick_books_no_quota(env) -> None:
    """``cl`` is often run and abandoned (wrong directory, changed mind).

    A reservation for a session that never starts would push the next real caller
    off a perfectly good account.
    """
    seen: dict = {}

    def select(**kwargs):
        seen.update(kwargs)
        return selection(_row("claude", fits=True))

    launch([], env, select=select)
    assert seen["record"] is False


# ======================================================================================
# The launch itself
# ======================================================================================


def test_the_real_binary_is_invoked_by_absolute_path(env) -> None:
    """Spawning bare ``claude`` re-enters this wrapper if it is ever aliased.

    An infinite fork loop at shell-launch time is a genuinely bad afternoon.
    """
    _, executor, _ = launch([], env)

    assert Path(executor.path).is_absolute()
    assert Path(executor.argv[0]).is_absolute()
    assert Path(executor.path).name == "claude"
    assert "claude" not in executor.argv[:1] or Path(executor.argv[0]).is_absolute()


def test_the_bypass_flag_is_the_one_that_actually_enters_bypass_mode(env) -> None:
    """``--allow-dangerously-skip-permissions`` merely offers it as a toggle."""
    _, executor, _ = launch([], env)

    assert "--dangerously-skip-permissions" in executor.argv
    assert "--allow-dangerously-skip-permissions" not in executor.argv


def test_user_arguments_pass_through_unchanged(env) -> None:
    args = ["--resume", "abc-123", "-p", "hello --model world", "--verbose"]
    _, executor, _ = launch(args, env)

    assert executor.argv[-len(args) :] == args


def test_proxy_variables_are_not_forwarded_to_the_session(env) -> None:
    """A proxy token makes Claude Code bill the API instead of the subscription.

    That defeats the entire router -- it would spend money while reporting that it
    spent an account's expiring quota.
    """
    from quota_router.config import BANNED_EXEC_ENV

    poisoned = {**env, **{name: "should-never-be-forwarded" for name in BANNED_EXEC_ENV}}
    _, executor, _ = launch([], poisoned)

    assert not set(executor.env) & set(BANNED_EXEC_ENV)


def test_the_rest_of_the_environment_survives(env) -> None:
    _, executor, _ = launch([], {**env, "TERM": "xterm-256color"})

    assert executor.env["TERM"] == "xterm-256color"
    assert executor.env["PATH"] == "/usr/bin:/bin"


# ======================================================================================
# Model detection
# ======================================================================================


def test_an_explicit_model_beats_transcript_detection(home) -> None:
    session = "9d1f7c22-0000-4000-8000-000000000006"
    transcript(home, session, ["claude-fable-5"] * 50)

    detected = launcher.detect_model(
        ["--resume", session, "--model", "opus[1m]"], home=home
    )
    assert detected == "opus[1m]"


def test_the_joined_model_form_is_understood(home) -> None:
    assert launcher.detect_model(["--model=opus[1m]"], home=home) == "opus[1m]"


def test_the_short_resume_flag_is_understood(home) -> None:
    session = "9d1f7c22-0000-4000-8000-000000000007"
    transcript(home, session, ["claude-fable-5"])

    assert launcher.detect_model(["-r", session], home=home) == "claude-fable-5"


def test_the_settings_default_is_the_last_resort(home) -> None:
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"model": "claude-sonnet-5"}), encoding="utf-8"
    )
    assert launcher.detect_model([], home=home) == "claude-sonnet-5"


def test_an_unknown_session_id_is_not_an_error(home) -> None:
    assert launcher.detect_model(["--resume", "no-such-session"], home=home) is None


def test_unreadable_settings_are_not_an_error(home) -> None:
    (home / ".claude" / "settings.json").write_text("{ broken", encoding="utf-8")
    assert launcher.detect_model([], home=home) is None


# ======================================================================================
# Operator overrides (the escape hatches the shell script exposed)
# ======================================================================================


def test_the_binary_can_be_pointed_elsewhere(env, tmp_path) -> None:
    elsewhere = tmp_path / "opt" / "claude"
    _, executor, _ = launch([], {**env, "CL_CLAUDE_BIN": str(elsewhere)})

    assert executor.path == str(elsewhere)


def test_the_candidate_set_defaults_to_the_four_claude_accounts(env) -> None:
    seen: dict = {}

    def select(**kwargs):
        seen.update(kwargs)
        return selection(_row("claude", fits=True))

    launch([], env, select=select)
    assert list(seen["only"]) == ["claude", "claude_b", "claude_c", "claude_d"]


def test_the_candidate_set_can_be_narrowed(env) -> None:
    seen: dict = {}

    def select(**kwargs):
        seen.update(kwargs)
        return selection(_row("claude_b", fits=True))

    launch([], {**env, "CL_ONLY": "claude_b,claude_c"}, select=select)
    assert list(seen["only"]) == ["claude_b", "claude_c"]


def test_the_banner_names_the_account_and_model(env, home) -> None:
    session = "9d1f7c22-0000-4000-8000-000000000008"
    transcript(home, session, ["claude-fable-5"] * 3)

    _, _, err = launch(
        ["--resume", session], env, answer=selection(_row("claude_b", fits=True))
    )
    assert "claude_b" in err
    assert "claude-fable-5" in err


def test_the_banner_can_be_silenced(env) -> None:
    _, _, err = launch([], {**env, "CL_QUIET": "1"}, answer=selection(_row("claude_b", fits=True)))

    assert err == ""


def test_a_silenced_banner_does_not_silence_the_exhaustion_warning(env) -> None:
    """CL_QUIET hides the routine line, not the news that nothing can serve you."""
    answer = selection(_row("claude_b", fits=False))
    _, _, err = launch([], {**env, "CL_QUIET": "1"}, answer=answer)

    assert "no account" in err.lower()


# ======================================================================================
# Packaging -- the operator's whole requirement is that typing `cl` works
# ======================================================================================


def test_cl_is_installed_as_a_console_script() -> None:
    """The entry point is the deliverable.

    ``cl`` used to be a loose shell script in ``~/.local/bin`` -- the same directory
    ``uv tool install`` writes console scripts into. If this declaration is dropped,
    an install silently leaves whatever file already occupies that path, and the
    operator keeps running the untested version while the suite stays green.
    """
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]

    assert scripts.get("cl") == "quota_router.launcher:main"


def test_the_console_script_entry_point_is_callable_with_no_arguments() -> None:
    """setuptools generates ``sys.exit(main())`` -- every parameter must have a default."""
    import inspect

    signature = inspect.signature(launcher.main)
    assert all(
        parameter.default is not inspect.Parameter.empty
        for parameter in signature.parameters.values()
    )
