"""Codex source, live: ``codex app-server`` answering ``account/rateLimits/read``.

Session transcripts exist only for sessions that wrote one. ``codex exec --ephemeral``
writes none, so a caller that runs that way spends quota the transcript adapter never
sees: one account's transcript reading was fifteen hours old and said 0% while the
account had spent 1%. The app server asks the backend, so this is the live reading and
:class:`~quota_router.providers.codex_sessions.CodexSessionsAdapter` is the fallback.

The exchange, over the child's stdin and stdout, one JSON object per line::

    -> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{...}}}
    <- {"id":1,"result":{...}}
    -> {"jsonrpc":"2.0","method":"initialized","params":{}}
    -> {"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read","params":{}}
    <- {"id":2,"result":{"rateLimits":{...},"rateLimitsByLimitId":{...},"accountId":...}}

The server may send notifications first. One of them carries the machine's host name,
so nothing but the one response is kept. ``rateLimits/read`` without the ``account/``
prefix is rejected as an unknown variant.

Rules this module keeps:

* **No credential is touched.** The adapter spawns the vendor binary and reads its
  answer. The binary manages its own login. ``auth.json`` is read only for the email
  claim, through the same helper the transcript adapter uses.
* **Fail loud.** An error response, a malformed payload, a missing binary, an early
  exit or a timeout yields no live snapshot for that account and a warning that names
  the account and the cause. The transcript reading then serves. A window whose length
  or reset is missing makes the whole read unusable: guessing either would be inventing
  data, and dropping only that window would make the account look healthier.
* **Nothing is left running.** The child starts in its own session, and the whole
  process group is signalled at the end, because the Homebrew ``codex`` is a node shim
  that runs the native server as its own child and cannot forward SIGKILL.
* **The binary path is explicit.** launchd's PATH has neither Homebrew nor
  ``~/.local/bin``, and the usage poller runs under launchd, so PATH is never searched.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final

from ..types import (
    ACCOUNT_CODEX,
    SOURCE_LIVE,
    TIER_UNKNOWN,
    AccountSnapshot,
    Identity,
    Window,
    normalize_tier,
)
from .codex_sessions import (
    CODEX_HOME_ENV,
    DEFAULT_CODEX_HOME,
    DEFAULT_LIMIT_IDS,
    CodexAccountConfig,
    _identity_from_auth,
    _LimitObservation,
    _windows_from_observation,
)

__all__ = [
    "CODEX_BIN_ENV",
    "DEFAULT_CODEX_BIN",
    "DEFAULT_TIMEOUT_S",
    "RATE_LIMITS_METHOD",
    "CodexAccountConfig",
    "CodexAppServerAdapter",
]

#: Overrides :data:`DEFAULT_CODEX_BIN` for every read this adapter makes.
CODEX_BIN_ENV: Final[str] = "QUOTA_ROUTER_CODEX_BIN"
#: Where Homebrew installs the ``codex`` shim. The same default ``cdx`` uses.
DEFAULT_CODEX_BIN: Final[str] = "/opt/homebrew/bin/codex"

#: Wall-clock budget for one account's whole exchange, process start included. A read
#: measured 0.4-0.55s. Accounts are read at the same time, so the fleet costs the
#: slowest one, and this stays under the 3s pick deadline of ``cl`` and ``cdx`` with
#: room for the rest of the pick. A slower read falls back to the transcripts.
DEFAULT_TIMEOUT_S: Final[float] = 2.0
#: How long a signalled process group gets to exit before SIGKILL.
_TERMINATE_GRACE_S: Final[float] = 0.3

RATE_LIMITS_METHOD: Final[str] = "account/rateLimits/read"
_INITIALIZE_ID: Final[int] = 1
_READ_ID: Final[int] = 2

#: Same flag the Claude usage reader honours: serve what is cached, spawn nothing.
_OFFLINE_ENV: Final[str] = "QUOTA_ROUTER_USAGE_OFFLINE"
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_STDERR_TAIL_CHARS: Final[int] = 400

#: Signature of the injected process starter: ``(argv, env) -> Popen``.
Spawn = Callable[[Sequence[str], Mapping[str, str]], "subprocess.Popen[bytes]"]


class _ReadFailed(Exception):
    """One account's read failed; the message is the cause, for the warning."""


def default_spawn(argv: Sequence[str], env: Mapping[str, str]) -> "subprocess.Popen[bytes]":
    """Start the app server in its own session, so its process group can be signalled."""
    return subprocess.Popen(  # noqa: S603 - argv is built here, never a shell string
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        start_new_session=True,
        close_fds=True,
    )


def _request(message_id: int | None, method: str, params: Mapping[str, Any]) -> bytes:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": dict(params)}
    if message_id is not None:
        message["id"] = message_id
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def _pump_lines(stream: Any, sink: "queue.Queue[bytes | None]") -> None:
    try:
        for line in iter(stream.readline, b""):
            sink.put(line)
    except (OSError, ValueError):
        pass
    finally:
        sink.put(None)


def _pump_tail(stream: Any, tail: "deque[str]") -> None:
    try:
        for line in iter(stream.readline, b""):
            tail.append(line.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        pass


def _signal_group(process: "subprocess.Popen[bytes]", sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:  # pragma: no cover - defensive
        try:
            process.send_signal(sig)
        except OSError:
            pass


def _stop(process: "subprocess.Popen[bytes]") -> None:
    """End the child and everything it started. Never raises."""
    try:
        if process.stdin is not None:
            process.stdin.close()
    except OSError:
        pass
    # The group is signalled even when the leader has exited: a shim's native child
    # can outlive it.
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL cannot be ignored
        pass
    for stream in (process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _window_entry(limit_id: str, slot: str, raw: Any) -> Mapping[str, Any] | None:
    """Validate one ``RateLimitWindow`` and translate it to the transcript spelling."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise _ReadFailed(f"limit {limit_id!r} {slot} is not an object")
    used = raw.get("usedPercent")
    minutes = raw.get("windowDurationMins")
    resets_at = raw.get("resetsAt")
    if not _is_int(used):
        raise _ReadFailed(f"limit {limit_id!r} {slot}.usedPercent is not an integer: {used!r}")
    if not _is_int(minutes) or minutes <= 0:
        raise _ReadFailed(
            f"limit {limit_id!r} {slot}.windowDurationMins is not a positive integer: {minutes!r}"
        )
    if not _is_int(resets_at) or resets_at <= 0:
        raise _ReadFailed(f"limit {limit_id!r} {slot}.resetsAt is not an epoch: {resets_at!r}")
    return {"used_percent": used, "window_minutes": minutes, "resets_at": resets_at}


def parse_rate_limits(
    result: Any, *, observed_at_s: float
) -> tuple[list[Window], str | None, str | None, list[str]]:
    """``(windows, plan_type, account_id, notes)`` from a ``GetAccountRateLimitsResponse``.

    Raises :class:`_ReadFailed` for anything that is not the documented shape. Window
    keys come from the transcript adapter's own builder, so the two sources name every
    window identically.
    """
    if not isinstance(result, Mapping):
        raise _ReadFailed("malformed response: result is not an object")
    single = result.get("rateLimits")
    if not isinstance(single, Mapping):
        raise _ReadFailed("malformed response: no rateLimits object")
    by_id = result.get("rateLimitsByLimitId")
    if by_id is None:
        limit_id = single.get("limitId")
        buckets: Mapping[str, Any] = {limit_id if isinstance(limit_id, str) else "": single}
    elif isinstance(by_id, Mapping):
        buckets = by_id
    else:
        raise _ReadFailed("malformed response: rateLimitsByLimitId is not an object")

    observations: list[_LimitObservation] = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, Mapping):
            raise _ReadFailed(f"malformed response: limit {key!r} is not an object")
        raw_id = bucket.get("limitId")
        limit_id = raw_id if isinstance(raw_id, str) else str(key)
        name = bucket.get("limitName")
        plan = bucket.get("planType")
        observations.append(
            _LimitObservation(
                limit_id=limit_id,
                limit_name=name if isinstance(name, str) else None,
                plan_type=plan if isinstance(plan, str) else None,
                observed_at_s=observed_at_s,
                primary=_window_entry(limit_id, "primary", bucket.get("primary")),
                secondary=_window_entry(limit_id, "secondary", bucket.get("secondary")),
            )
        )

    # Account-wide limits first, so they keep the plain "7d" key, as in the transcripts.
    observations.sort(
        key=lambda obs: (obs.limit_id.strip().casefold() not in DEFAULT_LIMIT_IDS, obs.limit_id)
    )
    windows: list[Window] = []
    notes: list[str] = []
    taken: set[str] = set()
    for observation in observations:
        if observation.primary is None and observation.secondary is None:
            notes.append(f"limit {observation.limit_id!r} reported no window")
            continue
        built = _windows_from_observation(observation, taken=taken)
        if not built:
            raise _ReadFailed(f"limit {observation.limit_id!r} has a window that did not validate")
        windows.extend(built)
    if not windows:
        raise _ReadFailed("no window in any limit")

    plan_type = next((obs.plan_type for obs in observations if obs.plan_type), None)
    account = result.get("accountId")
    return windows, plan_type, account if isinstance(account, str) and account else None, notes


class CodexAppServerAdapter:
    """Read each Codex account's live usage through its own ``codex app-server``.

    Args:
        codex_accounts: every Codex account to read, each with its own home. When
            absent the adapter reads one account from ``$CODEX_HOME`` or ``~/.codex``.
        codex_bin: the ``codex`` binary. Defaults to :data:`CODEX_BIN_ENV`, then
            :data:`DEFAULT_CODEX_BIN`. PATH is never searched.
        timeout_s: budget for one account's whole exchange; accounts run at once.
        read_identity: read ``auth.json`` for the account email (local file only).
        env: environment for path lookups and the child, injected for tests.
        spawn: process starter, injected for tests.
    """

    name = "codex_app_server"
    #: The cap a caller's timeout may shorten but never stretch; see ``cli._configure_adapters``.
    default_timeout_s: float = DEFAULT_TIMEOUT_S

    def __init__(
        self,
        *,
        codex_accounts: Sequence[CodexAccountConfig] | None = None,
        codex_home: Path | str | None = None,
        account_id: str = ACCOUNT_CODEX,
        codex_bin: str | os.PathLike[str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        read_identity: bool = True,
        env: Mapping[str, str] | None = None,
        spawn: Spawn | None = None,
    ) -> None:
        self._codex_accounts = tuple(codex_accounts) if codex_accounts else None
        self._codex_home = codex_home
        self._account_id = account_id
        self._codex_bin = os.fspath(codex_bin) if codex_bin is not None else None
        self.timeout_s = float(timeout_s)
        self._read_identity = read_identity
        self._env = env
        self._spawn = spawn or default_spawn
        self.warnings: tuple[str, ...] = ()

    def _environ(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def codex_bin(self) -> str:
        """The binary to spawn: argument, then :data:`CODEX_BIN_ENV`, then the default."""
        raw = self._codex_bin or self._environ().get(CODEX_BIN_ENV) or DEFAULT_CODEX_BIN
        return os.path.expanduser(raw)

    def accounts(self) -> tuple[CodexAccountConfig, ...]:
        """Every account this adapter reads, in policy order."""
        if self._codex_accounts is not None:
            return self._codex_accounts
        if self._codex_home is not None:
            home = Path(os.path.expanduser(str(self._codex_home)))
        else:
            home = Path(os.path.expanduser(self._environ().get(CODEX_HOME_ENV) or DEFAULT_CODEX_HOME))
        return (CodexAccountConfig(account_id=self._account_id, codex_home=home),)

    def snapshot(self, now_s: float) -> list[AccountSnapshot]:
        """One live snapshot per account that answered. Never raises."""
        self.warnings = ()
        if (self._environ().get(_OFFLINE_ENV) or "").strip().lower() in _TRUTHY:
            return []
        accounts = self.accounts()
        if not accounts:
            return []
        binary = self.codex_bin()
        if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
            self.warnings = tuple(
                f"{account.account_id}: no live Codex usage read: codex binary not found or "
                f"not executable at {binary} (set {CODEX_BIN_ENV})"
                for account in accounts
            )
            return []

        with ThreadPoolExecutor(
            max_workers=len(accounts), thread_name_prefix="codex-app-server"
        ) as pool:
            results = list(pool.map(lambda account: self._snapshot_one(account, binary, now_s), accounts))

        warnings: list[str] = []
        out: list[AccountSnapshot] = []
        for snapshot, notes in results:
            warnings.extend(notes)
            if snapshot is not None:
                out.append(snapshot)
        self.warnings = tuple(warnings)
        return out

    def _snapshot_one(
        self, account: CodexAccountConfig, binary: str, now_s: float
    ) -> tuple[AccountSnapshot | None, list[str]]:
        label = account.account_id
        try:
            result = self._exchange(account, binary)
            windows, plan_type, remote_account, notes = parse_rate_limits(result, observed_at_s=now_s)
        except _ReadFailed as exc:
            return None, [f"{label}: no live Codex usage read: {exc}"]
        except Exception as exc:  # noqa: BLE001 - an adapter never raises
            return None, [f"{label}: no live Codex usage read: {type(exc).__name__}: {exc}"]

        warnings = [f"{label}: {note}" for note in notes]
        identity: Identity | None = None
        plan_from_auth: str | None = None
        if self._read_identity:
            identity, plan_from_auth = _identity_from_auth(account.codex_home / "auth.json")
        if identity is not None and remote_account:
            if identity.organization_uuid and identity.organization_uuid != remote_account:
                warnings.append(
                    f"{label}: app server reports a different account id than auth.json; "
                    f"keyed on the app server's"
                )
            try:
                identity = Identity(email=identity.email, organization_uuid=remote_account)
            except (TypeError, ValueError):  # pragma: no cover - defensive
                pass

        plan = plan_type or plan_from_auth
        tier = normalize_tier(plan)
        if tier == TIER_UNKNOWN:
            warnings.append(f"{label}: unrecognized plan_type {plan!r}; capacity stays neutral")
        try:
            snapshot = AccountSnapshot(
                id=account.account_id,
                windows=tuple(windows),
                tier=tier,
                source=SOURCE_LIVE,
                confidence=1.0,
                available=True,
                note=f"codex app-server {RATE_LIMITS_METHOD} for {account.codex_home}",
                identity=identity,
            )
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            return None, [*warnings, f"{label}: snapshot rejected ({type(exc).__name__}: {exc})"]
        return snapshot, warnings

    def _child_env(self, account: CodexAccountConfig, binary: str) -> dict[str, str]:
        env = dict(self._environ())
        env[CODEX_HOME_ENV] = str(account.codex_home)
        # The Homebrew binary is a `#!/usr/bin/env node` shim, and node sits beside it.
        # Under launchd's PATH the shim exits 127 without this.
        directory = os.path.dirname(binary)
        rest = [part for part in (env.get("PATH") or "").split(os.pathsep) if part and part != directory]
        env["PATH"] = os.pathsep.join([directory, *rest])
        return env

    def _exchange(self, account: CodexAccountConfig, binary: str) -> Any:
        """Run the three-message exchange and return the read's ``result``."""
        deadline = time.monotonic() + self.timeout_s
        try:
            process = self._spawn([binary, "app-server"], self._child_env(account, binary))
        except OSError as exc:
            raise _ReadFailed(f"could not start {binary}: {exc}") from exc

        lines: "queue.Queue[bytes | None]" = queue.Queue()
        stderr_tail: "deque[str]" = deque(maxlen=20)
        threading.Thread(target=_pump_lines, args=(process.stdout, lines), daemon=True).start()
        threading.Thread(target=_pump_tail, args=(process.stderr, stderr_tail), daemon=True).start()

        def stderr_text() -> str:
            return "".join(stderr_tail).strip()[-_STDERR_TAIL_CHARS:]

        def send(payload: bytes) -> None:
            try:
                assert process.stdin is not None
                process.stdin.write(payload)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise _ReadFailed(f"app-server closed its input: {exc}") from exc

        def await_response(message_id: int, what: str) -> Mapping[str, Any]:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ReadFailed(f"timed out after {self.timeout_s:g}s waiting for {what}")
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty:
                    continue
                if line is None:
                    code = process.poll()
                    if code is None:
                        try:
                            code = process.wait(timeout=_TERMINATE_GRACE_S)
                        except subprocess.TimeoutExpired:
                            code = None
                    detail = stderr_text()
                    raise _ReadFailed(
                        f"app-server exited (code {code}) before answering {what}"
                        + (f": {detail}" if detail else "")
                    )
                try:
                    message = json.loads(line)
                except ValueError as exc:
                    raise _ReadFailed(f"malformed line from app-server while waiting for {what}") from exc
                if not isinstance(message, Mapping):
                    raise _ReadFailed(f"malformed message from app-server while waiting for {what}")
                # Notifications and server-to-client requests carry a method; skip them.
                if "method" in message or message.get("id") != message_id:
                    continue
                error = message.get("error")
                if error is not None:
                    text = error.get("message") if isinstance(error, Mapping) else error
                    code = error.get("code") if isinstance(error, Mapping) else None
                    raise _ReadFailed(f"{what} returned error {code}: {text}")
                if "result" not in message:
                    raise _ReadFailed(f"malformed response to {what}: no result")
                return message

        try:
            send(
                _request(
                    _INITIALIZE_ID,
                    "initialize",
                    {"clientInfo": {"name": "quota-router", "version": "0"}},
                )
            )
            await_response(_INITIALIZE_ID, "initialize")
            send(_request(None, "initialized", {}))
            send(_request(_READ_ID, RATE_LIMITS_METHOD, {}))
            return await_response(_READ_ID, RATE_LIMITS_METHOD)["result"]
        finally:
            _stop(process)
