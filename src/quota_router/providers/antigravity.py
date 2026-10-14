"""Antigravity source: ``agy -p "/usage" --output-format json``.

HISTORY
-------
Until 2026-10-12 this module was titled "there isn't one". Verified on the operator's
machine then: ``agy`` exposed no usage subcommand and wrote no quota fields anywhere on
disk, so the adapter reported every pool as **unobservable** -- no windows,
``confidence=0.0`` -- and the only signal it offered was a failure-learned deadline (see
below). That was the honest answer at the time, and it stays the answer whenever the read
described here fails: inventing a number ("probably half full", or one derived from call
counts) is how a router silently sends every call to an exhausted pool. Capability picks
could therefore use the pools only as ``unmetered = "fallback"``, and over two weeks the
router never chose one through routing while the Antigravity app showed them nearly
unused.

THE SOURCE
----------
agy 1.3.3 answers the ``/usage`` slash command headlessly. Measured 2026-10-12: about
3.5-4.5 s, exit 0, ``num_turns: 0`` and ``usage.total_tokens: 0``, so no agent turn runs
and no quota is spent. The structured answer is ``command.data.groups[]``, one group per
quota pool, each with a ``weekly`` and a ``5h`` bucket carrying ``remaining_fraction``
and ``reset_time``. ``tests/fixtures/agy_usage_raw.json`` is one raw response, verbatim.
An unstarted 5-hour window slides (its reset is now + 5 h); the weekly reset stays fixed.

One call describes BOTH pools of one Google account. The configured pools are therefore
grouped by how they are launched (the ``launch-plan`` recipe: ``argv_prefix``, the
binary, ``unset_env``, and the environment overlay without ``AGY_MODEL``), each group is
read once, and its groups are mapped to its pools:

* ``"Gemini Models"`` -> the pool whose ``AGY_MODEL`` does not contain "claude";
* ``"Claude and GPT models"`` -> the pool whose ``AGY_MODEL`` contains "claude".

That is the selector rule the config documents and
:func:`quota_router.capability.antigravity_flavour` implements, used here unchanged.

STRICT, NEVER GUESSED
---------------------
A response is accepted only when the process exits 0, the output is a JSON object,
``status == "SUCCESS"``, ``command.name == "usage"`` and ``num_turns == 0``. Anything
else gives no windows to any pool of that account and a warning that names the cause. An
unknown or nameless group is a warning that names it and maps to no pool. A pool whose
group is missing, or appears twice, gets no windows. A bucket with an unknown ``window``,
a duplicate window, a ``remaining_fraction`` outside ``[0, 1]`` or an unparseable
``reset_time`` makes that pool's whole reading unusable: dropping only the strange
bucket would make the pool look healthier than it is.

IDENTITY FROM AGY'S OWN LOG, ON EVERY READ
------------------------------------------
The response carries no account identity at all. The launch recipe (the macOS user,
through ``macos_user`` and the ``agy-as-user`` wrapper) selects the Google account, but a
login can change under it: an agy logout once reverted to the other account days later,
and the desktop app shares agy's Keychain login. So every read passes ``--log-file
/dev/stderr``, and agy's sign-in lines (``OAuth: authenticated successfully as ...``)
name the account that answered. That address is compared with the pool's declared
``identity_email``:

* the same address: the reading is used, and the snapshot carries it as ``identity``;
* another address: the numbers belong to another account, and a launch would spend that
  account's quota. The pool gets no windows and is marked unavailable, so a pick
  excludes it until the login is fixed. This holds even when the read itself failed;
* no address in the log, or no ``identity_email`` declared: the reading cannot be tied
  to the pool, so the pool gets no windows, as for any failed read.

Measured 2026-10-14: 1,473 of 1,474 successful ``/usage`` runs on one account logged its
address, and none logged another. The cache records the recipe and the logged address of
each reading, and the same comparison runs on every cached reading served.

NEVER ON THE PICK PATH
----------------------
``cl`` and ``cdx`` cap a whole pick at 3 s and this read takes about 4 s. The adapter
therefore spawns agy only when constructed with ``live=True``, which the CLI does for
``status`` alone (the launchd poller runs it every 120 s). Every other command serves
the last reading from the per-account usage cache the Claude reader already keeps
(:func:`~quota_router.providers.claude_oauth.usage_cache_path`, same record shape, same
120 s reuse window and 5 h stale bound), reported as ``source="cache"`` with its age. A
live read is ``source="live"``. ``QUOTA_ROUTER_USAGE_OFFLINE`` keeps even ``status``
from spawning. A failed live read is written to the cache too, so ``pick`` reports the
same failure ``status`` saw instead of serving the reading before it.

Each live read has its own timeout (:data:`DEFAULT_USAGE_TIMEOUT_S`). The child starts
in its own session and the whole process group is signalled on timeout, because the
second account's agy runs as a grandchild under ``sudo``.

FAILURE-LEARNED DEADLINES
-------------------------
Unchanged. When a call to a pool comes back with a quota error,
:mod:`quota_router.failure_text` extracts a deadline (``"Individual quota reached. ...
Resets in 25m54s"``) and whoever persists that hands it back here as
``exhausted_until``. Until that deadline passes the pool is marked unavailable, measured
or not.

A SECOND GOOGLE ACCOUNT IS A SECOND MACOS USER
----------------------------------------------
This adapter reports on whatever pools the operator's config declares. A second Google
account is NOT reached by a second ``HOME``: ``agy`` keeps one Keychain item per macOS
USER, so every ``HOME`` under one user serves the SAME Google account -- measured
2026-09-10, where 567 grades believed to span two accounts had all come from one. A
second account is a second macOS user, named by ``macos_user``. Read the README section
"A second Antigravity account is a second macOS user". The argv here is built from the
account's own launch-plan properties by :func:`agy_argv_env`, the same function the
capability refresher uses, never rebuilt by hand: the recipe has three parts (``env``,
``argv_prefix``, ``unset_env``) and applying only ``env`` returns a good answer from the
wrong account.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from ..capability import antigravity_flavour
from ..types import (
    ACCOUNT_ANTIGRAVITY_CLAUDE,
    ACCOUNT_ANTIGRAVITY_GEMINI,
    SOURCE_ASSUMED,
    SOURCE_CACHE,
    SOURCE_LIVE,
    TIER_UNKNOWN,
    WINDOW_KEY_5H,
    WINDOW_KEY_7D,
    AccountSnapshot,
    Identity,
    Window,
    normalize_tier,
)
from .base import (
    FIVE_HOUR_S,
    SEVEN_DAY_S,
    ProviderAdapter,
    parse_iso8601,
    parse_timestamp,
    read_json_file,
)
from .claude_oauth import (
    DEFAULT_USAGE_STALE_MAX_S,
    DEFAULT_USAGE_TTL_S,
    USAGE_OFFLINE_ENV,
    _read_usage_cache,
    _write_usage_cache,
    usage_cache_path,
)
from .codex_app_server import _stop

__all__ = [
    "ANTIGRAVITY_BINARY",
    "ANTIGRAVITY_ACCOUNT_IDS",
    "ANTIGRAVITY_STATE_ENV",
    "BUCKET_WINDOWS",
    "DEFAULT_USAGE_TIMEOUT_S",
    "GROUP_FOR_FLAVOUR",
    "MACOS_USER_AGY_BINARY",
    "USAGE_ARGS",
    "AgyCompleted",
    "AgyMissing",
    "AntigravityAdapter",
    "UsageRejected",
    "agy_argv_env",
    "default_run_agy",
    "launch_key",
    "map_usage",
    "signed_in_as",
]

#: The CLI the executor spawns for these pools, for the calling macOS user.
ANTIGRAVITY_BINARY: Final[str] = "agy"

#: The binary the macOS-user wrapper allowlists. Used only when such an account sets no
#: ``command``; the wrapper refuses anything else, loudly.
MACOS_USER_AGY_BINARY: Final[str] = "/usr/local/bin/agy"

#: The two default pools behind that one binary.
ANTIGRAVITY_ACCOUNT_IDS: Final[tuple[str, ...]] = (
    ACCOUNT_ANTIGRAVITY_GEMINI,
    ACCOUNT_ANTIGRAVITY_CLAUDE,
)

#: Optional path to a JSON file of failure-learned deadlines, written by whatever layer
#: handles failures: ``{"antigravity_gemini": 1786752000}``. Values may be epoch seconds
#: or ISO-8601. Unset means "no learned state", not an error.
ANTIGRAVITY_STATE_ENV: Final[str] = "QUOTA_ROUTER_ANTIGRAVITY_STATE"

#: What follows the binary to ask for the usage table instead of running a prompt. The
#: log goes to stderr, which the runner captures, so its sign-in lines can name the
#: account that answered. ``/dev/stderr`` and not a file path: the second account's agy
#: runs as another macOS user, who cannot write to the caller's directories, while the
#: inherited stderr reaches both.
USAGE_ARGS: Final[tuple[str, ...]] = (
    "-p", "/usage", "--output-format", "json", "--log-file", "/dev/stderr",
)

#: agy's two sign-in log lines, each naming the Google account it signed in as:
#: ``applyAuthResult: email=X, authMethod=...`` and ``OAuth: authenticated successfully
#: as X``.
_SIGNED_IN: Final[re.Pattern[str]] = re.compile(
    r"(?:applyAuthResult: email=|OAuth: authenticated successfully as )([^\s,]+@[^\s,]+)"
)

#: Budget for one Google account's whole read, process start included. Measured on
#: 2026-10-12: 3.6-6.1 s for one account alone, and 5.7-12.9 s for the second account
#: (through ``sudo``) when both accounts read at once, as ``status`` does. A first cap of
#: 10 s timed that read out in a live ``status`` run. This is about 2.3 times the slowest
#: observed read. Never on the pick path, so the 3 s launcher cap does not bound it; the
#: 120 s poll interval does.
DEFAULT_USAGE_TIMEOUT_S: Final[float] = 30.0

#: The environment variable that selects a pool behind one Google account.
POOL_SELECTOR_ENV: Final[str] = "AGY_MODEL"

#: Pool flavour (:func:`~quota_router.capability.antigravity_flavour`) -> the group name
#: ``/usage`` reports for it. Matched exactly; anything else is a warning.
GROUP_FOR_FLAVOUR: Final[Mapping[str, str]] = {
    "gemini": "Gemini Models",
    "claude": "Claude and GPT models",
}

#: Bucket ``window`` -> (window key, window length). Anything else is a warning.
BUCKET_WINDOWS: Final[Mapping[str, tuple[str, float]]] = {
    "weekly": (WINDOW_KEY_7D, SEVEN_DAY_S),
    "5h": (WINDOW_KEY_5H, FIVE_HOUR_S),
}

#: Confidence for a pool with no reading at all. Not "low" -- *none*.
UNOBSERVABLE_CONFIDENCE: Final[float] = 0.0
#: Confidence for a pool read from the vendor's own answer, live or cached. Age is
#: carried by the windows' ``observed_at_s``, which the staleness checks already read.
MEASURED_CONFIDENCE: Final[float] = 1.0

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_OUTPUT_TAIL_CHARS: Final[int] = 300

#: Type of the injected deadline source: a mapping or a callable per account id.
ExhaustedUntil = Mapping[str, Any] | Callable[[str], Any]


# ======================================================================================
# Launching agy the way the account is launched
# ======================================================================================


class AgyMissing(Exception):
    """The calling user's ``agy`` is not on PATH."""


def agy_argv_env(
    account: Any,
    args: Sequence[str],
    env: Mapping[str, str],
    *,
    which: Callable[[str], str | None] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """``(argv, child_env)`` that run agy with ``args`` as ``account`` is launched.

    Built from the account's own launch-plan properties (``exec_env()``,
    ``launch_argv_prefix``, ``launch_unset_env``, ``command``), all three parts of the
    recipe, so the call reaches the same Google account ``exec`` would. ``account`` is
    duck-typed like the policy layer's ``AccountConfig``.

    Raises:
        AgyMissing: the account is launched by the calling user and its binary is not
            on ``env``'s PATH.
    """
    child = dict(env)
    child.update(account.exec_env())
    for name in account.launch_unset_env:
        child.pop(name, None)
    prefix = list(account.launch_argv_prefix)
    if prefix:
        binary = account.command or MACOS_USER_AGY_BINARY
    else:
        resolve = which or (lambda name: shutil.which(name, path=env.get("PATH")))
        binary = resolve(account.exec_command) or ""
        if not binary:
            raise AgyMissing(f"{account.exec_command!r} is not on PATH ({env.get('PATH')})")
    return [*prefix, binary, *args], child


def launch_key(account: Any) -> list[Any]:
    """What selects the Google account an Antigravity pool draws on, as plain JSON.

    Two pools with the same key are two pools of ONE Google account, so one ``/usage``
    call serves both. Everything in the launch recipe except the pool selector counts:
    an overlay variable this module does not understand might select something, and
    reading it twice costs one more read, while merging it could cost a wrong account.
    """
    prefix = list(account.launch_argv_prefix)
    binary = (account.command or MACOS_USER_AGY_BINARY) if prefix else account.exec_command
    overlay = {
        str(key): str(value)
        for key, value in account.exec_env().items()
        if key != POOL_SELECTOR_ENV
    }
    return [
        [str(part) for part in prefix],
        str(binary),
        sorted(str(name) for name in account.launch_unset_env),
        [[key, overlay[key]] for key in sorted(overlay)],
    ]


@dataclass(frozen=True, slots=True)
class AgyCompleted:
    """What one agy process produced."""

    returncode: int
    stdout: str
    stderr: str


#: Signature of the injected agy runner: ``(argv, env, timeout_s) -> AgyCompleted``,
#: raising :class:`subprocess.TimeoutExpired` when the budget runs out.
AgyRunner = Callable[[Sequence[str], Mapping[str, str], float], AgyCompleted]


def default_run_agy(
    argv: Sequence[str], env: Mapping[str, str], timeout_s: float
) -> AgyCompleted:
    """Run agy in its own session; on timeout, end its whole process group and re-raise.

    ``subprocess.run`` kills only the direct child. For the second Google account that
    child is ``sudo``, and agy is its grandchild, so the group is signalled instead, with
    the same stop routine the Codex app-server reader uses (SIGTERM, a short grace, then
    SIGKILL). ``sudo`` relays the SIGTERM to the command it runs.
    """
    process = subprocess.Popen(  # noqa: S603 - argv is built here, never a shell string
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        start_new_session=True,
        close_fds=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except BaseException:  # the timeout, and anything else that abandons the child
        _stop(process)
        raise
    return AgyCompleted(
        returncode=process.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


# ======================================================================================
# Parsing the /usage answer
# ======================================================================================


class UsageRejected(Exception):
    """The answer is not a usable ``/usage`` reading; the message is the cause."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def usage_groups(payload: Any) -> list[Any]:
    """The ``command.data.groups`` list, after the identity and shape checks.

    Raises :class:`UsageRejected` unless this is the ``/usage`` command's own answer:
    ``status == "SUCCESS"``, ``command.name == "usage"`` and ``num_turns == 0``. A
    nonzero turn count means agy ran a model turn, whose text could look like a usage
    table without being one.
    """
    if not isinstance(payload, Mapping):
        raise UsageRejected("the response is not a JSON object")
    status = payload.get("status")
    if status != "SUCCESS":
        raise UsageRejected(f"status is {status!r}, not 'SUCCESS'")
    command = payload.get("command")
    name = command.get("name") if isinstance(command, Mapping) else None
    if name != "usage":
        raise UsageRejected(f"command.name is {name!r}, not 'usage'")
    turns = payload.get("num_turns")
    if not (_is_int(turns) and turns == 0):
        raise UsageRejected(
            f"num_turns is {turns!r}, not 0, so agy did not answer the /usage command itself"
        )
    data = command.get("data")
    groups = data.get("groups") if isinstance(data, Mapping) else None
    if not isinstance(groups, list) or not groups:
        raise UsageRejected("command.data.groups is missing or empty")
    return groups


def _group_windows(name: str, group: Mapping[str, Any], observed_at_s: float) -> tuple[Window, ...]:
    """Every bucket of one group as a window, or :class:`UsageRejected`."""
    buckets = group.get("buckets")
    if not isinstance(buckets, list) or not buckets:
        raise UsageRejected(f"group {name!r} has no buckets")
    windows: list[Window] = []
    seen: set[str] = set()
    for index, bucket in enumerate(buckets):
        if not isinstance(bucket, Mapping):
            raise UsageRejected(f"group {name!r} bucket #{index} is not an object")
        label = bucket.get("id", f"#{index}")
        kind = bucket.get("window")
        if kind not in BUCKET_WINDOWS:
            raise UsageRejected(
                f"group {name!r} bucket {label!r} has unknown window {kind!r} "
                f"(known: {', '.join(sorted(BUCKET_WINDOWS))})"
            )
        key, length_s = BUCKET_WINDOWS[kind]
        if key in seen:
            raise UsageRejected(f"group {name!r} reports window {kind!r} twice")
        seen.add(key)
        remaining = bucket.get("remaining_fraction")
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, (int, float))
            or not math.isfinite(float(remaining))
            or not 0.0 <= float(remaining) <= 1.0
        ):
            raise UsageRejected(
                f"group {name!r} bucket {label!r} remaining_fraction is not in [0, 1]: "
                f"{remaining!r}"
            )
        resets_at_s = parse_iso8601(bucket.get("reset_time"))
        if resets_at_s is None:
            raise UsageRejected(
                f"group {name!r} bucket {label!r} reset_time is not a timestamp: "
                f"{bucket.get('reset_time')!r}"
            )
        windows.append(
            Window(
                key=key,
                used_fraction=1.0 - float(remaining),
                length_s=length_s,
                resets_at_s=resets_at_s,
                observed_at_s=observed_at_s,
            )
        )
    return tuple(windows)


def map_usage(
    payload: Any, pools: Sequence[tuple[str, str]], observed_at_s: float
) -> tuple[dict[str, tuple[Window, ...]], list[str]]:
    """Map one ``/usage`` answer onto the pools of one Google account.

    Args:
        payload: The parsed JSON answer.
        pools: ``(account_id, flavour)`` for every pool of that account.
        observed_at_s: When the answer was read.

    Returns:
        ``(windows by account id, warnings)``. A pool absent from the mapping gets no
        windows, and a warning says why.

    Raises:
        UsageRejected: the answer as a whole is not a ``/usage`` reading.
    """
    groups = usage_groups(payload)
    ids = ", ".join(account_id for account_id, _flavour in pools)
    known = set(GROUP_FOR_FLAVOUR.values())
    warnings: list[str] = []
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for index, group in enumerate(groups):
        if not isinstance(group, Mapping):
            warnings.append(f"{ids}: /usage group #{index} is not an object; it maps to no pool")
            continue
        name = group.get("name")
        if not isinstance(name, str) or not name.strip():
            warnings.append(f"{ids}: /usage group #{index} has no name; it maps to no pool")
            continue
        by_name.setdefault(name, []).append(group)
        if name not in known:
            warnings.append(f"{ids}: /usage reported an unknown group {name!r}; it maps to no pool")

    out: dict[str, tuple[Window, ...]] = {}
    for account_id, flavour in pools:
        expected = GROUP_FOR_FLAVOUR[flavour]
        found = by_name.get(expected, [])
        if not found:
            warnings.append(
                f"{account_id}: /usage reported no {expected!r} group "
                f"(it reported {sorted(by_name)!r}); no windows"
            )
            continue
        if len(found) > 1:
            warnings.append(
                f"{account_id}: /usage reported the {expected!r} group {len(found)} times; "
                f"no windows"
            )
            continue
        try:
            out[account_id] = _group_windows(expected, found[0], observed_at_s)
        except UsageRejected as exc:
            warnings.append(f"{account_id}: {exc}; no windows")
    return out, warnings


def _failure_line(log_text: str) -> str | None:
    """agy's own one-line account of why print mode failed, if its log has one.

    The log is on stderr now, and its last lines are shutdown noise ("Waiting for
    migrations to complete"), so a plain tail would hide the cause.
    """
    for line in reversed((log_text or "").splitlines()):
        if line.startswith("E") and "Print mode:" in line:
            return line.split("] ", 1)[-1].strip()[:_OUTPUT_TAIL_CHARS]
    return None


def signed_in_as(log_text: str) -> tuple[str, ...]:
    """Every distinct Google account agy's log says it signed in as, in first-seen order."""
    seen: dict[str, str] = {}
    for email in _SIGNED_IN.findall(log_text or ""):
        seen.setdefault(email.casefold(), email)
    return tuple(seen.values())


def _identity_split(
    pools: Sequence[_Pool], served: Sequence[str]
) -> tuple[list[_Pool], dict[str, str], dict[str, str]]:
    """``(confirmed pools, {id: mismatch}, {id: why unconfirmed})`` for one account's read.

    A mismatch is evidence that the read reached another Google account. An unconfirmed
    pool has no such evidence, only no proof either way.
    """
    confirmed: list[_Pool] = []
    mismatched: dict[str, str] = {}
    unconfirmed: dict[str, str] = {}
    answered = ", ".join(served)
    for pool in pools:
        declared = getattr(pool.account, "identity_email", None)
        if not served:
            unconfirmed[pool.id] = (
                "agy's log named no signed-in Google account, so the reading cannot be tied "
                f"to {declared or 'this pool'}"
            )
        elif not declared:
            unconfirmed[pool.id] = (
                f"no identity_email is declared for this pool, so a reading from {answered} "
                f"cannot be checked; declare the account it should reach"
            )
        elif {email.casefold() for email in served} != {declared.casefold()}:
            mismatched[pool.id] = f"agy is signed in as {answered}, not the declared {declared}"
        else:
            confirmed.append(pool)
    return confirmed, mismatched, unconfirmed


# ======================================================================================
# The adapter
# ======================================================================================


def _format_deadline(epoch_s: float) -> str:
    """Render a deadline as UTC ISO-8601 for the human-readable note."""
    try:
        return (
            datetime.fromtimestamp(epoch_s, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return str(epoch_s)


@dataclass(frozen=True, slots=True)
class _Pool:
    account: Any
    flavour: str
    key: list[Any]

    @property
    def id(self) -> str:
        return str(self.account.id)


@dataclass(frozen=True, slots=True)
class _Reading:
    """What one pool's windows came from, for the note."""

    windows: tuple[Window, ...]
    source: str
    observed_at_s: float
    identity: str  # the address agy's log signed in as


@dataclass(frozen=True, slots=True)
class _Cached:
    """One pool's cache record, judged."""

    response: Any | None
    fetched_at_s: float | None
    error: str | None
    problem: str | None  # why the record cannot be served at all
    served: tuple[str, ...] = ()  # the addresses that read's log signed in as


@dataclass(frozen=True, slots=True)
class _Live:
    """One live ``/usage`` call for one Google account."""

    response: Any | None
    error: str | None
    served: tuple[str, ...]


class AntigravityAdapter(ProviderAdapter):
    """Each Antigravity pool's quota from agy's ``/usage``, plus failure-learned deadlines.

    Args:
        account_ids: Pools to report on when no launch plans are given. Such pools are
            reported but never read: there is no recipe to read them with.
        accounts: The policy layer's account objects (duck-typed ``AccountConfig``),
            one per pool. Their launch plans decide how each Google account is read.
            When given, they also decide which pools are reported.
        live: Spawn agy for a reading older than ``ttl_s``. Only ``status`` sets it.
            Without it, the adapter serves the cache and starts no process.
        timeout_s: Budget for one Google account's read.
        ttl_s: How long a live reading is reused before ``live`` reads again.
        stale_max_s: Oldest cached reading served when not reading live.
        run_agy: Injected agy runner; defaults to :func:`default_run_agy`.
        exhausted_until: Failure-learned deadlines -- a mapping ``account_id -> epoch/ISO``
            or a callable taking an account id. Values in the past (or ``None``) mean
            "not known to be exhausted".
        state_path: Explicit path to the JSON deadline file; otherwise
            :data:`ANTIGRAVITY_STATE_ENV` is consulted, and if that is unset no file is
            read at all.
        which: Injected PATH lookup, for the binary-presence check and the argv.
        env: Environment mapping, injected for tests; also the child's environment.
    """

    name = "antigravity"
    default_timeout_s: float = DEFAULT_USAGE_TIMEOUT_S

    def __init__(
        self,
        *,
        account_ids: Iterable[str] = ANTIGRAVITY_ACCOUNT_IDS,
        accounts: Iterable[Any] | None = None,
        live: bool = False,
        timeout_s: float = DEFAULT_USAGE_TIMEOUT_S,
        ttl_s: float = DEFAULT_USAGE_TTL_S,
        stale_max_s: float = DEFAULT_USAGE_STALE_MAX_S,
        run_agy: AgyRunner | None = None,
        exhausted_until: ExhaustedUntil | None = None,
        state_path: Path | str | None = None,
        binary: str = ANTIGRAVITY_BINARY,
        which: Callable[[str], str | None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._accounts = tuple(accounts) if accounts is not None else None
        if self._accounts:
            self._account_ids = tuple(str(account.id) for account in self._accounts)
        else:
            self._account_ids = tuple(account_ids)
        self._live = bool(live)
        self.timeout_s = float(timeout_s)
        self._ttl_s = float(ttl_s)
        self._stale_max_s = float(stale_max_s)
        self._run_agy = run_agy
        self._exhausted_until = exhausted_until
        self._state_path = state_path
        self._binary = binary
        self._which = which
        self._env = env
        self.warnings: tuple[str, ...] = ()

    def _environ(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def _resolve(self, name: str) -> str | None:
        if self._which is not None:
            return self._which(name)
        return shutil.which(name, path=self._environ().get("PATH"))

    # -- deadline resolution -------------------------------------------------------------

    def _state_file(self) -> Path | None:
        if self._state_path is not None:
            return Path(os.path.expanduser(str(self._state_path)))
        raw = self._environ().get(ANTIGRAVITY_STATE_ENV)
        if raw and raw.strip():
            return Path(os.path.expanduser(raw.strip()))
        return None

    def _deadlines(self) -> dict[str, float]:
        """Merge injected deadlines with the optional state file. Never raises."""
        merged: dict[str, float] = {}

        state_file = self._state_file()
        if state_file is not None:
            payload = read_json_file(state_file)
            if isinstance(payload, Mapping):
                source: Any = payload.get("exhausted_until", payload)
                if isinstance(source, Mapping):
                    for account_id, value in source.items():
                        deadline = parse_timestamp(value)
                        if isinstance(account_id, str) and deadline is not None:
                            merged[account_id] = deadline

        provided = self._exhausted_until
        if callable(provided):
            for account_id in self._account_ids:
                deadline = parse_timestamp(provided(account_id))
                if deadline is not None:
                    merged[account_id] = deadline
        elif isinstance(provided, Mapping):
            for account_id, value in provided.items():
                deadline = parse_timestamp(value)
                if isinstance(account_id, str) and deadline is not None:
                    merged[account_id] = deadline

        return merged

    # -- grouping and the cache ----------------------------------------------------------

    def _groups(self, warnings: list[str]) -> list[list[_Pool]]:
        """The configured pools, grouped by Google account, in config order."""
        groups: dict[str, list[_Pool]] = {}
        for account in self._accounts or ():
            try:
                pool = _Pool(account=account, flavour=antigravity_flavour(account),
                             key=launch_key(account))
            except Exception as exc:  # noqa: BLE001 - a malformed account must not raise
                warnings.append(
                    f"{getattr(account, 'id', account)}: no launch plan to read /usage with "
                    f"({type(exc).__name__}: {exc})"
                )
                continue
            groups.setdefault(json.dumps(pool.key), []).append(pool)
        for pools in groups.values():
            flavours = [pool.flavour for pool in pools]
            for flavour in sorted(set(flavours)):
                same = [pool.id for pool in pools if pool.flavour == flavour]
                if len(same) > 1:
                    warnings.append(
                        f"{', '.join(same)}: these pools draw on the SAME {flavour} pool of one "
                        f"Google account (same launch recipe), so each shows the same reading"
                    )
        return list(groups.values())

    def _cache_path(self, account_id: str) -> Path:
        return usage_cache_path(account_id, self._env)

    def _cached(self, pool: _Pool) -> _Cached:
        payload, fetched_at_s, _not_before = _read_usage_cache(self._cache_path(pool.id))
        if payload is None or fetched_at_s is None:
            return _Cached(None, None, None, "no cached /usage reading")
        if not isinstance(payload, Mapping) or payload.get("launch") != pool.key:
            return _Cached(
                None, fetched_at_s, None,
                "the cached /usage reading was made with a different launch recipe",
            )
        served = payload.get("served")
        if not isinstance(served, list) or not all(isinstance(e, str) for e in served):
            return _Cached(
                None, fetched_at_s, None,
                "the cached /usage reading predates the identity check",
            )
        error = payload.get("error")
        if isinstance(error, str) and error:
            return _Cached(None, fetched_at_s, error, None, tuple(served))
        if payload.get("response") is None:
            return _Cached(None, fetched_at_s, None, "the cached /usage record holds no response")
        return _Cached(payload.get("response"), fetched_at_s, None, None, tuple(served))

    def _write(self, pools: Sequence[_Pool], fetched_at_s: float, read: _Live) -> None:
        for pool in pools:
            _write_usage_cache(
                self._cache_path(pool.id),
                {"launch": pool.key, "response": read.response, "error": read.error,
                 "served": list(read.served)},
                fetched_at_s,
            )

    # -- reading -------------------------------------------------------------------------

    def _read_live(self, pools: Sequence[_Pool]) -> _Live:
        """One ``/usage`` call for one Google account, with the addresses its log named.

        The log is read even when the call failed: a sign-in line naming another
        account is evidence of a wrong login whatever happened after it.
        """
        try:
            argv, child = agy_argv_env(pools[0].account, USAGE_ARGS, self._environ(),
                                       which=self._resolve)
        except AgyMissing as exc:
            return _Live(None, str(exc), ())
        runner = self._run_agy if self._run_agy is not None else default_run_agy
        try:
            done = runner(argv, child, self.timeout_s)
        except subprocess.TimeoutExpired:
            return _Live(None, (
                f"{' '.join(argv)} timed out after {self.timeout_s:g}s; its process group "
                f"was stopped"
            ), ())
        except OSError as exc:
            return _Live(None, f"could not run {' '.join(argv)}: {exc}", ())
        except Exception as exc:  # noqa: BLE001 - an adapter never raises
            return _Live(None, f"{' '.join(argv)}: {type(exc).__name__}: {exc}", ())
        served = signed_in_as(done.stderr)
        if done.returncode != 0:
            tail = _failure_line(done.stderr) or (done.stderr or done.stdout or "").strip()[
                -_OUTPUT_TAIL_CHARS:
            ]
            return _Live(
                None,
                f"{' '.join(argv)} exited {done.returncode}" + (f": {tail}" if tail else ""),
                served,
            )
        try:
            response = json.loads(done.stdout)
        except ValueError:
            return _Live(None, (
                f"{' '.join(argv)} output is not JSON: "
                f"{(done.stdout or '').strip()[:_OUTPUT_TAIL_CHARS]!r}"
            ), served)
        try:
            usage_groups(response)
        except UsageRejected as exc:
            return _Live(None, str(exc), served)
        return _Live(response, None, served)

    @staticmethod
    def _confirm(
        pools: Sequence[_Pool],
        served: Sequence[str],
        error: str | None,
        refused: dict[str, str],
        warnings: list[str],
    ) -> list[_Pool]:
        """The pools a reading may be mapped to; a mismatch lands in ``refused``.

        A mismatch is reported whatever the read's outcome. ``error`` set means the
        caller reports the failure itself, so an unconfirmed pool is not reported twice.
        """
        confirmed, mismatched, unconfirmed = _identity_split(pools, served)
        for account_id, message in mismatched.items():
            refused[account_id] = message
            warnings.append(
                f"{account_id}: {message}; its /usage numbers are dropped and the pool is "
                f"unavailable until that login is fixed"
            )
        if error is None:
            for account_id, message in unconfirmed.items():
                warnings.append(f"{account_id}: no /usage reading: {message}")
        return confirmed

    def _readings(
        self, now_s: float, warnings: list[str]
    ) -> tuple[dict[str, _Reading], dict[str, str]]:
        """``(readings by account id, {account id: identity mismatch})``."""
        groups = self._groups(warnings)
        offline = (self._environ().get(USAGE_OFFLINE_ENV) or "").strip().lower() in _TRUTHY
        live = self._live and not offline

        cached = {pool.id: self._cached(pool) for pools in groups for pool in pools}

        def fresh(pool: _Pool) -> bool:
            record = cached[pool.id]
            return (
                record.response is not None
                and record.fetched_at_s is not None
                and now_s - record.fetched_at_s < self._ttl_s
            )

        to_read = [pools for pools in groups if live and not all(fresh(p) for p in pools)]
        results: list[_Live] = []
        if to_read:
            with ThreadPoolExecutor(
                max_workers=len(to_read), thread_name_prefix="agy-usage"
            ) as executor:
                results = list(executor.map(self._read_live, to_read))

        readings: dict[str, _Reading] = {}
        refused: dict[str, str] = {}
        for pools, read in zip(to_read, results):
            self._write(pools, now_s, read)
            confirmed = self._confirm(pools, read.served, read.error, refused, warnings)
            if read.error is not None:
                failed = [pool.id for pool in pools if pool.id not in refused]
                if failed:
                    warnings.append(f"{', '.join(failed)}: no /usage reading: {read.error}")
                continue
            self._map(read.response, confirmed, now_s, SOURCE_LIVE, read.served, readings,
                      warnings)

        read_ids = {pool.id for pools in to_read for pool in pools}
        for pools in groups:
            servable: dict[float, list[_Pool]] = {}
            for pool in pools:
                if pool.id in read_ids:
                    continue
                record = cached[pool.id]
                if record.problem is not None:
                    hint = "" if live else "; `quotapick status` reads one (the poller runs it every 120s)"
                    warnings.append(f"{pool.id}: {record.problem}{hint}")
                    continue
                assert record.fetched_at_s is not None
                age_s = now_s - record.fetched_at_s
                confirmed = self._confirm([pool], record.served, record.error, refused, warnings)
                if record.error is not None:
                    if pool.id not in refused:
                        warnings.append(
                            f"{pool.id}: the last /usage read, {age_s:.0f}s ago, failed: "
                            f"{record.error}"
                        )
                    continue
                if not confirmed:
                    continue
                if age_s >= self._stale_max_s:
                    warnings.append(
                        f"{pool.id}: the cached /usage reading is {age_s:.0f}s old, past the "
                        f"{self._stale_max_s:.0f}s bound; not served"
                    )
                    continue
                servable.setdefault(record.fetched_at_s, []).append(pool)
            for fetched_at_s, same in servable.items():
                record = cached[same[0].id]
                self._map(record.response, same, fetched_at_s, SOURCE_CACHE, record.served,
                          readings, warnings)
        return readings, refused

    @staticmethod
    def _map(
        response: Any,
        pools: Sequence[_Pool],
        observed_at_s: float,
        source: str,
        served: Sequence[str],
        readings: dict[str, _Reading],
        warnings: list[str],
    ) -> None:
        """Map a reading onto pools whose declared account its log already confirmed."""
        if not pools:
            return
        try:
            mapped, notes = map_usage(response, [(p.id, p.flavour) for p in pools], observed_at_s)
        except UsageRejected as exc:
            warnings.append(f"{', '.join(p.id for p in pools)}: no /usage reading: {exc}")
            return
        warnings.extend(notes)
        for account_id, windows in mapped.items():
            readings[account_id] = _Reading(windows, source, observed_at_s, served[0])

    # -- protocol -------------------------------------------------------------------------

    def snapshot(self, now_s: float) -> list[AccountSnapshot]:
        """One snapshot per pool, measured where ``/usage`` could be read. Never raises."""
        warnings: list[str] = []
        deadlines = self._deadlines()
        installed = self._resolve(self._binary) is not None
        if not installed:
            warnings.append(f"antigravity: {self._binary} not on PATH; pools marked unavailable")

        try:
            readings, refused = self._readings(now_s, warnings) if self._accounts else ({}, {})
        except Exception as exc:  # noqa: BLE001 - an adapter never raises
            warnings.append(f"antigravity: /usage read failed: {type(exc).__name__}: {exc}")
            readings, refused = {}, {}
        policy = {str(account.id): account for account in self._accounts or ()}

        snapshots: list[AccountSnapshot] = []
        for account_id in self._account_ids:
            deadline = deadlines.get(account_id)
            exhausted = deadline is not None and deadline > now_s
            reading = readings.get(account_id)
            account = policy.get(account_id)

            parts: list[str] = []
            if reading is not None:
                age_s = now_s - reading.observed_at_s
                parts.append(
                    "agy /usage, live"
                    if reading.source == SOURCE_LIVE
                    else f"agy /usage, cached reading {age_s:.0f}s old"
                )
                parts.append(f"signed in as {reading.identity} (agy's own log), as declared")
            elif account_id in refused:
                parts.append(f"{refused[account_id]}; numbers dropped")
            elif account is None:
                parts.append("no launch plan; usage not read")
            else:
                parts.append("no /usage reading; unmeasured")
            if not installed:
                parts.append(f"{self._binary} not installed")
            if exhausted:
                parts.append(f"failure-learned exhaustion until {_format_deadline(float(deadline))}")
            elif deadline is not None:
                parts.append(
                    f"last learned exhaustion expired at {_format_deadline(float(deadline))}"
                )

            tier = normalize_tier(getattr(account, "tier", None)) if account is not None else TIER_UNKNOWN
            try:
                snapshots.append(
                    AccountSnapshot(
                        id=account_id,
                        windows=reading.windows if reading is not None else (),
                        tier=tier,
                        source=reading.source if reading is not None else SOURCE_ASSUMED,
                        confidence=(
                            MEASURED_CONFIDENCE if reading is not None else UNOBSERVABLE_CONFIDENCE
                        ),
                        available=installed and not exhausted and account_id not in refused,
                        note="; ".join(parts),
                        identity=Identity(email=reading.identity) if reading is not None else None,
                    )
                )
            except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
                warnings.append(f"antigravity: {account_id} rejected ({exc})")

        self.warnings = tuple(dict.fromkeys(warnings))
        return snapshots
