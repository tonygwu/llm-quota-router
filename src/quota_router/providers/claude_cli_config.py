"""Discover the operator's local Claude config directories, identity, and tier.

Extracted from the retired external-oracle adapter, which owned this logic only
because it happened to be the first consumer. It is oracle-agnostic: it reads
``~/.claude*/.claude.json`` and nothing else, spawns no process, and makes no
network call.

``config_dir`` is what an executor puts in ``CLAUDE_CONFIG_DIR`` when it spawns
the real ``claude`` binary. It is *not* how an account is identified --
:attr:`ClaudeAccountConfig.identity` is. A directory is a mount point, and the
credentials inside one can be swapped for another account's at any time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping

from ..types import (
    ACCOUNT_CLAUDE,
    ACCOUNT_CLAUDE_B,
    ACCOUNT_CLAUDE_C,
    ACCOUNT_CLAUDE_D,
    TIER_UNKNOWN,
    Identity,
    normalize_tier,
)
from .base import read_json_file

__all__ = [
    "DEFAULT_CLAUDE_CONFIG_DIR_NAMES",
    "CLAUDE_CONFIG_DIRS_ENV",
    "ClaudeAccountConfig",
    "discover_claude_configs",
]


DEFAULT_CLAUDE_CONFIG_DIR_NAMES: Final[Mapping[str, str]] = {
    ACCOUNT_CLAUDE: ".claude",
    ACCOUNT_CLAUDE_B: ".claude-b",
    ACCOUNT_CLAUDE_C: ".claude-c",
    ACCOUNT_CLAUDE_D: ".claude-d",
}

#: Optional JSON env override: ``{"claude": "/abs/dir", "claude_b": "/abs/dir"}``.
CLAUDE_CONFIG_DIRS_ENV: Final[str] = "QUOTA_ROUTER_CLAUDE_CONFIG_DIRS"

#: older than a couple of minutes is a cached answer and anything past 15 minutes is old
#: enough that the operator may have burned a whole 5-hour window since.
_FRESH_S: Final[float] = 120.0
_STALE_S: Final[float] = 900.0

#: Multiplier applied when the tier could not be read. Capacity stays neutral (1.0 per
#: the contract); the uncertainty is expressed here instead.
_UNKNOWN_TIER_CONFIDENCE: Final[float] = 0.6

#: Multiplier applied when an account could only be matched by its positional number.
_NUMBER_MATCH_CONFIDENCE: Final[float] = 0.8


# ======================================================================================
# Identity / tier discovery from ~/.claude*/.claude.json
# ======================================================================================


@dataclass(frozen=True, slots=True)
class ClaudeAccountConfig:
    """One local Claude config directory, resolved to an identity and a tier.

    ``config_dir`` is what an executor puts in ``CLAUDE_CONFIG_DIR`` when it spawns the
    real ``claude`` binary for this account. It is *not* how the account is identified --
    :attr:`identity` is.

    ``declared_email`` comes from the operator's own policy file and is consulted
    only when the local ``.claude.json`` could not answer -- an email the operator
    wrote down is still identity matching.
    """

    account_id: str
    config_dir: Path
    identity: Identity | None = None
    tier: str = TIER_UNKNOWN
    source_path: Path | None = None
    declared_email: str | None = None

    @property
    def resolved(self) -> bool:
        """``True`` when we know who this directory is logged in as."""
        return self.identity is not None


def _config_file_candidates(config_dir: Path) -> tuple[Path, ...]:
    """Where a config directory's ``.claude.json`` may live.

    Claude Code has shipped both layouts: the file inside the directory, and the file
    beside it (``~/.claude`` -> ``~/.claude.json``, which is what this machine uses for
    the default account).
    """
    return (config_dir / ".claude.json", config_dir.parent / f"{config_dir.name}.json")


def _identity_and_tier(config_dir: Path) -> tuple[Identity | None, str, Path | None]:
    """Read ``oauthAccount`` out of a config directory. Never raises."""
    for candidate in _config_file_candidates(config_dir):
        payload = read_json_file(candidate)
        if not isinstance(payload, Mapping):
            continue
        account = payload.get("oauthAccount")
        if not isinstance(account, Mapping):
            continue
        email = account.get("emailAddress")
        if not isinstance(email, str) or not email.strip():
            continue
        org_uuid = account.get("organizationUuid")
        org_name = account.get("organizationName")
        tier = normalize_tier(
            account.get("organizationRateLimitTier") or account.get("userRateLimitTier")
        )
        try:
            identity = Identity(
                email=email,
                organization_uuid=org_uuid if isinstance(org_uuid, str) else "",
                organization_name=org_name if isinstance(org_name, str) else None,
            )
        except (TypeError, ValueError):
            continue
        return identity, tier, candidate
    return None, TIER_UNKNOWN, None


def _config_dirs_from_env(env: Mapping[str, str]) -> dict[str, Path] | None:
    """Parse :data:`CLAUDE_CONFIG_DIRS_ENV`, or ``None`` when unset/unusable."""
    raw = env.get(CLAUDE_CONFIG_DIRS_ENV)
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    out: dict[str, Path] = {}
    for account_id, path in parsed.items():
        if isinstance(account_id, str) and isinstance(path, str) and path.strip():
            out[account_id] = Path(os.path.expanduser(path.strip()))
    return out or None


def discover_claude_configs(
    *,
    home: Path | str | None = None,
    config_dirs: Mapping[str, Path | str] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[ClaudeAccountConfig, ...]:
    """Resolve every known Claude config directory to an identity + tier.

    Precedence: explicit ``config_dirs`` argument, then the
    :data:`CLAUDE_CONFIG_DIRS_ENV` JSON override, then the default
    ``~/.claude``, ``~/.claude-b``, ``~/.claude-c`` layout.

    Directories that do not exist, or that exist but hold no readable ``oauthAccount``,
    still come back -- with ``identity=None`` -- so a caller can tell "this account is not
    set up" apart from "this account was never configured".
    """
    environ = env if env is not None else os.environ
    if config_dirs is not None:
        dirs = {str(key): Path(os.path.expanduser(str(value))) for key, value in config_dirs.items()}
    else:
        dirs = _config_dirs_from_env(environ) or {}
        if not dirs:
            base = Path(home) if home is not None else Path(os.path.expanduser("~"))
            dirs = {
                account_id: base / name
                for account_id, name in DEFAULT_CLAUDE_CONFIG_DIR_NAMES.items()
            }

    resolved: list[ClaudeAccountConfig] = []
    for account_id, config_dir in dirs.items():
        identity, tier, source_path = _identity_and_tier(config_dir)
        resolved.append(
            ClaudeAccountConfig(
                account_id=account_id,
                config_dir=config_dir,
                identity=identity,
                tier=tier,
                source_path=source_path,
            )
        )
    return tuple(resolved)


