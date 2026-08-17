"""Wake a dark account by asking Claude Code to renew its own credentials.

THE PROBLEM THIS SOLVES
-----------------------
An OAuth *access* token lasts about eight hours. This package will never redeem a
refresh token to renew one -- see :mod:`tests.test_no_token_rotation` and the
single-writer rule in the README for what happened the last time something did. So
when a token lapses, the usage read fails, the snapshot carries no windows, and the
router cannot route to that account.

That is a starvation loop, and it inverts the objective of the whole tool. The account
with the most quota left is by definition the one being used least, so it is the FIRST
to go dark -- and once dark, the router will not spend from it, which is what keeps it
dark. Observed live: an account holding 53% of its Fable allowance was invisible while
the router picked one with 3% left, and the cycle only broke because the operator
happened to start a session on it by hand.

The fix does not weaken the single-writer rule; it leans on it. Claude Code is the one
process permitted to redeem a refresh token, so this asks Claude Code to do exactly
that, by spawning it against the account's own config directory for one trivial prompt.
Nothing here touches a credential. The renewal happens inside the vendor's own binary,
where it belongs, and the next usage read simply succeeds.

WHY THIS IS REACTIVE AND NOT A CRON
-----------------------------------
The obvious design -- a scheduled keepalive -- does not work, and two measurements say
so rather than intuition:

* ``claude auth status`` does not refresh anything. It completes in 0.19s, makes no
  network call, and leaves the stored credential byte-identical.
* ``claude -p`` against a *healthy* token does not refresh either: 3.5s, a real
  authenticated API call, credential still byte-identical afterwards.

Renewal happens only when the token has actually lapsed AND the account is used. So a
prompt every six hours against an eight-hour token is a no-op that buys nothing and
costs an API call each time; the token still expires on its original schedule. The only
moment the spend is worth anything is the moment a read fails, which is precisely when
this is called.

WHERE IT MAY BE CALLED FROM
---------------------------
Background paths only. A spawn costs seconds, and the interactive launcher caps its
entire routing decision at three -- so refreshing inline there would trade the bug for
a stall on every new terminal. The poller has no one waiting on it and is the right
caller.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from .config import BANNED_EXEC_ENV, AccountConfig
from .providers.base import Runner, default_runner

__all__ = [
    "DEFAULT_REFRESH_MODEL",
    "DEFAULT_REFRESH_PROMPT",
    "DEFAULT_REFRESH_TIMEOUT_S",
    "DEFAULT_REFRESH_COOLDOWN_S",
    "RefreshOutcome",
    "refresh_auth",
]

#: The cheapest model there is. The point is to make an *authenticated* call, not to
#: get an answer, so anything more capable is spending quota to accomplish nothing.
DEFAULT_REFRESH_MODEL: Final[str] = "haiku"

#: Deliberately trivial. A shorter prompt is a smaller bill and there is no output to
#: read: the return value of this call is a renewed token, not a completion.
DEFAULT_REFRESH_PROMPT: Final[str] = "1 + 1"

#: A refresh does a token exchange plus one small round trip. Generous enough for a
#: cold start on a slow link, bounded so a hung binary cannot wedge the poller.
DEFAULT_REFRESH_TIMEOUT_S: Final[float] = 60.0

#: How long to wait before trying the same account again.
#:
#: This is the guard that keeps the remedy cheaper than the disease. Some accounts are
#: dark for reasons a refresh cannot fix -- revoked credentials, a slot that was logged
#: out -- and without a cooldown those would spawn a CLI on every single invocation,
#: forever. Half an hour is well inside the ~8h token lifetime, so a genuinely fixable
#: account still recovers within one poll or two, while an unfixable one costs at most
#: two spawns an hour.
DEFAULT_REFRESH_COOLDOWN_S: Final[float] = 1800.0

#: Same default as the launcher, and never resolved through ``PATH``: this module may
#: run from launchd, whose minimal ``PATH`` excludes the usual install directory.
DEFAULT_CLAUDE_BIN: Final[str] = "~/.local/bin/claude"


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What happened, in enough detail to explain a routing decision afterwards."""

    account_id: str
    #: False when the cooldown blocked it, or there was nothing to spawn against.
    attempted: bool
    ok: bool
    reason: str


def _read_stamp(path: Path) -> float | None:
    """Last attempt time, or ``None`` if there isn't a readable one.

    A corrupt or missing stamp means "no evidence of a recent attempt", never an
    error: this is bookkeeping for a repair, and it must not be able to veto one.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("last_attempt_s") if isinstance(data, Mapping) else None
    return float(value) if isinstance(value, (int, float)) else None


def _write_stamp(path: Path, now_s: float, *, make_parents: bool) -> None:
    """Record the attempt. Best-effort, and silent on failure by design."""
    try:
        if make_parents:
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_attempt_s": now_s}), encoding="utf-8")
    except OSError:
        # An unwritable state directory is not a reason to refuse the refresh. The
        # cost of failing to record an attempt is one extra spawn; the cost of
        # refusing the refresh is an account that stays dark until a human notices.
        pass


def refresh_auth(
    account: AccountConfig,
    *,
    stamp_path: Path,
    now_s: float,
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
    model: str = DEFAULT_REFRESH_MODEL,
    prompt: str = DEFAULT_REFRESH_PROMPT,
    timeout_s: float = DEFAULT_REFRESH_TIMEOUT_S,
    cooldown_s: float = DEFAULT_REFRESH_COOLDOWN_S,
    binary: str | None = None,
    make_parents: bool = True,
) -> RefreshOutcome:
    """Spawn Claude Code once against ``account`` so that IT renews the token.

    Never raises. Every failure comes back as a :class:`RefreshOutcome` with
    ``ok=False``, because this is a repair attempted *during* a routing decision and
    must never become the reason one cannot be made.

    Args:
        account: Supplies the config directory and, through
            :meth:`AccountConfig.exec_env`, the rule that the default account is
            selected by the ABSENCE of the config-dir variable -- naming its directory
            makes Claude Code scaffold a phantom account instead of using the real one.
        stamp_path: Where the cooldown is recorded, one file per account.
        now_s: Injected, never read from a clock here.
    """
    if not account.config_dir:
        return RefreshOutcome(
            account.id, False, False, "no config directory to spawn against"
        )

    last = _read_stamp(stamp_path)
    if last is not None and now_s - last < cooldown_s:
        waited = now_s - last
        return RefreshOutcome(
            account.id,
            False,
            False,
            f"cooldown: last attempt {waited:.0f}s ago, need {cooldown_s:.0f}s",
        )

    # Recorded BEFORE the spawn, not after. A crash, a timeout, or a hard kill between
    # here and the result must still count as an attempt -- otherwise the one case that
    # most needs rate limiting, a spawn that never returns cleanly, is the one case
    # that gets retried immediately every time.
    _write_stamp(stamp_path, now_s, make_parents=make_parents)

    base = dict(os.environ if env is None else env)
    for banned in BANNED_EXEC_ENV:
        base.pop(banned, None)
    # Delete rather than omit: this may run from a shell already pointed at another
    # account, where merely not setting the variable would leave it inherited and the
    # refresh would renew the WRONG account's token.
    base.pop("CLAUDE_CONFIG_DIR", None)
    base.update(account.exec_env())

    argv = [
        os.path.expanduser(binary or DEFAULT_CLAUDE_BIN),
        "--model",
        model,
        "-p",
        prompt,
    ]

    run: Any = runner or default_runner
    try:
        completed = run(argv, env=base, timeout=timeout_s, capture_output=True, text=True)
    except Exception as exc:  # noqa: BLE001 -- a repair may not break the caller
        return RefreshOutcome(
            account.id, True, False, f"{type(exc).__name__}: {exc}"
        )

    code = getattr(completed, "returncode", 1)
    if code == 0:
        return RefreshOutcome(account.id, True, True, "spawned the CLI; token renewed")
    detail = (getattr(completed, "stderr", "") or "").strip().splitlines()
    return RefreshOutcome(
        account.id,
        True,
        False,
        f"exit {code}" + (f": {detail[-1][:120]}" if detail else ""),
    )
