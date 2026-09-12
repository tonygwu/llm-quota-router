"""The Python API: one call that returns a routing decision.

This is a thin wrapper, deliberately. It drives the same ``_prepare`` /
``_pick_payload`` path the ``quotapick`` CLI runs, so the library and the command
line cannot disagree without both changing. A second implementation of the
decision logic would be a second thing to keep correct, and the two would drift
the first time either was touched.

    from quota_router import select_account

    decision = select_account(model="fable", only=["claude", "claude_b"])
    env = {**os.environ, **decision.exec_env}
    for key in decision.exec_unset_env:
        env.pop(key, None)
    subprocess.run([*decision.exec_argv_prefix, "claude", "-p", prompt], env=env)

**The recipe has three parts, and two of them are usually empty.** Applying only
``exec_env`` is correct for every Claude and Codex account and silently wrong for an
Antigravity account declaring ``macos_user``: that account is selected by WHO runs
the binary, so dropping ``exec_argv_prefix`` runs the caller's own account instead,
and dropping ``exec_unset_env`` leaves an inherited ``HOME`` pointing at the wrong
profile directory. Both failures return a perfectly good answer from the wrong place.

**For an account you have already chosen, use ``quotapick launch-plan <id>``.** It
answers from config alone, with no routing and no reservation. The Antigravity pools
publish no quota at all, so ``pick`` cannot rank them and will decline them.

**Mapping the provider to a binary is the caller's job.** ``exec_env`` is an
environment overlay, not a command. The router says *which account*; it has no
idea whether you meant ``claude -p`` or ``codex exec``, and it will not translate
one into the other.

**``exec_env`` is frequently empty, and that is correct.** The default Claude
account is selected by the *absence* of ``CLAUDE_CONFIG_DIR`` -- its config lives
at ``~/.claude.json``, outside ``~/.claude``, so setting the variable makes the
CLI scaffold a fresh empty account. Merge the overlay; never assume it is
non-empty.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

__all__ = ["Selection", "select_account"]


@dataclass(frozen=True, slots=True)
class Selection:
    """One routing decision. :meth:`to_dict` is the ``pick --json`` payload verbatim."""

    account: str | None
    provider: str | None
    exec_env: dict[str, str]
    #: Argv that must come BEFORE the binary, empty for every account reached by an
    #: environment variable. Non-empty only for an account selected by a macOS user
    #: (Antigravity), where WHO runs the binary is the whole selection mechanism.
    #: A caller that merges `exec_env` and ignores this spawns the WRONG account.
    exec_argv_prefix: tuple[str, ...] = ()
    #: Variables to DELETE from the child environment. An overlay cannot express a
    #: deletion, so this cannot be folded into `exec_env`.
    exec_unset_env: tuple[str, ...] = ()
    ranked: tuple[dict[str, Any], ...] = ()
    excluded: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    degraded: tuple[Any, ...] = ()
    fits: bool | None = None
    meets_policy: bool | None = None
    available_at: float | None = None
    reason: str | None = None
    contract_version: int = 1
    _payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """The full CLI payload, for callers that want everything."""
        return dict(self._payload)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Selection":
        decision = payload.get("decision") or {}
        return cls(
            account=decision.get("account"),
            provider=decision.get("provider"),
            exec_env=dict((payload.get("exec") or {}).get("env") or {}),
            exec_argv_prefix=tuple((payload.get("exec") or {}).get("argv_prefix") or ()),
            exec_unset_env=tuple((payload.get("exec") or {}).get("unset_env") or ()),
            ranked=tuple(payload.get("ranked") or ()),
            excluded=tuple(payload.get("excluded") or ()),
            warnings=tuple(payload.get("warnings") or ()),
            degraded=tuple(payload.get("degraded") or ()),
            fits=decision.get("fits"),
            meets_policy=decision.get("meets_policy"),
            available_at=decision.get("available_at"),
            reason=decision.get("reason"),
            contract_version=int(payload.get("contract_version") or 1),
            _payload=dict(payload),
        )


def select_account(
    *,
    model: str | None = None,
    only: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    min_remaining: float | None = None,
    no_sticky: bool = False,
    timeout_ms: int | None = None,
    config: str | None = None,
    record: bool = True,
    env: Mapping[str, str] | None = None,
    now_s: float | None = None,
    cwd: str | None = None,
    deps: Any = None,
) -> Selection:
    """Choose which account to spend on this invocation.

    Args:
        model: Model id or class (``"claude-fable-5[1m]"``, ``"fable"``). Gates which
            of an account's own quota windows are counted -- it does **not** restrict
            which providers are eligible. Use ``only`` for that.
        only / exclude: Restrict the candidate set by account id.
        min_remaining: Refuse accounts below this remaining fraction.
        no_sticky: Ignore the sticky incumbent and hysteresis for this call.
        record: Persist the pick as an in-flight reservation so concurrent callers
            do not all pile onto the same account. Leave it ``True`` when you are
            about to run the work; pass ``False`` when you are only inspecting,
            because a decision that is never acted on should not book anyone's quota.
        env / now_s / cwd / deps: Injection points. Tests use them; callers rarely do.

    Returns:
        A :class:`Selection`. **This function does not raise on routing failure** --
        an unreachable oracle, an exhausted fleet, and a malformed config all come
        back as a degraded ``Selection`` with warnings. A quota router that cannot
        answer must never be the reason a caller cannot work.
    """
    from . import cli as _cli

    args = SimpleNamespace(
        model=model,
        # argparse declares --only/--exclude with action="append", so _split_list
        # expects an ITERABLE OF STRINGS. Handing it one joined string makes it
        # iterate the characters -- "claude" becomes ("c","l","a","u","d","e"),
        # which matches no account and silently excludes the entire fleet.
        only=list(only) if only else None,
        exclude=list(exclude) if exclude else None,
        min_remaining=min_remaining,
        no_sticky=no_sticky,
        timeout_ms=timeout_ms,
        config=config,
        dry_run=not record,
        json=True,
        explain=False,
        now=None,
    )

    # Deps.resolve() is what fills in the lazily-imported scoring and provider
    # layers. main() calls it; a wrapper that skips it gets a Deps with rank/select
    # still None and silently degrades to earliest-reset ordering -- a plausible
    # answer produced by none of the algorithm.
    prepared = _cli._prepare(
        args,
        env if env is not None else {},
        float(now_s) if now_s is not None else time.time(),
        _cli.Deps.resolve(deps),
        cwd,
        event="pick",
        write_state=record,
    )
    return Selection.from_payload(_cli._pick_payload(prepared))
