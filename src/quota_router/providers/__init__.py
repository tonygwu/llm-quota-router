"""Provider adapters: everything that turns the outside world into snapshots.

Each adapter implements :class:`~quota_router.providers.base.ProviderAdapter` -- one
method, ``snapshot(now_s)``, returning :class:`~quota_router.types.AccountSnapshot`
objects -- and each obeys the package rules: never raise, never spawn implicitly (the
runner is injectable), never invent data.

+-------------------------------+---------------------------------------------------+
| Adapter                       | Source of truth                                   |
+===============================+===================================================+
| :class:`ClaudeOAuthAdapter`   | vendor usage endpoint (primary, live)             |
| :class:`ClaudeStatuslineAdapter` | ``~/Library/Caches/.../claude*-rate-limits.json`` |
| :class:`CodexSessionsAdapter` | ``$CODEX_HOME/sessions/**/*.jsonl`` tails         |
| :class:`AntigravityAdapter`   | nothing observable; failure-learned deadline only |
+-------------------------------+---------------------------------------------------+

:func:`collect_snapshots` runs a set of adapters and reconciles the duplicates the two
Claude sources produce; see its docstring for the precedence rule.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from ..types import (
    PROVIDER_CLAUDE,
    SOURCE_ASSUMED,
    SOURCE_CACHE,
    SOURCE_CLAUDE_JSON,
    SOURCE_LIVE,
    SOURCE_MANUAL,
    TIER_UNKNOWN,
    AccountSnapshot,
    normalize_tier,
    provider_for_account_id,
)
from .antigravity import AntigravityAdapter
from .base import (
    CommandOutcome,
    ProviderAdapter,
    Runner,
    default_runner,
    run_command,
)
from .claude_cli_config import (
    CLAUDE_CONFIG_DIRS_ENV,
    DEFAULT_CLAUDE_CONFIG_DIR_NAMES,
    ClaudeAccountConfig,
    discover_claude_configs,
)
from .claude_oauth import ClaudeOAuthAdapter
from .claude_statusline import ClaudeStatuslineAdapter
from .codex_sessions import CodexSessionsAdapter

__all__ = [
    "ProviderAdapter",
    "Runner",
    "default_runner",
    "run_command",
    "CommandOutcome",
    "ClaudeOAuthAdapter",
    "ClaudeStatuslineAdapter",
    "ClaudeAccountConfig",
    "discover_claude_configs",
    "CodexSessionsAdapter",
    "AntigravityAdapter",
    "SOURCE_RANK",
    "build_default_adapters",
    "claude_configs_from_policy",
    "collect_snapshots",
    "load_snapshots",
]


#: How much a snapshot's provenance is worth when two adapters describe the same account.
#: A live oracle read outranks a cache even when the cache is fresher, because the oracle
#: carries *more windows* (the per-model ``scoped[]`` entries the statusline file has no
#: idea about) and a missing window silently removes a constraint.
SOURCE_RANK: Final[Mapping[str, int]] = {
    SOURCE_MANUAL: 4,
    SOURCE_LIVE: 3,
    SOURCE_CLAUDE_JSON: 2,
    SOURCE_CACHE: 1,
    SOURCE_ASSUMED: 0,
}


def _default_config_dir(account_id: str, home: Path) -> Path:
    """``claude`` -> ``~/.claude``, ``claude_b`` -> ``~/.claude-b``, and so on."""
    name = DEFAULT_CLAUDE_CONFIG_DIR_NAMES.get(account_id)
    if name is None:
        name = "." + account_id.replace("_", "-")
    return home / name


def claude_configs_from_policy(
    config: Any,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> tuple[ClaudeAccountConfig, ...]:
    """Build the identity/tier map from the operator's policy file plus the local configs.

    The policy layer owns *which* accounts exist, where their config directories are, and
    any manual overrides; the filesystem owns who each directory is logged in as and what
    tier it is on. This merges the two, with the policy winning only where it is an
    explicit override:

    * ``config_dir`` -- policy, falling back to the ``~/.claude*`` convention;
    * identity -- read from ``.claude.json``; ``identity_email`` from policy is kept as a
      fallback for when that file is unreadable (see ``ClaudeAccountConfig``);
    * ``tier`` -- policy wins when it is set to something other than ``unknown``, which is
      exactly the "config is unreadable, I know what I pay for" case it exists for;

    ``config`` is duck-typed (anything exposing ``enabled_accounts()``), so this module
    never has to import the policy layer.
    """
    accounts_fn = getattr(config, "enabled_accounts", None)
    if not callable(accounts_fn):
        return ()
    try:
        accounts = list(accounts_fn())
    except Exception:  # pragma: no cover - a broken policy object is not our failure mode
        return ()

    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    dirs: dict[str, Path] = {}
    policy: dict[str, Any] = {}
    for account in accounts:
        account_id = getattr(account, "id", None)
        if not isinstance(account_id, str) or not account_id:
            continue
        provider = getattr(account, "provider", "") or provider_for_account_id(account_id)
        if provider != PROVIDER_CLAUDE:
            continue
        raw_dir = getattr(account, "config_dir", None)
        dirs[account_id] = (
            Path(os.path.expanduser(str(raw_dir)))
            if raw_dir
            else _default_config_dir(account_id, base)
        )
        policy[account_id] = account

    if not dirs:
        return ()

    resolved: list[ClaudeAccountConfig] = []
    for discovered in discover_claude_configs(config_dirs=dirs, env=env):
        account = policy.get(discovered.account_id)
        declared_tier = normalize_tier(getattr(account, "tier", None))
        email = getattr(account, "identity_email", None)
        resolved.append(
            ClaudeAccountConfig(
                account_id=discovered.account_id,
                config_dir=discovered.config_dir,
                identity=discovered.identity,
                tier=declared_tier if declared_tier != TIER_UNKNOWN else discovered.tier,
                source_path=discovered.source_path,
                declared_email=email if isinstance(email, str) else None,
            )
        )
    return tuple(resolved)


def build_default_adapters(
    *,
    config: Any = None,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    timeout_s: float = 10.0,
    home: Path | str | None = None,
) -> tuple[ProviderAdapter, ...]:
    """The standard adapter set, in preference order (live read first, guesses last)."""
    claude_configs = claude_configs_from_policy(config, env=env, home=home) or None
    return (
        ClaudeOAuthAdapter(
            runner=runner, timeout_s=timeout_s, env=env, home=home, configs=claude_configs
        ),
        ClaudeStatuslineAdapter(env=env, home=home, configs=claude_configs),
        CodexSessionsAdapter(env=env),
        AntigravityAdapter(env=env),
    )


def _rank(snapshot: AccountSnapshot) -> tuple[int, int, float, int]:
    """Merge key for two readings of the same account, highest wins.

    ``has_windows`` outranks ``source`` deliberately. A snapshot with no windows
    carries no routing information whatever, so preferring it because its *source*
    is nominally better throws away a usable reading for an unusable one -- e.g. an
    account whose access token has expired yields a live-but-empty snapshot that
    would otherwise bury a perfectly good statusline cache.
    """
    return (
        1 if snapshot.windows else 0,
        SOURCE_RANK.get(snapshot.source, 0),
        snapshot.confidence,
        len(snapshot.windows),
    )


def collect_snapshots(
    adapters: Iterable[ProviderAdapter], now_s: float
) -> tuple[list[AccountSnapshot], list[str]]:
    """Run every adapter and merge the results by account id.

    Returns ``(snapshots, warnings)``. Adapters are consulted in order; when two describe
    the same account, the winner is the one with the better ``(source rank, confidence,
    window count)``. An adapter that raises despite the never-raise rule is caught here
    too -- a broken source may not take the router down.
    """
    best: dict[str, AccountSnapshot] = {}
    order: list[str] = []
    warnings: list[str] = []

    for adapter in adapters:
        name = getattr(adapter, "name", type(adapter).__name__)
        try:
            produced: Sequence[AccountSnapshot] = adapter.snapshot(now_s)
        except Exception as exc:  # pragma: no cover - defensive; adapters must not raise
            warnings.append(f"{name}: adapter raised {type(exc).__name__}: {exc}")
            continue
        warnings.extend(getattr(adapter, "warnings", ()))
        for snapshot in produced:
            existing = best.get(snapshot.id)
            if existing is None:
                best[snapshot.id] = snapshot
                order.append(snapshot.id)
            elif _rank(snapshot) > _rank(existing):
                best[snapshot.id] = snapshot

    return [best[account_id] for account_id in order], warnings


def load_snapshots(
    *,
    now_s: float | None = None,
    config: Any = None,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    timeout_s: float = 10.0,
    accounts: Iterable[Any] | None = None,
    home: Path | str | None = None,
) -> tuple[list[AccountSnapshot], list[str]]:
    """Read the world once: the standard adapters, merged, with warnings.

    This is the entry point the CLI binds to. It returns ``(snapshots, warnings)``; the
    caller decides what "degraded" means for its output.

    Args:
        now_s: Epoch seconds to evaluate at. Defaults to the wall clock -- this is the
            one layer allowed to read it, and even here it is overridable.
        config: The operator's policy object (duck-typed), used for config directories,
            tier overrides and the enabled-account list.
        env: Environment mapping for every path/override lookup.
        runner: Injected process runner for the Keychain read.
        timeout_s: Oracle timeout.
        accounts: Restrict the result to these accounts -- either ids or objects with an
            ``id``. Defaults to whatever the adapters found.
        home: ``$HOME`` override for the default config/cache locations.
    """
    moment = now_s if now_s is not None else time.time()
    adapters = build_default_adapters(
        config=config, env=env, runner=runner, timeout_s=timeout_s, home=home
    )
    snapshots, warnings = collect_snapshots(adapters, moment)

    if accounts is not None:
        wanted = {
            item if isinstance(item, str) else getattr(item, "id", None) for item in accounts
        }
        wanted.discard(None)
        if wanted:
            snapshots = [snapshot for snapshot in snapshots if snapshot.id in wanted]

    return snapshots, warnings
