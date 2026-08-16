"""``cl`` -- start an interactive Claude Code session on the right account.

``claude`` stays the raw binary. This adds one thing to it: it asks the router which
of the operator's own accounts has the most quota about to expire, and starts the
session there, in bypass-permissions mode.

    cl                        pick an account, start a session
    cl --resume <id>          same, but routed for the model that session used
    cl --model opus[1m] ...   an explicit --model wins over session detection

This is the router's **reference consumer**: the worked example of the integration
contract, handed to other teams. It is deliberately thin, and everything it does not
do itself it takes from the library rather than reimplementing -- the account-to-
directory mapping in particular, which is the piece most likely to be "tidied up"
into a silent breakage by someone who does not know rule 3 below.

DESIGN NOTES
------------

**1. A ranked winner is not a promise it can serve you.** The router's objective is
quota about to expire, so an account whose five-hour window is fully spent can
legitimately rank first -- it has the most weekly quota at risk. Correct for a batch
scheduler that can wait; useless for an interactive session, which lands in a shell
that cannot answer a prompt until the window resets. So take the top-ranked account
that actually ``fits``, and say so honestly when none do.

**2. Transcripts contain sentinel "models".** ``"<synthetic>"`` marks a locally
generated turn. The model matters because Fable draws on a separate weekly allowance
from the general pool -- an account can be wide open on one and exhausted on the
other -- so misreading it routes against the wrong window entirely.

**3. The default account is selected by the ABSENCE of the config-dir variable.**
Account A's config lives at ``~/.claude.json``, *outside* ``~/.claude``. Setting
``CLAUDE_CONFIG_DIR`` to the directory makes Claude Code look inside, find nothing,
and scaffold a brand-new empty account -- a silent, confusing failure. The rule is
enforced in :meth:`quota_router.config.AccountConfig.exec_env`, and this module
deletes the inherited variable rather than merely omitting it: ``cl`` is routinely
run from a shell that is already pointed at another account, where an omission would
leave the session exactly where it was.

**4. Everything degrades to "just run claude on the default account".** A quota
router that cannot answer must never be the reason a session cannot start, so the
pick is capped by a wall-clock deadline and every failure path falls through.

**5. The real binary is invoked by absolute path.** Calling bare ``claude`` would
re-enter this launcher if it is ever aliased, and an infinite fork loop at
shell-launch time is a genuinely bad afternoon.

``main()`` takes ``argv``/``env``/``stderr`` and its router and exec calls as
parameters, so the whole launcher is directly callable from a test with no
subprocess, no clock, and no reading of the operator's real machine.
"""

from __future__ import annotations

import collections
import glob as glob_mod
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Mapping, NoReturn, Sequence, TextIO

from .api import Selection, select_account
from .config import BANNED_EXEC_ENV, Config, load_config
from .providers.claude_cli_config import DEFAULT_CLAUDE_CONFIG_DIR_NAMES
from .types import ACCOUNT_CLAUDE

__all__ = ["Deps", "child_env", "choose_account", "detect_model", "main"]

_PROG: Final[str] = "cl"

#: Where Claude Code installs itself. Overridable, but never resolved through PATH --
#: see design note 5.
DEFAULT_CLAUDE_BIN: Final[str] = "~/.local/bin/claude"

#: ``--dangerously-skip-permissions`` actually enters bypass mode.
#: ``--allow-dangerously-skip-permissions`` merely offers it as a togglable option.
#: They are one word apart and do different things; do not swap them.
BYPASS_FLAG: Final[str] = "--dangerously-skip-permissions"

#: Wall-clock cap on the pick, in seconds. Sized for a shell launch: the failure this
#: guards against is a Keychain prompt with nobody to answer it, which would otherwise
#: hang every single new terminal.
DEFAULT_PICK_TIMEOUT_S: Final[float] = 3.0

#: The interactive fleet: the Claude accounts this operator can actually be dropped
#: into. Taken from the config-directory map so registering a new slot (``claude_d``
#: was added this way) does not need a second edit here.
DEFAULT_ONLY: Final[tuple[str, ...]] = tuple(DEFAULT_CLAUDE_CONFIG_DIR_NAMES)

#: How many transcript lines to count models over.
#:
#: JUDGMENT CALL, stated plainly: this reads the *head* of the file, so a session that
#: switched models after 4000 records is routed by the model it started with. The cap
#: exists because this runs on the critical path of opening a terminal and transcripts
#: reach hundreds of megabytes. Head-biased-but-fast beat exact-but-slow; the failure
#: mode is routing against the wrong window, not a wrong session.
TRANSCRIPT_LINE_CAP: Final[int] = 4000

#: Router-level failure: there was nothing to hand the session off to. Matches
#: ``quotapick``'s own convention (:data:`quota_router.cli.EXIT_ROUTER_FAILURE`), and
#: is the shell's "command not found" code, which is what actually happened.
EXIT_CANNOT_EXEC: Final[int] = 127

_DIM: Final[str] = "\033[2m"
_YELLOW: Final[str] = "\033[33m"
_RESET: Final[str] = "\033[0m"


# ======================================================================================
# Injectable dependencies
# ======================================================================================


def _execve(path: str, argv: Sequence[str], env: Mapping[str, str]) -> NoReturn:
    """Become the real binary.

    ``exec``, not ``spawn``: the session inherits this process's terminal, signals and
    exit code directly. Nothing after this line ever runs -- including the pick thread
    below, which the image replacement discards along with everything else.
    """
    os.execve(path, list(argv), dict(env))


@dataclass
class Deps:
    """Everything the launcher reaches outside itself, injectable for tests."""

    select: Callable[..., Selection] = select_account
    exec_: Callable[[str, Sequence[str], Mapping[str, str]], Any] = _execve
    load_config: Callable[..., Config] = load_config


# ======================================================================================
# Which model is this session going to use?
# ======================================================================================


def _scan_args(argv: Sequence[str]) -> tuple[str | None, str | None]:
    """Pull an explicit ``--model`` and a ``--resume`` id out of the user's arguments.

    Deliberately *not* argparse: these arguments belong to ``claude``, not to us, and
    a parser would have to know every flag Claude Code accepts in order to know which
    ones take a value. Anything unrecognised is none of our business.
    """
    model: str | None = None
    resume: str | None = None
    args = list(argv)
    for index, arg in enumerate(args):
        following = args[index + 1] if index + 1 < len(args) else None
        if arg == "--model":
            # A trailing "--model" with no value is claude's error to report, not a
            # reason for us to route as though a model had been named.
            if following:
                return following, resume
        elif arg.startswith("--model="):
            return arg[len("--model=") :], resume
        elif arg in ("-r", "--resume"):
            if following:
                resume = following
        elif arg.startswith("--resume="):
            resume = arg[len("--resume=") :]
    return model, resume


def _transcript_path(home: Path, session_id: str) -> Path | None:
    """Find a session transcript under ``~/.claude/projects/<project>/<id>.jsonl``.

    The id is escaped: it is user input on the way into a glob, and a session id with
    a bracket in it should find nothing rather than match something else.
    """
    root = home / ".claude" / "projects"
    pattern = str(root / "*" / f"{glob_mod.escape(session_id)}.jsonl")
    matches = sorted(glob_mod.glob(pattern))
    return Path(matches[0]) if matches else None


def _dominant_model(path: Path) -> str | None:
    """The model this transcript is mostly made of, ignoring sentinels.

    Transcripts carry sentinel "models" like ``"<synthetic>"`` for locally generated
    turns. Taking the LAST model in file order let twelve trailing sentinels outvote
    628 real records, and the session was then routed as an unknown class instead of
    against Fable's own weekly allowance. Count the real ids and take the dominant
    one; a handful of sentinels cannot outvote the actual model.
    """
    counts: collections.Counter[str] = collections.Counter()
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= TRANSCRIPT_LINE_CAP:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # a partially written trailing line is normal
                if not isinstance(record, Mapping):
                    continue
                message = record.get("message")
                model = message.get("model") if isinstance(message, Mapping) else None
                if isinstance(model, str) and model and not model.startswith("<"):
                    counts[model] += 1
    except OSError:
        return None
    return counts.most_common(1)[0][0] if counts else None


def _settings_model(home: Path) -> str | None:
    """The operator's configured default model, or ``None`` if it cannot be read."""
    try:
        settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    model = settings.get("model") if isinstance(settings, Mapping) else None
    return model if isinstance(model, str) and model else None


def detect_model(argv: Sequence[str], *, home: Path) -> str | None:
    """Which model class should the pick be scored against?

    Precedence: an explicit ``--model``, then the model a resumed session actually
    used, then the operator's configured default. ``None`` at every step is a real
    answer -- the router scores against the general pool when it is not told a model,
    which is better than guessing one and gating on the wrong window.
    """
    explicit, resume_id = _scan_args(argv)
    if explicit:
        return explicit
    if resume_id:
        path = _transcript_path(home, resume_id)
        # A transcript that yields nothing usable falls through to the settings
        # default rather than giving up: no answer and a plausible answer are not
        # equally useful when the alternative is free.
        if path is not None:
            model = _dominant_model(path)
            if model:
                return model
    return _settings_model(home)


# ======================================================================================
# Which account can actually serve this session?
# ======================================================================================


def choose_account(selection: Selection) -> str | None:
    """The top-ranked account that can serve a call right now, or ``None``.

    See design note 1: the router's winner is its best *score*. ``fits`` is the
    separate question of whether the account has budget left at all, and for an
    interactive session it is the binding one.
    """
    if selection.fits and selection.account:
        return selection.account
    for row in selection.ranked:
        if isinstance(row, Mapping) and row.get("fits") and row.get("account"):
            return str(row["account"])
    return None


def child_env(
    account_id: str, base_env: Mapping[str, str], config: Config | None
) -> dict[str, str]:
    """The environment the session runs under.

    The account's overlay comes from the config layer, so the account-to-directory
    mapping (and rule 3 -- the default account is selected by absence) lives in one
    place for every consumer, not copied into each one.

    The variable is **deleted** when the chosen account does not name one. ``cl`` is
    routinely run from a shell already pointed at another account, so merging an empty
    overlay over an inherited ``CLAUDE_CONFIG_DIR`` would leave the session on
    whichever account the terminal happened to be holding -- the exact mistake this
    router exists to prevent.
    """
    account = config.account(account_id) if config is not None else None
    overlay = account.exec_env() if account is not None else {}
    overlay = {
        key: value
        for key, value in overlay.items()
        if key.strip().upper() not in BANNED_EXEC_ENV
    }

    # A proxy token makes Claude Code bill the metered API instead of the account's
    # subscription, which would defeat the router while reporting success. Same
    # blocklist `quotapick exec` applies; this router never proxies.
    out = {
        key: value
        for key, value in base_env.items()
        if key.strip().upper() not in BANNED_EXEC_ENV
    }

    config_dir_var = (
        account.config_dir_env_var if account is not None else "CLAUDE_CONFIG_DIR"
    )
    if config_dir_var not in overlay:
        out.pop(config_dir_var, None)
    out.update(overlay)
    return out


def _route(
    argv: Sequence[str],
    env: Mapping[str, str],
    model: str | None,
    deps: Deps,
    stderr: TextIO,
) -> tuple[str, Config | None]:
    """Ask the router for an account, capped and fully insulated.

    Returns the account id to launch on, falling back to the default account on every
    failure: an unreachable oracle, a broken config, a pick that ran past its deadline,
    and a fleet with nothing left all mean "start the session anyway".
    """
    timeout_s = _float_env(env, "CL_PICK_TIMEOUT_S", DEFAULT_PICK_TIMEOUT_S)
    only = _list_env(env, "CL_ONLY", DEFAULT_ONLY)

    def pick() -> tuple[Selection, Config]:
        selection = deps.select(
            model=model,
            only=only,
            # --dry-run in the shell version: this launcher may be run and then
            # abandoned (wrong directory, changed mind), and a session that never
            # starts must not be booked against anyone's quota.
            record=False,
            env=dict(env),
        )
        return selection, deps.load_config(env=dict(env))

    result = _within_deadline(pick, timeout_s)
    if result is None:
        _debug(env, stderr, f"pick exceeded {timeout_s}s; using the default account")
        return ACCOUNT_CLAUDE, None

    selection, config = result
    account = choose_account(selection)
    if account is None:
        stderr.write(
            f"{_YELLOW}{_PROG}: no account can serve this right now; using the "
            f"default. Check `quotapick status`.{_RESET}\n"
        )
        return ACCOUNT_CLAUDE, config
    return account, config


def _within_deadline(call: Callable[[], Any], timeout_s: float) -> Any | None:
    """Run ``call`` on a daemon thread and give up on it after ``timeout_s``.

    A thread rather than a signal or a subprocess: the work is a library call that may
    block on the Keychain, ``SIGALRM`` would fire inside whatever the operator's own
    Claude session later does, and re-spawning a process to get a timeout is what the
    shell version had to do. Abandoning the thread is safe because the very next thing
    this process does is ``exec`` itself away, which discards it.

    ``None`` means "no usable answer" -- deadline exceeded *or* the call raised. Both
    have exactly one correct response here, and it is not to stop the operator working.
    """
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - a router bug must not block a shell
            box["error"] = exc

    thread = threading.Thread(target=work, name="cl-pick", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive() or "error" in box:
        return None
    return box.get("value")


# ======================================================================================
# Small environment helpers
# ======================================================================================


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    # A zero or negative cap would mean "never wait", which reads like a way to
    # disable routing but actually just makes every launch silently unrouted.
    return value if value > 0 else default


def _list_env(env: Mapping[str, str], name: str, default: Sequence[str]) -> list[str]:
    raw = (env.get(name) or "").strip()
    if not raw:
        return list(default)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or list(default)


def _debug(env: Mapping[str, str], stderr: TextIO, message: str) -> None:
    if env.get("CL_DEBUG"):
        stderr.write(f"{_PROG}: {message}\n")


# ======================================================================================
# Entry point
# ======================================================================================


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stderr: TextIO | None = None,
    deps: Deps | None = None,
) -> int:
    """Route, then become ``claude``. Returns only when the launch itself failed."""
    args = list(sys.argv[1:] if argv is None else argv)
    environ = dict(os.environ if env is None else env)
    err = sys.stderr if stderr is None else stderr
    resolved = deps or Deps()

    home = Path(environ.get("HOME") or os.path.expanduser("~"))
    model = detect_model(args, home=home)
    _debug(environ, err, f"model={model or '<none>'}")

    account, config = _route(args, environ, model, resolved, err)
    launch_env = child_env(account, environ, config)

    binary = os.path.expanduser(environ.get("CL_CLAUDE_BIN") or DEFAULT_CLAUDE_BIN)
    _debug(
        environ,
        err,
        f"account={account} dir={launch_env.get('CLAUDE_CONFIG_DIR', '<default ~/.claude>')}",
    )
    if not environ.get("CL_QUIET"):
        suffix = f" · {model}" if model else ""
        err.write(f"{_DIM}{_PROG} → {account}{suffix}{_RESET}\n")

    command = [binary, BYPASS_FLAG, *args]
    try:
        resolved.exec_(binary, command, launch_env)
    except OSError as exc:
        err.write(f"{_PROG}: cannot execute {binary!r}: {exc}\n")
        return EXIT_CANNOT_EXEC
    # Only reachable under an injected exec_; the real one never returns.
    return 0
