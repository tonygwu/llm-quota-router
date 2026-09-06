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
    """One ``ranked`` row, with the keys the launcher actually reads.

    ``remaining`` carries a number, never ``None``: these rows stand for accounts the
    router successfully READ. ``None`` there means "could not be read", which is the
    one input that unlocks the weekly-reset fallback, and a fixture that left the key
    out would put every ordinary test through that branch by accident.
    """
    return {
        "account": account,
        "fits": fits,
        "score": score,
        "eligible": True,
        "remaining": 0.5 if fits else 0.0,
    }


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


def launch(argv, env, *, answer=None, error=None, select=None, stderr=None, config=None):
    """Run ``main()`` against a canned router answer and return ``(code, Exec, stderr)``."""
    import io

    executor = Exec()
    err = io.StringIO() if stderr is None else stderr

    if select is None:
        def select(**_kwargs):
            if error is not None:
                raise error
            return answer if answer is not None else selection(_row("claude", fits=True))

    deps_kwargs = {"select": select, "exec_": executor}
    if config is not None:
        deps_kwargs["load_config"] = lambda **_kw: config

    code = launcher.main(
        argv,
        env=env,
        stderr=err,
        deps=launcher.Deps(**deps_kwargs),
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


def test_a_session_that_switched_models_routes_on_the_one_it_ended_on(home) -> None:
    """The question is what the NEXT turn will burn, not what the history mostly was.

    Live: a session ran 650 Fable turns, was switched to Opus with ``/model``, then
    ran 310 more. ``cl --resume`` scored the pick against Fable and sent it to the
    only Fable-capable account -- gating on a scoped weekly sub-cap the resumed work
    does not touch, while ignoring the general pool it does. Both fallbacks (the
    session's last model and the settings default) said Opus; the majority vote was
    the one thing that said otherwise, so detection made the answer worse than
    returning nothing would have.

    Majority was itself a fix for sentinels outvoting real records. But sentinels are
    already excluded by name, so taking the LAST REAL model is safe -- the vote solved
    that problem a second time and bought this one.
    """
    session = "9d1f7c22-0000-4000-8000-000000000006"
    transcript(
        home,
        session,
        ["claude-fable-5"] * 650 + ["claude-opus-5"] * 310 + ["<synthetic>"] * 16,
    )

    assert launcher.detect_model(["--resume", session], home=home) == "claude-opus-5"


def test_a_model_switch_past_the_head_of_a_long_transcript_is_still_seen(home) -> None:
    """The old reader capped at 4000 head lines, so a late switch was invisible.

    A last-model question is answered from the tail, which is also cheaper than
    reading thousands of lines from the front.
    """
    session = "9d1f7c22-0000-4000-8000-000000000007"
    transcript(home, session, ["claude-fable-5"] * 8000 + ["claude-opus-5"] * 5)

    assert launcher.detect_model(["--resume", session], home=home) == "claude-opus-5"


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


def test_the_debug_line_names_the_real_cause_of_a_failed_pick(env) -> None:
    """A crash and a timeout degrade identically and must not be *reported* identically.

    They have completely different fixes -- one is a bug in the router, the other is
    a blocked credential read -- and CL_DEBUG is the only place either is visible.
    Reporting a crash as "exceeded 3.0s" sends the reader looking for a hang that
    never happened.
    """
    _, _, err = launch(
        [], {**env, "CL_DEBUG": "1"}, error=RuntimeError("oracle exploded")
    )

    assert "oracle exploded" in err
    assert "exceeded" not in err


def test_the_debug_line_reports_a_timeout_as_a_timeout(env) -> None:
    released = threading.Event()

    def select(**_kwargs):
        released.wait(30)
        return selection(_row("claude_b", fits=True))

    try:
        _, _, err = launch(
            [], {**env, "CL_DEBUG": "1", "CL_PICK_TIMEOUT_S": "0.1"}, select=select
        )
        assert "exceeded" in err
    finally:
        released.set()


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


# ======================================================================================
# Regression 4 -- resuming a session whose model is exhausted everywhere
# ======================================================================================


#: A config search root that cannot exist, so ``load_config`` below reads the builtin
#: defaults and nothing at all off the operator's machine.
#:
#: ``load_config(env={})`` does NOT mean "no config": ``config_search_paths`` falls
#: back to ``Path.home()`` for an absent HOME and to ``Path.cwd()`` for an absent cwd,
#: so it reads ``~/.config/quota-router/config.toml`` and whatever project file the
#: suite happens to be run from. Every helper here used it until 2026-08-24, when
#: adding real ``weekly_reset`` schedules to that file turned four tests red -- tests
#: that had been asserting on the absence of settings the operator merely had not set
#: yet. A suite whose result depends on the machine it runs on is not a suite.
_NO_CONFIG_FILES: str = "/nonexistent/quota-router-test-root"


def builtin_only():
    """The builtin config -- account config-dirs resolve, no file layer is read."""
    from quota_router.config import load_config

    return load_config(env={"XDG_CONFIG_HOME": _NO_CONFIG_FILES}, cwd=_NO_CONFIG_FILES)


def cfg(**overrides):
    """The builtin config (so account config-dirs resolve), plus overrides."""
    import dataclasses

    return dataclasses.replace(builtin_only(), **overrides)


def _by_model(**answers):
    """A router stub that answers differently depending on the model it is asked about."""

    def select(**kwargs):
        model = kwargs.get("model")
        key = (model or "none").replace("-", "_").replace("[", "_").replace("]", "")
        if key not in answers:
            raise AssertionError(f"router asked about an unexpected model: {model!r}")
        return answers[key]

    return select


def test_an_exhausted_model_substitutes_the_configured_fallback(env, home) -> None:
    """Resuming a Fable thread with no Fable quota anywhere.

    Before: the launcher warned and dropped the session on the default account --
    where its very first turn hits the same wall it was just told about. Nothing about
    the launch reflected that the requested model was unavailable.

    Substitution is the one case where passing ``--model`` through to claude is
    correct. Everywhere else it would be trying to REPRODUCE a selection from a
    server-stamped id, which is lossy (``opus[1m]`` is recorded as ``claude-opus-5``,
    so the variant cannot be recovered). Here it is a deliberate OVERRIDE, and the
    string comes from the operator's config rather than from a transcript -- so there
    is no fidelity to lose, and no way for the override to take effect without it.
    """
    session = "9d1f7c22-0000-4000-8000-00000000000a"
    transcript(home, session, ["claude-fable-5"] * 40)

    code, executor, err = launch(
        ["--resume", session],
        env,
        config=cfg(fallback_model="opus[1m]"),
        select=_by_model(
            claude_fable_5=selection(_row("claude_c", fits=False)),
            opus_1m=selection(_row("claude_b", fits=True)),
        ),
    )

    assert code == 0
    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-b"), (
        "the account must be chosen for the model that will actually run"
    )
    assert "--model" in executor.argv, "an override that is not passed is not an override"
    assert executor.argv[executor.argv.index("--model") + 1] == "opus[1m]", (
        "the configured string must be passed VERBATIM -- deriving it from a "
        "transcript is what loses the [1m] variant"
    )
    assert "fable" in err.lower() and "substitut" in err.lower(), (
        f"a silent capability downgrade is worse than a failed launch; got {err!r}"
    )


def test_without_a_configured_fallback_nothing_is_substituted(env, home) -> None:
    """Opt-in. The router must not choose a weaker model on the operator's behalf."""
    session = "9d1f7c22-0000-4000-8000-00000000000b"
    transcript(home, session, ["claude-fable-5"] * 40)

    code, executor, err = launch(
        ["--resume", session],
        env,
        config=cfg(),
        select=_by_model(claude_fable_5=selection(_row("claude_c", fits=False))),
    )

    assert code == 0
    assert "--model" not in executor.argv
    assert "no account can serve" in err


def test_an_explicit_model_is_never_substituted(env, home) -> None:
    """The operator asked for this model by name. Overriding it is not ours to do."""
    code, executor, err = launch(
        ["--model", "claude-fable-5"],
        env,
        config=cfg(fallback_model="opus[1m]"),
        # The fallback is registered and WOULD be served. The only thing standing
        # between it and a substitution is the explicit --model, which is the point:
        # without this arm the test passes because the retry errors, not because the
        # guard held.
        select=_by_model(
            claude_fable_5=selection(_row("claude_c", fits=False)),
            opus_1m=selection(_row("claude_b", fits=True)),
        ),
    )

    assert code == 0
    assert executor.argv.count("--model") == 1, "no second --model may be injected"
    assert "opus[1m]" not in executor.argv


def test_a_model_that_still_fits_is_left_alone(env, home) -> None:
    """Substitution is a last resort, not an optimisation."""
    session = "9d1f7c22-0000-4000-8000-00000000000c"
    transcript(home, session, ["claude-fable-5"] * 40)

    code, executor, _ = launch(
        ["--resume", session],
        env,
        config=cfg(fallback_model="opus[1m]"),
        select=_by_model(claude_fable_5=selection(_row("claude_c", fits=True))),
    )

    assert code == 0
    assert "--model" not in executor.argv
    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-c")


def test_fallback_model_is_not_reported_as_an_unknown_section(tmp_path) -> None:
    """A working setting that warns "ignored" teaches the operator to distrust it.

    ``_KNOWN_SECTIONS`` is derived from the builtin defaults, which contain only
    tables. ``fallback_model`` is a top-level *scalar* with no builtin entry, so the
    validator called it an unknown section and said it was ignored -- while ``_build``
    read and applied it perfectly well. Both halves are bad: the warning is false, and
    it is the kind of false that makes someone "fix" a config that was already right.
    """
    from quota_router.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('fallback_model = "opus[1m]"\n', encoding="utf-8")
    cfg = load_config(env={}, explicit_path=path)

    assert cfg.fallback_model == "opus[1m]"
    assert not [w for w in cfg.warnings if "fallback_model" in w], cfg.warnings


def test_a_genuinely_unknown_top_level_key_still_warns(tmp_path) -> None:
    """Widening the allow-list must not turn the validator off."""
    from quota_router.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('falback_model = "opus[1m]"\n', encoding="utf-8")  # typo, on purpose
    cfg = load_config(env={}, explicit_path=path)

    assert any("falback_model" in w for w in cfg.warnings), cfg.warnings


# ======================================================================================
# 2026-08-24 -- the give-up branch that landed on the exhausted account, silently
# ======================================================================================
#
# What happened: `cl` printed `cl → claude · opus` and dropped an interactive session
# on the one account whose weekly window was 100% spent. Replaying the recorded
# snapshot through the decision layer shows the router had answered `claude_d`. The
# pick failed inside its three-second cap, and `_route`'s give-up branch is hardcoded
# to `ACCOUNT_CLAUDE`. Two separate faults, pinned separately below:
#
#   1. The give-up banner was byte-identical to a successful route, so there was
#      nothing on screen to distinguish "chosen" from "gave up". The cause was
#      written only under CL_DEBUG, which nobody sets before the thing goes wrong.
#   2. The give-up TARGET was a fixed account. When live usage is unavailable for
#      every account, the weekly reset schedule is still known -- it is fixed when the
#      account is created -- so the fleet's most-nearly-expiring week is a better
#      answer than always picking the same one.
#
# The second is a LAST resort. Measured quota always wins, and "measured, and
# everyone is empty" is a different fact from "not measured" -- it keeps the older
# exhaustion path, which several tests below hold in place.

import datetime as _datetime
from zoneinfo import ZoneInfo as _ZoneInfo

_LA = _ZoneInfo("America/Los_Angeles")

#: Monday morning, the hours before every account's weekly window rolls over.
MONDAY_0953 = _datetime.datetime(2026, 8, 24, 9, 53, tzinfo=_LA).timestamp()


def _dark_row(account: str) -> dict:
    """A candidate whose usage could NOT be read: remaining is unknown, not zero."""
    return {
        "account": account,
        "fits": False,
        "score": 0.0,
        "eligible": False,
        "remaining": None,
        "source": "live",
        "reason": "account unavailable (access token expired)",
    }


def _measured_row(account: str, remaining: float) -> dict:
    """A candidate that WAS read. ``remaining=0.0`` means empty, which is a measurement."""
    return {
        "account": account,
        "fits": remaining > 0.0,
        "score": 0.0,
        "eligible": True,
        "remaining": remaining,
        "source": "live",
    }


def dark_selection(*rows: dict, excluded: list | None = None) -> Selection:
    """A router answer that completed but measured nothing."""
    return Selection.from_payload(
        {
            "contract_version": 1,
            "decision": {"account": None, "provider": None, "fits": False},
            "exec": {"env": {}},
            "ranked": list(rows),
            "excluded": list(excluded or []),
            "degraded": [],
            "warnings": [],
        }
    )


def scheduled(**by_account: str):
    """The builtin config plus a weekly reset schedule for the named accounts.

    Built on :func:`builtin_only`, so an account is scheduled here if and only if this
    test said so -- never because the operator's own config happens to schedule it.
    """
    import dataclasses

    from quota_router.weekly_reset import parse_weekly_reset

    base = builtin_only()
    accounts = dict(base.accounts)
    for account_id, text in by_account.items():
        accounts[account_id] = dataclasses.replace(
            accounts[account_id], weekly_reset=parse_weekly_reset(text)
        )
    return dataclasses.replace(base, accounts=accounts)


def launch_at(argv, env, *, now, **kwargs):
    """``launch`` with the launcher's clock pinned, so reported durations are exact."""
    import io

    executor = Exec()
    err = io.StringIO()
    answer = kwargs.pop("answer", None)
    error = kwargs.pop("error", None)
    select = kwargs.pop("select", None)
    config = kwargs.pop("config", None)
    assert not kwargs, kwargs

    if select is None:

        def select(**_kwargs):
            if error is not None:
                raise error
            return answer

    deps_kwargs = {"select": select, "exec_": executor, "now": lambda: now}
    if config is not None:
        deps_kwargs["load_config"] = lambda **_kw: config

    code = launcher.main(argv, env=env, stderr=err, deps=launcher.Deps(**deps_kwargs))
    return code, executor, err.getvalue()


# -- fault 1: the give-up banner must not look like a route ----------------------------


def test_a_real_route_is_not_marked_as_a_fallback(env) -> None:
    """The negative half. Without it, marking *everything* would pass every test below."""
    _, _, err = launch([], env, answer=selection(_row("claude_b", fits=True)))

    assert "claude_b" in err
    assert "NOT ROUTED" not in err
    assert "ROUTER FAILED" not in err


def test_a_timed_out_pick_says_so_on_stderr_without_cl_debug(env) -> None:
    """The 2026-08-24 symptom exactly: a silent default that reads as a decision.

    CL_DEBUG is deliberately NOT set here. A failure only visible to someone who
    already suspected it is not visible.
    """
    released = threading.Event()

    def select(**_kwargs):
        released.wait(30)
        return selection(_row("claude_b", fits=True))

    try:
        _, executor, err = launch(
            [], {**env, "CL_PICK_TIMEOUT_S": "0.1"}, select=select
        )

        assert executor.calls, "a timed-out pick must still start a session"
        assert "ROUTER FAILED" in err
        assert "exceeded" in err
        assert "NOT ROUTED" in err
    finally:
        released.set()


def test_a_raising_pick_says_so_on_stderr_without_cl_debug(env) -> None:
    """A crash and a timeout have different fixes, so they keep different text."""
    _, _, err = launch([], env, error=RuntimeError("oracle exploded"))

    assert "ROUTER FAILED" in err
    assert "oracle exploded" in err
    assert "NOT ROUTED" in err
    assert "exceeded" not in err


def test_an_exhausted_fleet_is_marked_unrouted_but_not_as_a_router_failure(env) -> None:
    """Measured-and-empty is not the same news as not-measured. Both are non-routes."""
    answer = selection(_measured_row("claude_b", 0.0), _measured_row("claude_c", 0.0))
    _, _, err = launch([], env, answer=answer)

    assert "NOT ROUTED" in err
    assert "ROUTER FAILED" not in err
    assert "no account can serve" in err


def test_the_fallback_banner_survives_cl_quiet(env) -> None:
    """CL_QUIET hides the routine line. Nothing about this launch is routine."""
    _, _, err = launch([], {**env, "CL_QUIET": "1"}, error=RuntimeError("oracle exploded"))

    assert "NOT ROUTED" in err
    assert "ROUTER FAILED" in err


# -- fault 2: where the give-up branch lands -------------------------------------------


def test_a_failed_pick_falls_back_to_the_soonest_weekly_reset(env) -> None:
    """The heuristic itself: with nothing measured, spend the week that expires first.

    Reverting this puts every router failure on the same hardcoded account, which is
    how a 100%-spent account got an interactive session on 2026-08-24.
    """
    config = scheduled(
        claude="Fri 15:59 America/Los_Angeles",
        claude_b="Wed 15:59 America/Los_Angeles",
        claude_c="Mon 15:59 America/Los_Angeles",
        claude_d="Thu 15:59 America/Los_Angeles",
    )

    code, executor, err = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("oracle exploded"), config=config
    )

    assert code == 0
    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-c")
    assert "WEEKLY-RESET FALLBACK" in err
    assert "claude_c" in err


def test_the_weekly_fallback_says_it_is_not_using_live_quota(env) -> None:
    """The operator must be able to tell this apart from a measured decision at a glance."""
    config = scheduled(claude_c="Mon 15:59 America/Los_Angeles")

    _, _, err = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("oracle exploded"), config=config
    )

    assert "WEEKLY-RESET FALLBACK" in err
    assert "not live quota" in err.lower()
    assert "6h06m" in err, f"the reset deadline is what justifies the choice; got {err!r}"


def test_a_pick_that_hangs_still_reaches_the_weekly_fallback(env) -> None:
    """Config is read BEFORE the slow part, so a blown deadline does not lose it.

    Loading it inside the same capped call and returning it as a tuple -- the shape
    this replaces -- discards the config along with the abandoned thread, leaving the
    fallback with no schedules exactly when it is needed.
    """
    released = threading.Event()
    config = scheduled(claude_c="Mon 15:59 America/Los_Angeles")

    def select(**_kwargs):
        released.wait(30)
        return selection(_row("claude_b", fits=True))

    try:
        _, executor, err = launch_at(
            [],
            {**env, "CL_PICK_TIMEOUT_S": "0.1"},
            now=MONDAY_0953,
            select=select,
            config=config,
        )

        assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-c")
        assert "WEEKLY-RESET FALLBACK" in err
    finally:
        released.set()


def test_usage_that_could_not_be_read_anywhere_reaches_the_weekly_fallback(env) -> None:
    """The pick completed and measured nothing -- four dark tokens, say."""
    config = scheduled(claude_c="Mon 15:59 America/Los_Angeles")
    answer = dark_selection(
        _dark_row("claude"),
        _dark_row("claude_b"),
        _dark_row("claude_c"),
        _dark_row("claude_d"),
    )

    _, executor, err = launch_at([], env, now=MONDAY_0953, answer=answer, config=config)

    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-c")
    assert "WEEKLY-RESET FALLBACK" in err


def test_a_dark_account_reported_only_as_excluded_still_counts_as_unmeasured(env) -> None:
    """An unreadable account can land in ``excluded`` rather than ``ranked``.

    Reading only ``ranked`` would see an empty list, conclude nothing about usage,
    and take the wrong branch on the strength of a rendering detail.
    """
    config = scheduled(claude_c="Mon 15:59 America/Los_Angeles")
    answer = dark_selection(
        excluded=[
            _dark_row("claude"),
            _dark_row("claude_b"),
            _dark_row("claude_c"),
            _dark_row("claude_d"),
        ]
    )

    _, executor, err = launch_at([], env, now=MONDAY_0953, answer=answer, config=config)

    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-c")
    assert "WEEKLY-RESET FALLBACK" in err


# -- the "ONLY if" guard: one measurement anywhere disables the heuristic ---------------


def test_one_measured_account_keeps_the_weekly_fallback_out_of_it(env) -> None:
    """``remaining = 0.0`` is a measurement. It says empty; it does not say unknown.

    This is the whole guard. The schedule is a far weaker signal than the bar, so it
    must never override a reading -- not even a reading that says zero.
    """
    config = scheduled(claude_c="Mon 15:59 America/Los_Angeles")
    answer = dark_selection(
        _dark_row("claude"),
        _dark_row("claude_b"),
        _measured_row("claude_c", 0.0),
        _dark_row("claude_d"),
    )

    _, executor, err = launch_at([], env, now=MONDAY_0953, answer=answer, config=config)

    assert "WEEKLY-RESET FALLBACK" not in err
    assert "CLAUDE_CONFIG_DIR" not in executor.env, "the older exhaustion path owns this"
    assert "no account can serve" in err


def test_a_fully_measured_and_fully_spent_fleet_never_reaches_the_weekly_fallback(env) -> None:
    """The 2026-08-24 fleet, one hour later: everyone readable, everyone empty."""
    config = scheduled(
        claude="Mon 15:59 America/Los_Angeles",
        claude_b="Wed 15:59 America/Los_Angeles",
    )
    answer = selection(
        _measured_row("claude", 0.0),
        _measured_row("claude_b", 0.0),
        _measured_row("claude_c", 0.0),
        _measured_row("claude_d", 0.0),
    )

    _, _, err = launch_at([], env, now=MONDAY_0953, answer=answer, config=config)

    assert "WEEKLY-RESET FALLBACK" not in err
    assert "no account can serve" in err


def test_a_routable_answer_never_reaches_the_weekly_fallback(env) -> None:
    config = scheduled(claude_c="Mon 15:59 America/Los_Angeles")

    _, executor, err = launch_at(
        [], env, now=MONDAY_0953, answer=selection(_row("claude_b", fits=True)), config=config
    )

    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-b")
    assert "WEEKLY-RESET FALLBACK" not in err


# -- the candidate set, and having no schedule at all ----------------------------------


def test_the_weekly_fallback_only_considers_the_candidate_set(env) -> None:
    """``CL_ONLY`` narrows the fleet. It must narrow this branch too."""
    config = scheduled(
        claude_b="Wed 15:59 America/Los_Angeles",
        claude_c="Mon 15:59 America/Los_Angeles",
    )

    _, executor, _ = launch_at(
        [],
        {**env, "CL_ONLY": "claude_b,claude_d"},
        now=MONDAY_0953,
        error=RuntimeError("oracle exploded"),
        config=config,
    )

    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-b"), (
        "claude_c resets soonest but was not a candidate"
    )


def test_the_weekly_fallback_skips_a_disabled_account(env) -> None:
    import dataclasses

    config = scheduled(
        claude_b="Wed 15:59 America/Los_Angeles",
        claude_c="Mon 15:59 America/Los_Angeles",
    )
    accounts = dict(config.accounts)
    accounts["claude_c"] = dataclasses.replace(accounts["claude_c"], enabled=False)
    config = dataclasses.replace(config, accounts=accounts)

    _, executor, _ = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("oracle exploded"), config=config
    )

    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-b")


def test_no_schedule_anywhere_still_launches_and_names_the_missing_setting(env) -> None:
    """Degrading to the old hardcoded default is fine. Doing it quietly is not."""
    code, executor, err = launch_at(
        [],
        env,
        now=MONDAY_0953,
        error=RuntimeError("oracle exploded"),
        config=cfg(),
    )

    assert code == 0
    assert executor.calls
    assert "CLAUDE_CONFIG_DIR" not in executor.env
    assert "NOT ROUTED" in err
    assert "weekly_reset" in err, "the operator needs to be told which setting is missing"


def test_the_weekly_fallback_books_no_quota_and_injects_no_model(env) -> None:
    """It is still a dry pick and still not a model substitution."""
    config = scheduled(claude_c="Mon 15:59 America/Los_Angeles")

    _, executor, _ = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("oracle exploded"), config=config
    )

    assert "--model" not in executor.argv
    assert executor.argv[1] == launcher.BYPASS_FLAG


# -- the setting itself, end to end through the config layer ----------------------------


def test_a_weekly_reset_schedule_survives_the_config_layer(tmp_path) -> None:
    from quota_router.config import load_config
    from quota_router.weekly_reset import WeeklyReset

    path = tmp_path / "config.toml"
    path.write_text(
        "[accounts.claude]\n"
        'weekly_reset = "Mon 15:59 America/Los_Angeles"\n',
        encoding="utf-8",
    )
    config = load_config(env={}, explicit_path=path)

    assert config.account("claude").weekly_reset == WeeklyReset(
        weekday=0, hour=15, minute=59, zone="America/Los_Angeles"
    )
    assert not [w for w in config.warnings if "weekly_reset" in w], config.warnings


def test_an_account_without_the_setting_has_no_schedule() -> None:
    """Opt-in. An unscheduled account simply cannot win the fallback."""
    assert builtin_only().account("claude_b").weekly_reset is None


def test_a_malformed_schedule_is_a_config_error_naming_the_account(tmp_path) -> None:
    """Fail loud. A schedule that quietly defaults would route on a fabricated deadline."""
    from quota_router.config import ConfigError, load_config

    path = tmp_path / "config.toml"
    path.write_text(
        '[accounts.claude]\nweekly_reset = "Mon 15:59"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError) as caught:
        load_config(env={}, explicit_path=path)

    assert "accounts.claude" in str(caught.value)
    assert "weekly_reset" in str(caught.value)


# ======================================================================================
# Refinement -- the fallback skips an account it last saw spent
# ======================================================================================
#
# The weekly-reset heuristic alone cannot tell "expires in an hour with a full week
# left" from "expires in an hour with nothing left". Both look equally urgent, and the
# second is worthless. Run against the 2026-08-24 fleet it would have picked `claude`,
# the exhausted account, for exactly that reason.
#
# The cached usage payload closes the gap, and it is legitimate here for a reason that
# does not generalise: the fallback runs only when there is no LIVE reading, so a stale
# one is not competing with anything. The gate is not age. It is which WEEK the reading
# describes -- a reading taken before the account's last rollover describes a window
# that no longer exists, and acting on it would skip an account that has since refilled.


def _cache_weekly(env: dict, account: str, used_fraction: float, observed_at_s: float) -> None:
    """Write a cache entry for ``account`` under the test's HOME.

    Uses the adapter's own writer and the real payload shape, so this cannot keep
    passing after the format it stands in for has moved.
    """
    from quota_router.providers.claude_oauth import _write_usage_cache, usage_cache_path

    payload = {
        "limits": [
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": used_fraction * 100,
                "resets_at": "2026-08-31T22:59:00+00:00",
                "scope": None,
            }
        ]
    }
    _write_usage_cache(usage_cache_path(account, env), payload, observed_at_s)


#: Monday 08:00, inside the same weekly window as MONDAY_0953 for a Mon 15:59 schedule.
MONDAY_0800 = _datetime.datetime(2026, 8, 24, 8, 0, tzinfo=_LA).timestamp()
#: The Monday BEFORE last week's rollover: same weekday, previous window.
LAST_MONDAY_1200 = _datetime.datetime(2026, 8, 17, 12, 0, tzinfo=_LA).timestamp()


def _fleet():
    """Four scheduled accounts. `claude` resets soonest, so it wins on schedule alone."""
    return scheduled(
        claude="Mon 15:59 America/Los_Angeles",     # today, in ~6h
        claude_d="Tue 11:00 America/Los_Angeles",   # tomorrow
        claude_b="Wed 02:00 America/Los_Angeles",   # Wednesday
        claude_c="Fri 19:00 America/Los_Angeles",   # Friday
    )


def test_without_any_cache_the_soonest_reset_still_wins(env) -> None:
    """The baseline the skip is measured against. This IS the 2026-08-24 outcome."""
    _, executor, err = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("boom"), config=_fleet()
    )

    assert "CLAUDE_CONFIG_DIR" not in executor.env, "claude, whose week expires first"
    assert "WEEKLY-RESET FALLBACK" in err


def test_an_account_last_seen_spent_this_week_is_skipped(env) -> None:
    """The refinement. `claude` still expires first; it just has nothing left to lose."""
    _cache_weekly(env, "claude", used_fraction=1.0, observed_at_s=MONDAY_0800)

    _, executor, err = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("boom"), config=_fleet()
    )

    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-d"), (
        "claude_d resets next after claude"
    )
    assert "skipped" in err and "claude" in err


def test_the_banner_says_which_account_was_skipped_and_why(env) -> None:
    _cache_weekly(env, "claude", used_fraction=1.0, observed_at_s=MONDAY_0800)

    _, _, err = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("boom"), config=_fleet()
    )

    assert "NOT ROUTED" in err
    assert "WEEKLY-RESET FALLBACK" in err
    assert "skipped claude" in err
    assert "100%" in err, f"the reading that justified the skip must be shown; got {err!r}"


def test_a_reading_from_before_the_last_rollover_does_not_skip(env) -> None:
    """The correctness rule. That week is over; the account may be completely fresh.

    Deleting the window check leaves this test asserting the very inversion the
    fallback exists to prevent: skipping an account BECAUSE it was spent last week.
    """
    _cache_weekly(env, "claude", used_fraction=1.0, observed_at_s=LAST_MONDAY_1200)

    _, executor, err = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("boom"), config=_fleet()
    )

    assert "CLAUDE_CONFIG_DIR" not in executor.env, "claude's week rolled over since"
    assert "skipped" not in err


def test_an_account_with_no_cache_entry_is_never_skipped(env) -> None:
    """No reading is not a reading of zero.

    Every candidate except ``claude`` is cached as fully spent, so ``claude`` can only
    win by being *kept* on the strength of having no cache entry at all. Treating a
    missing entry as empty would skip the whole fleet and take the last-resort branch
    instead, which the second assertion pins.
    """
    for account in ("claude_b", "claude_c", "claude_d"):
        _cache_weekly(env, account, used_fraction=1.0, observed_at_s=MONDAY_0800)

    _, executor, err = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("boom"), config=_fleet()
    )

    assert "CLAUDE_CONFIG_DIR" not in executor.env, "claude has no cache and still wins"
    assert "every candidate" not in err.lower()
    assert "skipped claude_b" in err


def test_an_account_with_quota_left_is_not_skipped(env) -> None:
    _cache_weekly(env, "claude", used_fraction=0.61, observed_at_s=MONDAY_0800)

    _, executor, _ = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("boom"), config=_fleet()
    )

    assert "CLAUDE_CONFIG_DIR" not in executor.env


def test_the_skip_bar_is_the_routers_own_eligibility_floor(env) -> None:
    """Not a second threshold invented for this path.

    An account the live router would have excluded on this reading is the same account
    this path should decline to guess onto. One bar, one place to change it.
    """
    import dataclasses

    base = _fleet()
    strict = dataclasses.replace(
        base, eligibility=dataclasses.replace(base.eligibility, min_remaining=0.5)
    )
    _cache_weekly(env, "claude", used_fraction=0.60, observed_at_s=MONDAY_0800)

    _, lenient_exec, _ = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("boom"), config=base
    )
    _, strict_exec, _ = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("boom"), config=strict
    )

    assert "CLAUDE_CONFIG_DIR" not in lenient_exec.env, "40% left clears a 2% floor"
    assert strict_exec.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-d"), (
        "40% left does not clear a 50% floor"
    )


def test_skipping_every_candidate_still_launches_on_the_soonest_to_refill(env) -> None:
    """Nowhere good to go is not a reason to refuse a shell.

    When every cached reading says spent, the soonest rollover is the least bad
    answer: it is the account that becomes usable first.
    """
    for account in ("claude", "claude_b", "claude_c", "claude_d"):
        _cache_weekly(env, account, used_fraction=1.0, observed_at_s=MONDAY_0800)

    code, executor, err = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("boom"), config=_fleet()
    )

    assert code == 0
    assert executor.calls
    assert "CLAUDE_CONFIG_DIR" not in executor.env, "claude refills first, in ~6h"
    assert "every candidate" in err.lower()
    assert "NOT ROUTED" in err


def test_a_corrupt_cache_entry_never_stops_the_launch(env) -> None:
    """This path is reached because something already failed. It cannot be the second."""
    from quota_router.providers.claude_oauth import usage_cache_path

    path = usage_cache_path("claude", env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    code, executor, err = launch_at(
        [], env, now=MONDAY_0953, error=RuntimeError("boom"), config=_fleet()
    )

    assert code == 0
    assert "CLAUDE_CONFIG_DIR" not in executor.env, "an unreadable cache skips nobody"
    assert "WEEKLY-RESET FALLBACK" in err


def test_the_cache_is_not_consulted_when_the_router_answered(env) -> None:
    """A live reading is never second-guessed with a stale one."""
    _cache_weekly(env, "claude_b", used_fraction=1.0, observed_at_s=MONDAY_0800)

    _, executor, err = launch_at(
        [],
        env,
        now=MONDAY_0953,
        answer=selection(_row("claude_b", fits=True)),
        config=_fleet(),
    )

    assert executor.env["CLAUDE_CONFIG_DIR"].endswith("/.claude-b")
    assert "skipped" not in err


# ======================================================================================
# Regression 5 -- 2026-09-06: rank on the cache before guessing from a schedule
# ======================================================================================
#
# A pick blew the three-second deadline (four sequential endpoint reads on a slow
# afternoon) and the weekly-reset fallback landed the session on the account with 3%
# of its week left, while two others had 66%. Fifteen-minute-old readings for every
# account were on disk the whole time; the fallback only ever used them to prune.
#
# The fix asks the real router a second time with the usage adapter in OFFLINE mode --
# one local file per account, no Keychain, no socket -- so the ranking, the eligibility
# floor and the hysteresis all apply to the cached readings. The schedule is consulted
# only when the cache has nothing usable either.

#: The switch the launcher sets on its second attempt. A literal, not the package
#: constant, so this module keeps importing while the constant does not exist yet.
OFFLINE_SWITCH = "QUOTA_ROUTER_USAGE_OFFLINE"


def _offline_only(answer, *, hang: threading.Event | None = None):
    """A router that answers only when asked to read from cache.

    The live attempt hangs on ``hang`` when given, else raises -- the two ways a pick
    fails. Returns ``(select, calls)`` so a test can inspect every call it saw.
    """
    calls: list[dict] = []

    def select(**kwargs):
        calls.append(dict(kwargs))
        if (kwargs.get("env") or {}).get(OFFLINE_SWITCH):
            return answer
        if hang is not None:
            hang.wait(30)
            return selection(_row("claude", fits=True))
        raise RuntimeError("endpoint unreachable")

    return select, calls


def test_a_timed_out_pick_is_ranked_on_cached_usage_before_any_schedule(env) -> None:
    """The 2026-09-06 incident: `claude` expires first, so the schedule picks it; the
    cache says `claude_b` is the one with quota left. The cache must win."""
    released = threading.Event()
    select, _ = _offline_only(selection(_row("claude_b", fits=True)), hang=released)
    try:
        code, executor, err = launch_at(
            [],
            {**env, "CL_PICK_TIMEOUT_S": "0.1"},
            now=MONDAY_0953,
            select=select,
            config=_fleet(),
        )

        assert code == 0
        assert executor.env.get("CLAUDE_CONFIG_DIR", "").endswith("/.claude-b"), err
        assert "CACHED-USAGE FALLBACK" in err
        assert "WEEKLY-RESET" not in err
        assert "ROUTER FAILED" in err and "exceeded" in err
        assert "not live quota" in err.lower()
    finally:
        released.set()


def test_a_crashed_pick_is_ranked_on_cached_usage_too(env) -> None:
    select, _ = _offline_only(selection(_row("claude_d", fits=True)))

    _, executor, err = launch_at([], env, now=MONDAY_0953, select=select, config=_fleet())

    assert executor.env.get("CLAUDE_CONFIG_DIR", "").endswith("/.claude-d"), err
    assert "CACHED-USAGE FALLBACK" in err
    assert "endpoint unreachable" in err, "the original failure must still be named"


def test_a_pick_that_measured_nothing_live_is_ranked_on_cached_usage(env) -> None:
    """Four dark tokens: the live pick completed and read nothing. The readings the
    poller cached while the tokens were still good are the best information left."""
    dark = dark_selection(*(_dark_row(a) for a in ("claude", "claude_b", "claude_c", "claude_d")))

    def select(**kwargs):
        if (kwargs.get("env") or {}).get(OFFLINE_SWITCH):
            return selection(_row("claude_d", fits=True))
        return dark

    _, executor, err = launch_at([], env, now=MONDAY_0953, select=select, config=_fleet())

    assert executor.env.get("CLAUDE_CONFIG_DIR", "").endswith("/.claude-d"), err
    assert "CACHED-USAGE FALLBACK" in err
    assert "could not be read" in err


def test_with_nothing_usable_in_the_cache_the_weekly_fallback_still_runs(env) -> None:
    """Guard, not a regression: the schedule remains the last resort, and the failure
    line then says that neither live nor cached usage was available."""
    nothing_cached = dark_selection(
        *(_dark_row(a) for a in ("claude", "claude_b", "claude_c", "claude_d"))
    )
    select, _ = _offline_only(nothing_cached)

    _, executor, err = launch_at([], env, now=MONDAY_0953, select=select, config=_fleet())

    assert "CLAUDE_CONFIG_DIR" not in executor.env, "claude, whose week expires first"
    assert "WEEKLY-RESET FALLBACK" in err
    assert "CACHED-USAGE" not in err
    assert "live or cached" in err, err


def test_the_cached_usage_fallback_books_no_quota_and_keeps_the_switch_out_of_the_session(
    env,
) -> None:
    select, calls = _offline_only(selection(_row("claude_b", fits=True)))

    _, executor, _ = launch_at([], env, now=MONDAY_0953, select=select, config=_fleet())

    offline_calls = [c for c in calls if (c.get("env") or {}).get(OFFLINE_SWITCH)]
    assert len(offline_calls) == 1, [sorted(c) for c in calls]
    assert offline_calls[0]["record"] is False
    assert offline_calls[0]["only"] == calls[0]["only"], "same candidate set both times"
    # The session must not inherit the switch: Claude Code's own statusline runs
    # `quotapick`, and an inherited switch would blind it to live usage for good.
    assert OFFLINE_SWITCH not in executor.env


def test_the_cached_usage_banner_says_how_old_the_readings_are(env) -> None:
    """A decision on stale data must say how stale, so the operator can judge it."""
    winner = {**_row("claude_b", fits=True), "age_s": 840.0}
    other = {**_row("claude_d", fits=True, score=0.5), "age_s": 300.0}
    select, _ = _offline_only(selection(winner, other))

    _, _, err = launch_at([], env, now=MONDAY_0953, select=select, config=_fleet())

    assert "CACHED-USAGE FALLBACK" in err
    assert "14m" in err, err


def test_the_offline_switch_reaches_the_real_adapter_through_select_account(
    tmp_path, monkeypatch
) -> None:
    """The launcher sets one variable and the adapter reads it, with the whole CLI in
    between. A rename on either side would leave every fake-router test green and the
    real fallback fetching live -- or worse, hanging on the Keychain it was meant to
    avoid. So: the real router, a real cache file, and a guard on both slow paths."""
    import subprocess
    import urllib.request

    from quota_router import select_account
    from quota_router.providers.claude_oauth import _write_usage_cache, usage_cache_path

    home = tmp_path / "home"
    (home / ".claude-b").mkdir(parents=True)
    # HOME has to move in the PROCESS environment too, not just in the injected env
    # dict. Account config_dirs arrive from config as "~/.claude-b" and are expanded
    # with os.path.expanduser, which reads os.environ. Without this the test silently
    # resolved to the author's real ~/.claude-b and passed for the wrong reason.
    monkeypatch.setenv("HOME", str(home))
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        OFFLINE_SWITCH: "1",
    }

    def explode(*args, **kwargs):
        raise AssertionError(f"offline must touch neither the Keychain nor the network: {args!r}")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(urllib.request, "urlopen", explode)

    payload = {
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 10,
                "resets_at": "2026-08-24T22:00:00+00:00",
                "scope": None,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": 40,
                "resets_at": "2026-08-31T22:59:00+00:00",
                "scope": None,
            },
        ]
    }
    _write_usage_cache(usage_cache_path("claude_b", env), payload, MONDAY_0953 - 900.0)

    answer = select_account(
        only=["claude", "claude_b", "claude_c", "claude_d"],
        record=False,
        env=env,
        now_s=MONDAY_0953,
    )

    assert answer.account == "claude_b", json.dumps(answer.to_dict(), indent=1)
    row = next(r for r in answer.ranked if r["account"] == "claude_b")
    assert row["source"] == "cache"
    assert row["age_s"] == pytest.approx(900.0, abs=1.0)
