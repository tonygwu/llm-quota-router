"""llm-quota-router -- pick which of the operator's own LLM accounts to spend from.

The operator already pays for several LLM subscriptions and is already authorized on all
of them. Every one has quota that expires on a rolling schedule, unused quota is simply
burned money, and the accounts drain at wildly different rates. This package answers one
question per invocation:

    *Which of my own already-authorized accounts should serve this call?*

and it answers it by preferring quota that is about to expire unused, while never
routing a call to a pool that cannot actually serve it.

The decision
------------
``slack = remaining_fraction - expected_demand_before_reset`` per window, ``min`` across
the windows that apply to the requested model class, then one of two mandatory regimes:

* **Regime A** -- someone has surplus (``min_slack > 0``): maximize
  ``provider_weight * capacity * min_slack``. Spend the quota most at risk of expiring.
* **Regime B** -- nobody has surplus: maximize
  ``provider_weight * capacity * min_remaining``. Spend from whoever can serve the call.

See :mod:`quota_router.types` for the full derivation, including why scaling a negative
slack by a capacity ratio is a bug rather than a shortcut.

What this package will never do
-------------------------------
* **It never proxies.** No ``ANTHROPIC_BASE_URL``, no ``ANTHROPIC_AUTH_TOKEN``, ever.
  Execution is always "spawn the vendor's own CLI with that account's own
  ``CLAUDE_CONFIG_DIR``". Anthropic blocked OAuth proxying on 2026-04-04 and routing
  around that is neither supported nor attempted.
* **It never mints a credential.** Usage is read with the access token an
  account already holds. Redeeming a refresh token rotates it, and any path that
  never invoked from library or CLI code. Switching the *global* active account behind
  the operator's back is exactly the failure this router exists to avoid.

Layout
------
``quota_router.types``
    Frozen, hashable value types and the shared constants. Pure.
``quota_router.scoring`` / ``quota_router.select``
    The decision function. Pure: stdlib plus :mod:`quota_router.types` only, ``now_s``
    passed in, no filesystem, env or clock access.
``quota_router.cli``
    The ``quotapick`` console script.

Only the value-type layer is re-exported here; the other modules are imported directly
(``from quota_router.scoring import ...``) so that importing this package can never
depend on, or fail because of, a sibling module.
"""

from __future__ import annotations

#: Distribution version. Kept as a plain literal because ``pyproject.toml`` reads it
#: statically (``[tool.setuptools.dynamic] version = {attr = "quota_router.__version__"}``)
#: and must not have to import the package to build it.
__version__ = "0.1.0"

from .types import (
    ACCOUNT_ANTIGRAVITY_CLAUDE,
    ACCOUNT_ANTIGRAVITY_GEMINI,
    ACCOUNT_CLAUDE,
    ACCOUNT_CLAUDE_B,
    ACCOUNT_CLAUDE_C,
    ACCOUNT_CODEX,
    ACCOUNT_IDS,
    ACCOUNT_PROVIDERS,
    CONFIDENCE_LABELS,
    DEFAULT_PROVIDER_WEIGHTS,
    MODEL_CLASS_FABLE,
    PROVIDER_ANTIGRAVITY,
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_UNKNOWN,
    PROVIDERS,
    QUOTA_ROUTER_CONTRACT_VERSION,
    REGIME_A,
    REGIME_B,
    SOURCE_ASSUMED,
    SOURCE_CACHE,
    SOURCE_CLAUDE_JSON,
    SOURCE_LIVE,
    SOURCE_MANUAL,
    SOURCE_UNKNOWN,
    TIER_CAPACITY,
    TIER_MAX_5X,
    TIER_MAX_20X,
    TIER_PRO,
    TIER_UNKNOWN,
    UNKNOWN_TIER_CAPACITY,
    AccountSnapshot,
    Decision,
    Identity,
    ScoreBreakdown,
    Window,
    WindowSlack,
    capacity_for_tier,
    confidence_label,
    is_canonical_account_id,
    normalize_model_class,
    normalize_regime,
    normalize_tier,
    provider_for_account_id,
)

__all__ = [
    "__version__",
    "QUOTA_ROUTER_CONTRACT_VERSION",
    # value types
    "Identity",
    "Window",
    "AccountSnapshot",
    "WindowSlack",
    "ScoreBreakdown",
    "Decision",
    # account ids / providers
    "ACCOUNT_CLAUDE",
    "ACCOUNT_CLAUDE_B",
    "ACCOUNT_CLAUDE_C",
    "ACCOUNT_CODEX",
    "ACCOUNT_ANTIGRAVITY_GEMINI",
    "ACCOUNT_ANTIGRAVITY_CLAUDE",
    "ACCOUNT_IDS",
    "ACCOUNT_PROVIDERS",
    "PROVIDER_CLAUDE",
    "PROVIDER_CODEX",
    "PROVIDER_ANTIGRAVITY",
    "PROVIDER_UNKNOWN",
    "PROVIDERS",
    "DEFAULT_PROVIDER_WEIGHTS",
    "is_canonical_account_id",
    "provider_for_account_id",
    # tiers / capacity
    "TIER_MAX_20X",
    "TIER_MAX_5X",
    "TIER_PRO",
    "TIER_UNKNOWN",
    "TIER_CAPACITY",
    "UNKNOWN_TIER_CAPACITY",
    "normalize_tier",
    "capacity_for_tier",
    # model classes
    "MODEL_CLASS_FABLE",
    "normalize_model_class",
    # provenance / confidence
    "SOURCE_LIVE",
    "SOURCE_CLAUDE_JSON",
    "SOURCE_MANUAL",
    "SOURCE_CACHE",
    "SOURCE_ASSUMED",
    "SOURCE_UNKNOWN",
    "CONFIDENCE_LABELS",
    "confidence_label",
    # regimes
    "REGIME_A",
    "REGIME_B",
    "normalize_regime",
]
