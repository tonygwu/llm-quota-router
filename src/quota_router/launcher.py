"""``cl`` -- start an interactive Claude Code session on the right account.

``claude`` stays the raw binary. This adds one thing to it: it asks the router which
of the operator's own accounts has the most quota about to expire, and starts the
session there, in bypass-permissions mode.

    cl                        pick an account, start a session
    cl --resume <id>          same, but routed for the model that session ENDED on
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

**4. Everything degrades to a session that still starts.** A quota router that
cannot answer must never be the reason a session cannot start, so the pick is capped
by a wall-clock deadline and every failure path falls through. Two things about *how*
it falls through were bought on 2026-08-24, when a blown deadline put an interactive
session on the one account whose weekly window was 100% spent:

* **A degraded launch never wears the banner of a routed one.** The give-up line was
  byte-identical to a real decision, with the cause written only under ``CL_DEBUG``
  -- which nobody sets before the thing goes wrong. Anything that is not a routing
  decision now says so on stderr, unconditionally, ``CL_QUIET`` included.
* **Where it lands is not a constant.** When the router obtained no usage measurement
  for *any* candidate, :mod:`quota_router.weekly_reset` still knows when each
  account's week rolls over: that is fixed when the account is created and needs no
  network. The fleet's soonest expiry beats the same hardcoded account every time. It
  is strictly a last resort, and :func:`measured_any` is the gate -- a reading that
  says an account is empty is still a reading, and a reading always wins.

**5. The real binary is invoked by absolute path.** Calling bare ``claude`` would
re-enter this launcher if it is ever aliased, and an infinite fork loop at
shell-launch time is a genuinely bad afternoon.

``main()`` takes ``argv``/``env``/``stderr`` and its router and exec calls as
parameters, so the whole launcher is directly callable from a test with no
subprocess, no clock, and no reading of the operator's real machine.
"""

from __future__ import annotations

import glob as glob_mod
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Mapping, NoReturn, Sequence, TextIO

from .api import Selection, select_account
from .config import BANNED_EXEC_ENV, Config, load_config
from .explain import format_duration
from .providers.claude_cli_config import DEFAULT_CLAUDE_CONFIG_DIR_NAMES
from .providers.claude_oauth import USAGE_OFFLINE_ENV, cached_weekly_usage
from .types import ACCOUNT_CLAUDE
from .weekly_reset import earliest_weekly_reset

__all__ = [
    "Deps",
    "child_env",
    "choose_account",
    "detect_model",
    "main",
    "measured_any",
]

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

#: How much of a transcript's tail to read per step when looking for the last model.
#: One chunk covers the end of any ordinary session; the loop widens only if that much
#: contains no model id at all.
TAIL_CHUNK_BYTES: Final[int] = 256 * 1024

#: Stop widening after this much. A transcript whose final quarter-gigabyte names no
#: model is not one detection can answer for, and this runs on the critical path of
#: opening a terminal -- returning ``None`` falls through to the settings default,
#: which is a real answer rather than a guess.
TAIL_MAX_BYTES: Final[int] = 8 * 1024 * 1024

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
    #: Read exactly once per launch, so every deadline this run reports is measured
    #: against the same instant. Injected by tests, which is the only way an assertion
    #: about "in 6h06m" can be exact rather than approximate.
    now: Callable[[], float] = time.time


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


def _model_in(line: str) -> str | None:
    """The real model id on one transcript line, or ``None``.

    Sentinels (``<synthetic>``, ``<none>``, ...) are excluded by shape rather than by
    a blocklist: they name a locally generated turn, not something a router can score.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return None  # a partially written trailing line is normal
    if not isinstance(record, Mapping):
        return None
    message = record.get("message")
    model = message.get("model") if isinstance(message, Mapping) else None
    if isinstance(model, str) and model and not model.startswith("<"):
        return model
    return None


def _last_model(path: Path) -> str | None:
    """The last real model this session ran, scanning backwards from the end.

    WHY THE LAST ONE AND NOT THE MOST COMMON
    ----------------------------------------
    The router is being asked a forward-looking question -- what will this session
    burn when it resumes -- and the model it ended on is the best available answer.
    How the history was distributed answers something else that nothing consumes.

    This replaced a majority vote, which was itself a fix for twelve trailing
    ``<synthetic>`` sentinels outvoting 628 real records. But sentinels are already
    excluded by :func:`_model_in`, so the vote solved that a second time and bought a
    new failure: a live session ran 650 Fable turns, was switched to Opus, ran 310
    more, and resumed routed against Fable's scoped weekly sub-cap -- a window the
    resumed work does not touch, while the general pool it does touch went unchecked.
    Both fallbacks said Opus. Detection was worse than returning nothing.

    WHY BACKWARDS
    -------------
    Reading the tail is what the question asks for, and it is also cheaper than the
    4000-line head scan it replaces: transcripts reach hundreds of megabytes, and this
    runs on the critical path of opening a terminal. A late switch used to be
    invisible past the cap; now the late records are the first ones read.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            tail = b""
            while position > 0:
                step = min(TAIL_CHUNK_BYTES, position)
                position -= step
                handle.seek(position)
                tail = handle.read(step) + tail

                # Drop the leading fragment: it is the tail of a line whose start is
                # still further back, and parsing half a record yields nothing anyway.
                lines = tail.split(b"\n")
                whole = lines[1:] if position > 0 else lines
                for raw in reversed(whole):
                    if not raw.strip():
                        continue
                    model = _model_in(raw.decode("utf-8", errors="replace"))
                    if model:
                        return model

                if len(tail) >= TAIL_MAX_BYTES:
                    # A transcript this long with no model id in it is not one we can
                    # answer for. Stop rather than read a huge file to say so.
                    return None
    except OSError:
        return None
    return None


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
            model = _last_model(path)
            if model:
                return model
    return _settings_model(home)


# ======================================================================================
# Which account can actually serve this session?
# ======================================================================================


@dataclass(frozen=True, slots=True)
class _Route:
    """Where the session is going, and on what -- possibly not what was asked for."""

    account: str
    config: Config | None
    model: str | None
    #: The model that could not be served anywhere, when a substitution happened.
    #: Non-``None`` is the ONLY condition under which ``cl`` passes ``--model``.
    substituted_from: str | None = None
    #: When the original model becomes available again, if the router knew.
    available_at: float | None = None
    #: Why this launch is NOT a routing decision, in the words that go in the banner.
    #: ``None`` means the router really chose this account, and is the only state in
    #: which the plain ``cl -> account`` line is truthful.
    unrouted: str | None = None
    #: The router failed outright, as opposed to answering "nobody has quota". Printed
    #: on its own line above the banner, because a crashed router and an exhausted
    #: fleet want completely different things from the operator.
    failure: str | None = None


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


def measured_any(selection: Selection, candidates: Sequence[str]) -> bool:
    """Did the router get a usage reading for ANY of ``candidates``?

    This is the gate on the weekly-reset fallback, and the whole reason that fallback
    is safe. ``remaining`` is ``None`` for an account nothing could be read for -- an
    expired token, an unreachable endpoint -- and a number for one that was read.
    **Zero is a number.** "The bar says this account is empty" and "the bar could not
    be read" produce the same routing outcome and are opposite facts, and only the
    second one may fall back to a schedule. A schedule is a static guess; it must
    never be allowed to overrule a live reading, including a reading of zero.

    Both ``ranked`` and ``excluded`` are searched. Which of the two an unreadable
    account lands in is a rendering detail of the decision layer, and reading only one
    of them would make this gate depend on it.
    """
    wanted = {str(candidate) for candidate in candidates}
    for row in (*selection.ranked, *selection.excluded):
        if not isinstance(row, Mapping):
            continue
        if str(row.get("account")) in wanted and row.get("remaining") is not None:
            return True
    return False


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
    now_s: float,
) -> _Route:
    """Ask the router for an account, capped and fully insulated.

    Returns the account id to launch on. Every failure -- an unreachable oracle, a
    broken config, a pick that ran past its deadline, a fleet with nothing left --
    still yields a launch, and every one of them is marked as not-a-routing-decision
    so the banner cannot pass it off as one.
    """
    timeout_s = _float_env(env, "CL_PICK_TIMEOUT_S", DEFAULT_PICK_TIMEOUT_S)
    only = _list_env(env, "CL_ONLY", DEFAULT_ONLY)

    def pick_with(which: str | None, *, offline: bool = False) -> Selection:
        call_env = dict(env)
        if offline:
            # Read by the usage adapter, and by nothing the session inherits: the
            # child is built from ``env``, not from this copy.
            call_env[USAGE_OFFLINE_ENV] = "1"
        return deps.select(
            model=which,
            only=only,
            # --dry-run in the shell version: this launcher may be run and then
            # abandoned (wrong directory, changed mind), and a session that never
            # starts must not be booked against anyone's quota.
            record=False,
            env=call_env,
        )

    # The config is loaded FIRST and left where the failure path can still find it.
    # It used to be returned alongside the pick, which meant a blown deadline threw it
    # away with the abandoned thread -- and the fallback that needs the weekly reset
    # schedules is reached only when the deadline blows. Local TOML reads are also the
    # cheap half of this call, so doing them first costs the fast path nothing.
    carried: dict[str, Config] = {}

    def pick() -> Selection:
        carried["config"] = deps.load_config(env=dict(env))
        return pick_with(model)

    attempt = _within_deadline(pick, timeout_s)
    config = carried.get("config")

    if not attempt.ok:
        # Same degradation, deliberately different report: a crash is a bug in the
        # router and a timeout is a blocked credential read, they have nothing in
        # common but the outcome.
        cause = (
            f"pick exceeded {timeout_s}s"
            if attempt.timed_out
            else f"pick failed: {type(attempt.error).__name__}: {attempt.error}"
        )
        _debug(env, stderr, f"{cause}; nothing was routed live")
        cached = _route_from_cache(cause, pick_with, timeout_s, config, only, model, env, stderr)
        return cached or _unrouted(
            f"ROUTER FAILED: {cause}. No usable usage data, live or cached, for any account.",
            config,
            only,
            model,
            now_s,
            env,
            stderr,
        )

    selection = attempt.value
    account = choose_account(selection)
    if account is not None:
        return _Route(account, config, model)

    # Nothing can serve the requested model. Before giving up, see whether the
    # operator declared something to fall back to.
    fallback = getattr(config, "fallback_model", None) if config else None
    explicit, _resume = _scan_args(argv)
    if fallback and not explicit and fallback != model:
        retry = _within_deadline(lambda: pick_with(fallback), timeout_s)
        if retry.ok:
            alternative = choose_account(retry.value)
            if alternative is not None:
                return _Route(
                    alternative,
                    config,
                    fallback,
                    substituted_from=model,
                    available_at=selection.available_at,
                )

    # The router answered and no account fits. Two very different things look like
    # this, and only one of them may consult a schedule.
    if not measured_any(selection, only):
        cause = f"usage could not be read for any of {', '.join(only)}"
        cached = _route_from_cache(cause, pick_with, timeout_s, config, only, model, env, stderr)
        return cached or _unrouted(
            f"ROUTER FAILED: {cause}. No usable usage data, live or cached, for any account.",
            config,
            only,
            model,
            now_s,
            env,
            stderr,
        )

    stderr.write(
        f"{_YELLOW}{_PROG}: no account can serve this right now; using the "
        f"default. Check `quotapick status`.{_RESET}\n"
    )
    return _Route(
        ACCOUNT_CLAUDE,
        config,
        model,
        unrouted="hardcoded default; every account was read and every one is spent",
    )


# ======================================================================================
# Second attempt: the same router, on the readings already on disk
# ======================================================================================


def _oldest_reading_age(selection: Selection, only: Sequence[str]) -> float | None:
    """How old the oldest reading behind this decision is, or ``None`` if unreported."""
    wanted = {str(candidate) for candidate in only}
    ages = [
        float(row["age_s"])
        for row in (*selection.ranked, *selection.excluded)
        if isinstance(row, Mapping)
        and str(row.get("account")) in wanted
        and isinstance(row.get("age_s"), (int, float))
    ]
    return max(ages) if ages else None


def _route_from_cache(
    cause: str,
    pick_with: Callable[..., Selection],
    timeout_s: float,
    config: Config | None,
    only: Sequence[str],
    model: str | None,
    env: Mapping[str, str],
    stderr: TextIO,
) -> _Route | None:
    """Ask the router again with the usage adapter reading only its cache.

    The live pick failed -- a blown deadline, a crash, or four dark tokens. The poller
    leaves a reading for every account on disk, minutes old on an ordinary day, and
    the router's own ranking, eligibility floor and hysteresis all apply to those
    readings exactly as they would to live ones. On 2026-09-06 that reading was
    fifteen minutes old for every account while the schedule-only fallback put the
    session on the one with 3% of its week left.

    Capped like the live attempt. The offline read touches one file per account and
    nothing slow, but a cap that exists only on the path that already failed is a cap
    the next surprise walks straight past.

    Returns ``None`` when the cache has nothing usable either, so the caller can fall
    through to the schedule.
    """
    attempt = _within_deadline(lambda: pick_with(model, offline=True), timeout_s)
    if not attempt.ok:
        detail = (
            f"cached pick exceeded {timeout_s}s"
            if attempt.timed_out
            else f"cached pick failed: {type(attempt.error).__name__}: {attempt.error}"
        )
        _debug(env, stderr, detail)
        return None
    account = choose_account(attempt.value)
    if account is None:
        _debug(env, stderr, "cached readings fit no account")
        return None
    age_s = _oldest_reading_age(attempt.value, only)
    basis = (
        f"ranked on readings cached up to {format_duration(age_s)} ago"
        if age_s is not None
        else "ranked on cached readings of unreported age"
    )
    return _Route(
        account,
        config,
        model,
        failure=f"ROUTER FAILED: {cause}; routed on cached usage instead.",
        unrouted=f"CACHED-USAGE FALLBACK, not live quota: {basis}",
    )


# ======================================================================================
# Landing somewhere sensible when there is nothing to route on
# ======================================================================================


def _schedules(config: Config | None, only: Sequence[str]) -> dict[str, Any]:
    """Weekly reset schedules for the candidates that could actually be launched.

    Filtered here rather than in :func:`quota_router.weekly_reset.earliest_weekly_reset`,
    which knows nothing about candidate sets or disabled accounts and should not: the
    pure layer answers "which of these expires first", and which accounts are *these*
    is this launcher's question.
    """
    if config is None:
        return {}
    found: dict[str, Any] = {}
    for account_id in only:
        account = config.account(account_id)
        if account is None or not account.enabled or account.weekly_reset is None:
            continue
        found[account_id] = account.weekly_reset
    return found


@dataclass(frozen=True, slots=True)
class _Spent:
    """A candidate the cache says had nothing left, in the week we are still in."""

    account: str
    used_fraction: float


def _prune_spent(
    schedules: Mapping[str, Any],
    config: Config | None,
    only: Sequence[str],
    now_s: float,
    env: Mapping[str, str],
    stderr: TextIO,
) -> tuple[dict[str, Any], list[_Spent]]:
    """Drop candidates whose last CACHED weekly reading was already spent.

    The schedule alone cannot tell a week that expires in an hour with everything
    left from one that expires in an hour with nothing left. Both look equally
    urgent and only one is worth having, and on 2026-08-24 that difference was the
    whole incident. The cached payload closes it.

    A stale reading is admissible **here specifically** because this path runs only
    when there is no live one -- it competes with nothing. Two rules keep it honest:

    * **Which week, not how old.** A reading is used only if it was taken inside the
      window we are still in, decided by the account's own schedule. A reading from
      before the last rollover describes a window that no longer exists, and skipping
      on it would reject an account that has since refilled completely -- the exact
      inversion this fallback exists to avoid. Age would answer the wrong question:
      six days old can be current, ten minutes old can be a week out of date.
    * **The router's own bar.** ``eligibility.min_remaining`` is what the live
      decision layer excludes on, so an account it would have rejected on this
      reading is the one this path declines to guess onto. Inventing a second
      threshold would give the operator two numbers to keep in agreement.

    Every uncertainty keeps the account: no cache file, an unparseable one, a reading
    from another week. Skipping is the destructive move, so it needs positive
    evidence, and this whole path is already a recovery from a failure.
    """
    floor = config.eligibility.min_remaining if config is not None else 0.0
    kept: dict[str, Any] = {}
    spent: list[_Spent] = []
    for account_id, schedule in schedules.items():
        try:
            reading = cached_weekly_usage(account_id, env)
        except Exception as exc:  # noqa: BLE001 - see the docstring; never wedge a shell
            _debug(env, stderr, f"{account_id}: cached usage unreadable: {exc!r}")
            reading = None
        if reading is None or not schedule.same_window(reading.observed_at_s, now_s):
            kept[account_id] = schedule
        elif 1.0 - reading.used_fraction <= floor:
            spent.append(_Spent(account_id, reading.used_fraction))
        else:
            kept[account_id] = schedule
    return kept, spent


def _unrouted(
    failure: str,
    config: Config | None,
    only: Sequence[str],
    model: str | None,
    now_s: float,
    env: Mapping[str, str],
    stderr: TextIO,
) -> _Route:
    """Land the session somewhere defensible when nothing could be measured.

    Only ever called with no usage reading for any candidate. The weekly reset
    schedule is all that is left, and it is enough for the router's own objective:
    spend the pool whose week expires soonest. :func:`_prune_spent` then removes the
    candidates the cache already knows are empty.

    With no schedule configured this degrades to the historical behaviour, the
    hardcoded default account -- but says which setting would have made it better,
    because a fallback nobody knows exists is a fallback nobody configures.
    """
    schedules = _schedules(config, only)
    spent: list[_Spent] = []
    try:
        if schedules:
            kept, spent = _prune_spent(schedules, config, only, now_s, env, stderr)
            # Everyone looks spent. Still launch, and on the soonest rollover: that is
            # the account which becomes usable first, which is the most useful thing
            # left to say. Ranking the full set again gives exactly that.
            choice = earliest_weekly_reset(kept or schedules, now_s)
        else:
            choice = None
    except Exception as exc:  # noqa: BLE001 - a bad schedule must not wedge a shell
        _debug(env, stderr, f"weekly reset fallback failed: {type(exc).__name__}: {exc}")
        choice = None

    if choice is None:
        return _Route(
            ACCOUNT_CLAUDE,
            config,
            model,
            failure=failure,
            unrouted=(
                f"hardcoded default -- no [accounts.<id>] weekly_reset is configured "
                f"for {', '.join(only)}, so there was nothing better to pick on"
            ),
        )

    account, resets_at = choice
    if len(spent) == len(schedules):
        detail = (
            f"every candidate's cached weekly reading is spent this week, so "
            f"{account} -- which refills first{_in_hint(resets_at, now_s)} -- is the "
            f"least bad"
        )
    else:
        considered = ", ".join(name for name in only if name not in {s.account for s in spent})
        detail = (
            f"of {considered}, {account}'s weekly window expires first"
            f"{_in_hint(resets_at, now_s)}"
        )
        if spent:
            detail += "; skipped " + ", ".join(
                f"{item.account} (cached weekly reading {item.used_fraction:.0%} spent "
                f"this week)"
                for item in spent
            )
    return _Route(
        account,
        config,
        model,
        failure=failure,
        unrouted=f"WEEKLY-RESET FALLBACK, not live quota: {detail}",
    )


@dataclass(frozen=True, slots=True)
class _Attempt:
    """The outcome of a capped call: an answer, a crash, or a blown deadline.

    All three degrade the same way, so it would be tempting to collapse them into
    ``None``. They are kept apart because they have nothing else in common: a crash is
    a bug in the router and a timeout is something blocking on the machine, and the
    only place either is ever reported is the debug line.
    """

    value: Any = None
    error: BaseException | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out


def _within_deadline(call: Callable[[], Any], timeout_s: float) -> _Attempt:
    """Run ``call`` on a daemon thread and give up on it after ``timeout_s``.

    A thread rather than a signal or a subprocess: the work is a library call that may
    block on the Keychain, ``SIGALRM`` would fire inside whatever the operator's own
    Claude session later does, and re-spawning a process to get a timeout is what the
    shell version had to do. Abandoning the thread is safe because the very next thing
    this process does is ``exec`` itself away, which discards it.
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
    if thread.is_alive():
        return _Attempt(timed_out=True)
    if "error" in box:
        return _Attempt(error=box["error"])
    return _Attempt(value=box.get("value"))


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


def _duration(seconds: float) -> str:
    """``42m`` / ``6h06m``. Never a clock time.

    A deadline is a wall-clock instant in some timezone, and formatting one against
    the machine's local zone is how a stamp ends up an hour off for half the year. A
    duration is unambiguous wherever it is read, and needs no zone at all.
    """
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(1, minutes)}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _reset_hint(available_at: float | None, now_s: float) -> str:
    """`` (back in 42m)`` when the router knew, empty when it did not."""
    if available_at is None or available_at - now_s <= 0:
        return ""
    return f" (back in {_duration(available_at - now_s)})"


def _in_hint(deadline_s: float | None, now_s: float) -> str:
    """`` in 6h06m`` when the deadline is ahead, empty when it is not."""
    if deadline_s is None or deadline_s - now_s <= 0:
        return ""
    return f" in {_duration(deadline_s - now_s)}"


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

    # One clock read for the whole launch, so the deadlines this run reports are all
    # measured against the same instant and cannot disagree with each other.
    now_s = resolved.now()

    route = _route(args, environ, model, resolved, err, now_s)
    account, config, model = route.account, route.config, route.model
    launch_env = child_env(account, environ, config)

    binary = os.path.expanduser(environ.get("CL_CLAUDE_BIN") or DEFAULT_CLAUDE_BIN)
    _debug(
        environ,
        err,
        f"account={account} dir={launch_env.get('CLAUDE_CONFIG_DIR', '<default ~/.claude>')}",
    )
    if route.failure:
        # The router itself broke. Unconditional, and above the banner: on 2026-08-24
        # this was written only under CL_DEBUG, so a blown deadline reached the
        # operator as a confident-looking routing decision onto a spent account.
        err.write(f"{_YELLOW}{_PROG}: {route.failure}{_RESET}\n")
    suffix = f" · {model}" if model else ""
    if route.unrouted:
        # Never silenced by CL_QUIET. CL_QUIET suppresses the routine line, and there
        # is nothing routine about a launch nobody chose.
        err.write(
            f"{_YELLOW}{_PROG} ⚠ {account}{suffix}  "
            f"[NOT ROUTED -- {route.unrouted}]{_RESET}\n"
        )
    elif not environ.get("CL_QUIET"):
        err.write(f"{_DIM}{_PROG} → {account}{suffix}{_RESET}\n")
    if route.substituted_from:
        # Always shown, CL_QUIET or not: the session is about to run something other
        # than what was asked for, and a capability downgrade nobody notices is worse
        # than a launch that fails outright.
        err.write(
            f"{_YELLOW}{_PROG}: {route.substituted_from} is exhausted on every "
            f"account; substituted {model}"
            f"{_reset_hint(route.available_at, now_s)}.{_RESET}\n"
        )

    # The ONLY place --model is injected. Detection elsewhere is a prediction used to
    # score the pick; here it is a deliberate override, and an override that is not
    # passed through does not happen. Safe from colliding with the operator's own
    # --model because a substitution is never attempted when one was given.
    override = ["--model", model] if (route.substituted_from and model) else []
    command = [binary, BYPASS_FLAG, *override, *args]
    try:
        resolved.exec_(binary, command, launch_env)
    except OSError as exc:
        err.write(f"{_PROG}: cannot execute {binary!r}: {exc}\n")
        return EXIT_CANNOT_EXEC
    # Only reachable under an injected exec_; the real one never returns.
    return 0
