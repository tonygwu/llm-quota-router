"""``cdx`` -- run ``codex`` on the right account.

``codex`` stays the raw binary. This adds one thing to it: it asks the router which
of the operator's Codex accounts has the most quota about to expire, and runs the
command there, by setting ``CODEX_HOME``.

    cdx exec "prompt"              pick an account, run there
    cdx exec resume <uuid> ...     runs in the home that HOLDS that session
    cdx                            same, for the interactive TUI

It is ``cl`` (:mod:`quota_router.launcher`) for Codex, and deliberately thinner.
Three things differ, and each is a Codex fact rather than a preference:

**1. Nothing is injected.** ``cl`` adds ``--dangerously-skip-permissions`` because
Claude Code has one obvious headless mode. Codex has three, and the operator's own
callers use all of them: ``--sandbox read-only`` for judges and miners,
``--approve-for-me`` for evals, ``--dangerously-bypass-approvals-and-sandbox`` for
agents inside an OS sandbox. In ``clap`` the last flag wins, so appending a bypass
here would silently widen every read-only caller to full access. ``cdx`` therefore
passes argv through byte-for-byte; a caller swaps ``codex`` for ``cdx`` and changes
nothing else.

**2. A session lives in exactly one home.** Codex writes each session's transcript
under ``$CODEX_HOME/sessions/`` and can only resume what is there. So ``resume`` and
``fork`` are routed by *ownership*: the home that holds the file wins, whatever the
router would have scored. Anything that names a session without a UUID -- ``--last``,
a thread name, the interactive picker -- means "the most recent one", which is a
different session in each home; with more than one Codex account that is refused
rather than guessed, and ``CDX_ONLY`` names the home.

**3. A cold home cannot be scored.** The router reads Codex quota out of session
transcripts, so a freshly logged-in account has no reading at all until it has run
once, and an account with no reading is left out of routing rather than scored as
wide open. ``cdx`` says which accounts are in that state and prints the one command
that seeds them, on every launch until it is done.

Everything else follows ``cl``: the pick is capped by a deadline and every failure
still runs the command, on the default account, under a banner that says it was not
routed. The real binary is invoked by absolute path.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Mapping, NoReturn, Sequence, TextIO

from .api import Selection, select_account
from .config import BANNED_EXEC_ENV, Config, load_config
from .launcher import choose_account, measured_any
from .providers.codex_sessions import CODEX_HOME_ENV, DEFAULT_CODEX_HOME
from .types import ACCOUNT_CODEX, PROVIDER_CODEX

__all__ = [
    "Deps",
    "child_env",
    "codex_accounts",
    "main",
    "session_owners",
    "session_request",
]

_PROG: Final[str] = "cdx"

#: Where Homebrew installs the ``codex`` npm shim. Overridable with ``CDX_CODEX_BIN``,
#: never resolved through PATH: an alias of ``codex`` to ``cdx`` would otherwise loop.
DEFAULT_CODEX_BIN: Final[str] = "/opt/homebrew/bin/codex"

#: Wall-clock cap on the pick. The Codex adapter reads local files only, so this is
#: insurance against a slow disk or a bug, not against a Keychain prompt.
DEFAULT_PICK_TIMEOUT_S: Final[float] = 3.0

#: Last-resort fleet when the config yields no Codex account at all.
DEFAULT_ONLY: Final[tuple[str, ...]] = (ACCOUNT_CODEX,)

#: Same convention as ``cl`` and ``quotapick``: the command could not be handed off.
EXIT_CANNOT_EXEC: Final[int] = 127
#: A request this launcher refuses to guess about. Distinct from a router failure,
#: which still launches: here launching would resume the wrong conversation.
EXIT_AMBIGUOUS: Final[int] = 2

_UUID: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_SESSION_VERBS: Final[frozenset[str]] = frozenset({"resume", "fork"})

_DIM: Final[str] = "\033[2m"
_YELLOW: Final[str] = "\033[33m"
_RESET: Final[str] = "\033[0m"


def _execve(path: str, argv: Sequence[str], env: Mapping[str, str]) -> NoReturn:
    os.execve(path, list(argv), dict(env))


@dataclass
class Deps:
    """Everything the launcher reaches outside itself, injectable for tests."""

    select: Callable[..., Selection] = select_account
    exec_: Callable[[str, Sequence[str], Mapping[str, str]], Any] = _execve
    load_config: Callable[..., Config] = load_config
    now: Callable[[], float] = time.time


# ======================================================================================
# Which accounts, and where each one lives
# ======================================================================================


def codex_accounts(config: Config | None) -> tuple[str, ...]:
    """Every Codex account the operator configured, in config order; ``()`` if none."""
    if config is None:
        return ()
    try:
        accounts = config.enabled_accounts()
    except Exception:  # noqa: BLE001 - a broken config must still yield a launch
        return ()
    return tuple(
        account.id
        for account in accounts
        if getattr(account, "provider", "") == PROVIDER_CODEX and getattr(account, "id", "")
    )


def _home_for(account_id: str, config: Config | None, env: Mapping[str, str]) -> Path | None:
    """The ``CODEX_HOME`` that selects ``account_id``, or ``None`` if nothing declares one.

    The default account is the one exception: with no ``config_dir`` it is wherever
    ``codex`` itself would look, ``$CODEX_HOME`` then ``~/.codex``.
    """
    account = config.account(account_id) if config is not None else None
    declared = getattr(account, "config_dir", None) if account is not None else None
    if declared:
        return Path(os.path.expanduser(str(declared)))
    if account_id != ACCOUNT_CODEX:
        return None
    raw = env.get(CODEX_HOME_ENV) or DEFAULT_CODEX_HOME
    home = env.get("HOME")
    if raw.startswith("~/") and home:
        return Path(home) / raw[2:]
    return Path(os.path.expanduser(raw))


def child_env(
    account_id: str, base_env: Mapping[str, str], config: Config | None
) -> dict[str, str]:
    """The environment the command runs under: ``CODEX_HOME`` set for the account.

    ``cdx`` may be run from a shell already pointed at another home, so an inherited
    ``CODEX_HOME`` is never left in place: it is replaced, or for the default account
    with nothing declared, deleted -- absence is how ``codex`` selects ``~/.codex``.
    """
    account = config.account(account_id) if config is not None else None
    overlay = dict(account.exec_env()) if account is not None else {}
    out = {
        key: value
        for key, value in base_env.items()
        if key.strip().upper() not in BANNED_EXEC_ENV
    }
    out.pop(CODEX_HOME_ENV, None)
    out.update(
        {k: v for k, v in overlay.items() if k.strip().upper() not in BANNED_EXEC_ENV}
    )
    return out


# ======================================================================================
# Sessions: routed by who holds them, not by score
# ======================================================================================


@dataclass(frozen=True)
class _SessionRequest:
    """What the argv asks to continue: a UUID, or something meaning "the latest"."""

    verb: str
    session_id: str | None  # a UUID; None when the request is --last, a name, or a picker


def session_request(argv: Sequence[str]) -> _SessionRequest | None:
    """Recognize ``resume``/``fork`` in any of their spellings, or ``None``.

    Covers ``codex resume X``, ``codex fork X``, ``codex exec resume X`` and
    ``codex exec fork X``, with options before or after the verb. The session is the
    first bare token after the verb; ``--last`` or no token at all means "latest".
    """
    for index, token in enumerate(argv):
        if token in _SESSION_VERBS:
            rest = list(argv[index + 1 :])
            if "--last" in rest:
                return _SessionRequest(token, None)
            for candidate in rest:
                if candidate.startswith("-"):
                    continue
                return _SessionRequest(token, candidate if _UUID.match(candidate) else None)
            return _SessionRequest(token, None)
        if token == "--":
            break
    return None


def session_owners(homes: Sequence[tuple[str, Path]], session_id: str) -> list[str]:
    """Account ids whose home holds a transcript for ``session_id``.

    Codex names the file ``rollout-<timestamp>-<uuid>.jsonl`` under a day directory,
    and moves it flat into ``archived_sessions/`` when archived. Both are searched.
    """
    pattern = f"rollout-*-{session_id.lower()}.jsonl"
    owners: list[str] = []
    for account_id, home in homes:
        found = False
        for root, glob in ((home / "sessions", f"*/*/*/{pattern}"), (home / "archived_sessions", pattern)):
            try:
                found = any(True for _ in root.glob(glob))
            except OSError:
                found = False
            if found:
                break
        if found:
            owners.append(account_id)
    return owners


# ======================================================================================
# Routing
# ======================================================================================


@dataclass
class _Route:
    account: str
    config: Config | None
    #: Why this launch is NOT a routing decision, in the words that go in the banner.
    unrouted: str | None = None
    #: The router failed outright. Printed above the banner.
    failure: str | None = None
    #: Set when the account was chosen by session ownership, for the banner.
    pinned_by: str | None = None
    selection: Selection | None = None


@dataclass
class _Attempt:
    value: Any = None
    error: BaseException | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out


def _within_deadline(call: Callable[[], Any], timeout_s: float) -> _Attempt:
    """Run ``call`` on a daemon thread and give up after ``timeout_s``; see ``cl``."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - a router bug must not block a launch
            box["error"] = exc

    thread = threading.Thread(target=work, name="cdx-pick", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        return _Attempt(timed_out=True)
    if "error" in box:
        return _Attempt(error=box["error"])
    return _Attempt(value=box.get("value"))


class _Refused(Exception):
    """A request ``cdx`` will not guess about. The message is the whole report."""


def _route(
    argv: Sequence[str],
    env: Mapping[str, str],
    deps: Deps,
    stderr: TextIO,
) -> _Route:
    timeout_s = _float_env(env, "CDX_PICK_TIMEOUT_S", DEFAULT_PICK_TIMEOUT_S)
    pinned = _list_env(env, "CDX_ONLY", ())

    # The config first, and kept where every later path can see it: it names the
    # fleet, and it is a local TOML read, the cheap half of the pick.
    carried: dict[str, Config] = {}

    def load() -> Config:
        config = deps.load_config(env=dict(env))
        carried["config"] = config
        return config

    loaded = _within_deadline(load, timeout_s)
    config = carried.get("config")
    only = list(pinned) or list(codex_accounts(config)) or list(DEFAULT_ONLY)

    request = session_request(argv)
    if request is not None and not pinned and len(only) > 1:
        homes = [(account_id, _home_for(account_id, config, env)) for account_id in only]
        known = [(account_id, home) for account_id, home in homes if home is not None]
        if request.session_id is None:
            raise _Refused(
                f"{request.verb} without a session UUID means the most recent session, "
                f"which is a different session in each of {', '.join(only)}. Name the "
                f"account: CDX_ONLY=<account> cdx {' '.join(argv)}"
            )
        owners = session_owners(known, request.session_id)
        if len(owners) == 1:
            return _Route(owners[0], config, pinned_by=request.session_id)
        looked = ", ".join(f"{account_id}={home}" for account_id, home in known)
        if not owners:
            raise _Refused(
                f"no configured Codex home holds session {request.session_id} "
                f"(looked in {looked})"
            )
        raise _Refused(
            f"session {request.session_id} is held by more than one home "
            f"({', '.join(owners)}); name one with CDX_ONLY=<account>"
        )

    def pick() -> Selection:
        # record=False: a launch may be abandoned, and it must not be booked.
        return deps.select(only=only, record=False, env=dict(env))

    attempt = (
        _within_deadline(pick, timeout_s)
        if loaded.ok
        else _Attempt(error=loaded.error, timed_out=loaded.timed_out)
    )
    if not attempt.ok:
        cause = (
            f"pick exceeded {timeout_s}s"
            if attempt.timed_out
            else f"pick failed: {type(attempt.error).__name__}: {attempt.error}"
        )
        _debug(env, stderr, f"{cause}; nothing was routed")
        return _Route(
            only[0],
            config,
            unrouted=f"first configured account; ROUTER FAILED: {cause}",
            failure=f"ROUTER FAILED: {cause}",
        )

    selection = attempt.value
    account = choose_account(selection)
    if account is not None:
        return _Route(account, config, selection=selection)
    if not measured_any(selection, only):
        return _Route(
            only[0],
            config,
            unrouted=f"first configured account; usage could not be read for any of {', '.join(only)}",
            selection=selection,
        )
    return _Route(
        only[0],
        config,
        unrouted="first configured account; every account was read and every one is spent",
        selection=selection,
    )


def _unread(selection: Selection | None, only: Sequence[str]) -> list[str]:
    """Accounts the router obtained no reading for at all, in fleet order."""
    if selection is None:
        return []
    return [account_id for account_id in only if not measured_any(selection, [account_id])]


# ======================================================================================
# Small environment helpers
# ======================================================================================


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _list_env(env: Mapping[str, str], name: str, default: Sequence[str]) -> list[str]:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _debug(env: Mapping[str, str], stderr: TextIO, message: str) -> None:
    if env.get("CDX_DEBUG"):
        stderr.write(f"{_DIM}{_PROG}: {message}{_RESET}\n")


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
    """Route, then become ``codex``. Returns only when the launch itself did not happen."""
    args = list(sys.argv[1:] if argv is None else argv)
    environ = dict(os.environ if env is None else env)
    err = sys.stderr if stderr is None else stderr
    resolved = deps or Deps()

    try:
        route = _route(args, environ, resolved, err)
    except _Refused as refusal:
        err.write(f"{_YELLOW}{_PROG}: {refusal}{_RESET}\n")
        return EXIT_AMBIGUOUS

    account, config = route.account, route.config
    launch_env = child_env(account, environ, config)
    if account != ACCOUNT_CODEX and CODEX_HOME_ENV not in launch_env:
        # Only reachable when the router chose an account the config cannot place.
        err.write(
            f"{_YELLOW}{_PROG}: {account} has no config_dir, so there is no CODEX_HOME "
            f"to select it with; set [accounts.{account}] config_dir.{_RESET}\n"
        )
        return EXIT_AMBIGUOUS

    binary = os.path.expanduser(environ.get("CDX_CODEX_BIN") or DEFAULT_CODEX_BIN)
    _debug(environ, err, f"account={account} {CODEX_HOME_ENV}={launch_env.get(CODEX_HOME_ENV, '<unset>')}")

    if route.failure:
        err.write(f"{_YELLOW}{_PROG}: {route.failure}{_RESET}\n")
    if route.unrouted:
        err.write(f"{_YELLOW}{_PROG} ⚠ {account}  [NOT ROUTED -- {route.unrouted}]{_RESET}\n")
    elif route.pinned_by:
        err.write(f"{_DIM}{_PROG} → {account} · holds session {route.pinned_by}{_RESET}\n")
    elif not environ.get("CDX_QUIET"):
        err.write(f"{_DIM}{_PROG} → {account}{_RESET}\n")

    # Never silenced: an account with no reading is one the router cannot spend
    # from, which inverts the whole objective, and the fix is one command.
    fleet = _list_env(environ, "CDX_ONLY", ()) or list(codex_accounts(config)) or list(DEFAULT_ONLY)
    for cold in _unread(route.selection, fleet):
        home = _home_for(cold, config, environ)
        err.write(
            f"{_YELLOW}{_PROG}: {cold} has no usage reading yet (no session transcript "
            f"under {home}); seed it once with: "
            f"{CODEX_HOME_ENV}={home} codex exec --skip-git-repo-check 'reply with ok'{_RESET}\n"
        )

    command = [binary, *args]
    try:
        resolved.exec_(binary, command, launch_env)
    except OSError as exc:
        err.write(f"{_PROG}: cannot execute {binary!r}: {exc} (set CDX_CODEX_BIN)\n")
        return EXIT_CANNOT_EXEC
    return 0
