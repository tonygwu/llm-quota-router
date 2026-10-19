"""Pure demand-group identities shared by measurement and routing.

An Antigravity account needs configuration to distinguish its Gemini and Claude-model
pools. Missing configuration keeps it in the legacy group; account names are not pool
selectors and must never be used to guess one.
"""

from collections.abc import Mapping
from typing import Final


GROUP_FOR_PROVIDER: Final[Mapping[str, str]] = {
    "claude": "claude",
    "codex": "codex",
    "antigravity": "antigravity",
    "anthropic_api": "claude",
}


def group_for(
    account_id: str, provider: str, groups: Mapping[str, str] | None = None
) -> str | None:
    """Resolve an explicit account group, otherwise the provider's legacy group."""
    if groups is not None and account_id in groups:
        return groups[account_id]
    return GROUP_FOR_PROVIDER.get(provider)
