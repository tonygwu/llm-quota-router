"""Layered TOML configuration -- and a router that runs with none of it.

Layers, lowest priority first::

    builtin defaults
    ${XDG_CONFIG_HOME:-~/.config}/quota-router/config.toml   (optional)
    ./.quota-router.toml                                     (optional, per-project)
    $QUOTA_ROUTER_CONFIG                                      (explicit -> must exist)
    --config PATH                                             (explicit -> must exist)
    CLI flags                                                 (Config.with_overrides)

Merging is a *deep* merge per section, so a user file that sets one weight keeps every
other builtin value. Optional files that do not exist are skipped in silence; a file
named explicitly (env var or ``--config``) that cannot be read is a hard error, because
"the operator pointed at a config and we ignored it" is exactly the silent failure that
makes a routing decision unexplainable.

**Zero-config is a requirement, not a nicety.** ``quotapick pick`` on a machine with no
config file at all must produce a valid decision: the builtin defaults below carry the
three Claude config directories, the tier capacity ratios, the provider weights and the
model-class tables. Configuration exists to *adjust* policy, never to enable the router.

Everything here is a value object; nothing in this module makes a routing decision.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from . import model_classes as mc
from .pse import MIN_WEEKLY_TO_SESSION
from .types import (
    PROVIDER_ANTIGRAVITY,
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_CURSOR,
    PROVIDER_UNKNOWN,
    PROVIDERS,
    TIER_CAPACITY,
    TIER_UNKNOWN,
    UNKNOWN_TIER_CAPACITY,
    normalize_tier,
    provider_for_account_id,
)
from .weekly_reset import WeeklyReset, parse_weekly_reset

__all__ = [
    "ConfigError",
    "AccountConfig",
    "HysteresisConfig",
    "StalenessConfig",
    "EligibilityConfig",
    "PileupConfig",
    "OracleConfig",
    "ExecConfig",
    "Config",
    "BANNED_EXEC_ENV",
    "PROVIDER_CONFIG_DIR_ENV",
    "PROVIDER_COMMANDS",
    "builtin_config",
    "config_search_paths",
    "load_config",
]


class ConfigError(ValueError):
    """A configuration file is missing, malformed, or states something impossible.

    Always a *user* error: the CLI maps it to exit code 2 (flag/config errors are the one
    class of failure that is allowed to stop ``pick`` from answering).
    """


# ======================================================================================
# Hard rails
# ======================================================================================

#: Environment variables that must never reach a spawned CLI from this package. Setting
#: either one turns "spawn the vendor's own binary as the operator" into "proxy the
#: operator's OAuth session", which Anthropic blocked on 2026-04-04 and which this
#: package does not do. Config that tries to set them is rejected, loudly.
BANNED_EXEC_ENV: Final[tuple[str, ...]] = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")

#: Provider -> the environment variable that points its CLI at an account's own config.
#: This is the entire execution mechanism: no proxying, no token juggling, just "run the
#: real binary with that account's directory".
PROVIDER_CONFIG_DIR_ENV: Final[Mapping[str, str]] = MappingProxyType(
    {
        PROVIDER_CLAUDE: "CLAUDE_CONFIG_DIR",
        PROVIDER_CODEX: "CODEX_HOME",
        PROVIDER_ANTIGRAVITY: "AGY_CONFIG_DIR",
    }
)

#: Provider -> the binary ``quotapick exec`` spawns.
PROVIDER_COMMANDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        PROVIDER_CLAUDE: "claude",
        PROVIDER_CODEX: "codex",
        PROVIDER_CURSOR: "cursor-agent",
        PROVIDER_ANTIGRAVITY: "agy",
    }
)

_ENV_VAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


# ======================================================================================
# Coercion helpers (every one of them fails loud on a wrong *type*)
# ======================================================================================


def _where(section: str, key: str) -> str:
    return f"[{section}] {key}" if section else key


def _as_bool(value: Any, section: str, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{_where(section, key)} must be a boolean, got {value!r}")


def _as_number(
    value: Any,
    section: str,
    key: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{_where(section, key)} must be a number, got {value!r}")
    out = float(value)
    if out != out or out in (float("inf"), float("-inf")):
        raise ConfigError(f"{_where(section, key)} must be finite, got {value!r}")
    if minimum is not None and out < minimum:
        raise ConfigError(f"{_where(section, key)} must be >= {minimum}, got {out}")
    if maximum is not None and out > maximum:
        raise ConfigError(f"{_where(section, key)} must be <= {maximum}, got {out}")
    return out


def _as_int(value: Any, section: str, key: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{_where(section, key)} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{_where(section, key)} must be >= {minimum}, got {value}")
    return int(value)


def _as_str(value: Any, section: str, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{_where(section, key)} must be a string, got {value!r}")
    return value.strip()


def _as_weekly_reset(value: Any, section: str, key: str) -> WeeklyReset:
    """Parse a weekly reset schedule, reporting failure as a *config* error.

    The pure parser raises ``ValueError`` and knows nothing about files or sections.
    Its message says what is wrong with the value; this adds where the value came
    from, which is the half the operator needs in order to go and fix it.
    """
    text = _as_str(value, section, key)
    try:
        return parse_weekly_reset(text)
    except ValueError as exc:
        raise ConfigError(f"{_where(section, key)}: {exc}") from None


def _as_table(value: Any, section: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{section}] must be a table, got {value!r}")
    return value


def _as_float_table(
    value: Any, section: str, *, minimum: float | None = 0.0
) -> dict[str, float]:
    table = _as_table(value, section)
    return {
        str(key): _as_number(item, section, str(key), minimum=minimum)
        for key, item in table.items()
    }


def _as_str_table(value: Any, section: str) -> dict[str, str]:
    table = _as_table(value, section)
    return {str(key): _as_str(item, section, str(key)) for key, item in table.items()}


def _first_present(table: Mapping[str, Any], names: Sequence[str], default: Any) -> Any:
    """First of ``names`` actually present in ``table`` -- how TOML aliases are honored."""
    for name in names:
        if table.get(name) is not None:
            return table[name]
    return default


def expand_path(raw: str, env: Mapping[str, str] | None = None) -> str:
    """Expand ``~`` and ``$VAR`` in a configured path against ``env`` (not ``os.environ``).

    Taking the environment as a parameter is what lets the CLI be tested without mutating
    the real process environment.
    """
    environ = os.environ if env is None else env
    text = raw.strip()
    if not text:
        return text

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return environ.get(name, match.group(0))

    text = _ENV_VAR_PATTERN.sub(_substitute, text)
    if text == "~" or text.startswith("~/"):
        home = environ.get("HOME") or str(Path.home())
        text = home + text[1:]
    return text


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; ``overlay`` wins on scalars, tables merge key by key."""
    out: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        current = out.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            out[key] = _deep_merge(current, value)
        else:
            out[key] = value
    return out


# ======================================================================================
# Section value objects
# ======================================================================================


@dataclass(frozen=True, slots=True)
class AccountConfig:
    """How to reach, identify and weigh one spendable account.

    Args:
        id: Wire-format account id (see :data:`quota_router.types.ACCOUNT_IDS`).
        config_dir: The account's own CLI config directory. This *is* the execution
            mechanism -- it becomes ``CLAUDE_CONFIG_DIR`` (or the provider's equivalent)
            for the spawned binary.
        tier: Subscription tier. Usually left unset: the providers layer reads the real
            value out of ``<config_dir>/.claude.json``'s ``organizationRateLimitTier``.
            Setting it here is a manual override for accounts whose config is unreadable.
            when identity matching fails; positions get reassigned, identities do not.
        identity_email: The account's email, which together with the organization UUID is
            how accounts are actually keyed.
        provider: Overrides the provider inferred from :attr:`id`.
        enabled: ``False`` removes the account from routing entirely.
        weekly_to_session: This account's measured weekly:session capacity ratio (k),
            overriding :data:`quota_router.pse.DEFAULT_WEEKLY_TO_SESSION`. Set it from
            ``quotapick calibrate``, never by hand: it is the denominator that turns a
            weekly percentage into absolute PSE, so an account carrying the wrong one
            has its whole weekly pool mis-sized. Must be >= 1 -- the weekly window
            contains the session window, so a smaller ratio is arithmetically
            impossible rather than merely aggressive.
        calls_per_window: Calibration -- how many calls of the baseline model class this
            account's window holds. Drives the pileup reservation cost
            (``multiplier / calls_per_window``); ``quotapick calibrate`` estimates it.
        env: Extra environment overlaid on the spawned CLI (for example
            ``AGY_MODEL = "claude"`` to select the Antigravity Claude pool).
        env_var: Overrides the config-dir environment variable name for this account.
        command: Overrides the binary ``quotapick exec`` spawns for this account.
        weekly_reset: When this account's weekly window rolls over, as a wall time in
            a named zone (``"Mon 15:59 America/Los_Angeles"``). Fixed when the account
            is created, so unlike everything else here it stays knowable with no
            network, no Keychain and no token. It is read ONLY as a last resort, when
            the router obtained no usage measurement for any candidate at all; a
            measurement, including one that says the account is empty, always wins.
            See :mod:`quota_router.weekly_reset`.
    """

    id: str
    config_dir: str | None = None
    tier: str = TIER_UNKNOWN
    identity_email: str | None = None
    provider: str = ""
    enabled: bool = True
    weekly_to_session: float | None = None
    calls_per_window: float | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    env_var: str | None = None
    command: str | None = None
    weekly_reset: WeeklyReset | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", self.provider or provider_for_account_id(self.id))
        object.__setattr__(self, "tier", normalize_tier(self.tier))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))

    @property
    def config_dir_env_var(self) -> str:
        """Environment variable this account's :attr:`config_dir` is passed through."""
        return self.env_var or PROVIDER_CONFIG_DIR_ENV.get(
            self.provider, PROVIDER_CONFIG_DIR_ENV[PROVIDER_CLAUDE]
        )

    @property
    def exec_command(self) -> str:
        """Binary to spawn for this account."""
        return self.command or PROVIDER_COMMANDS.get(self.provider, self.provider or self.id)

    def exec_env(self) -> dict[str, str]:
        """The environment overlay for this account.

        Never contains :data:`BANNED_EXEC_ENV` -- construction rejects those keys, so this
        method cannot be the thing that leaks a proxy variable.
        """
        out: dict[str, str] = {}
        if self.config_dir and not self.is_default_config_dir:
            out[self.config_dir_env_var] = self.config_dir
        out.update(self.env)
        return out

    @property
    def is_default_config_dir(self) -> bool:
        """True when this account is the CLI's own default, selected by absence.

        Claude Code keeps the default account's config at ``~/.claude.json``,
        *outside* ``~/.claude``. Pointing CLAUDE_CONFIG_DIR at the directory makes it
        look inside, find nothing, and scaffold a fresh empty account -- so naming
        the default dir in an exec plan actively breaks the account it selects.
        Selecting it means leaving the variable unset.
        """
        if self.id != "claude" or not self.config_dir:
            return False
        # config_dir has already been expanded against whichever HOME the config was
        # loaded with, so re-expanding "~" here would compare against the *process*
        # home instead and never match under an injected environment. Compare the
        # conventional directory name instead, which is home-agnostic.
        #
        # Tradeoff, stated plainly: an operator who relocates the primary account to
        # some other path ending in ".claude" would have the variable omitted when it
        # should be set. That is a contrived layout; always setting it breaks the
        # ordinary one, which is the case that actually occurs.
        return os.path.basename(os.path.normpath(self.config_dir)) == ".claude"

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        return {
            "id": self.id,
            "provider": self.provider,
            "config_dir": self.config_dir,
            "tier": self.tier,
            "identity_email": self.identity_email,
            "enabled": self.enabled,
            "calls_per_window": self.calls_per_window,
            "env": dict(self.env),
            "weekly_reset": (
                self.weekly_reset.to_text() if self.weekly_reset is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class HysteresisConfig:
    """Stickiness: how much better a challenger must be before we switch accounts.

    These knob names mirror the selection layer's own contract exactly, because that layer
    reads them structurally out of whatever config object it is handed. The switch test it
    applies is::

        challenger.min_slack > incumbent.min_slack * switch_margin_ratio + switch_margin_abs

    on the **unscaled** ``min_slack``, never on the capacity-scaled score -- an additive
    epsilon on a scaled score is 4x stricter for a ``max_5x`` account than for a
    ``max_20x`` one, which would quietly pin the router to its biggest pool.

    Args:
        enabled: Master switch (``--no-sticky`` turns it off for one invocation).
        switch_margin_ratio: Multiplicative part of the switch test. TOML alias:
            ``switch_margin_multiplier``.
        switch_margin_abs: Additive part, in fraction-of-budget units. TOML alias:
            ``switch_margin``.
        min_dwell_calls: The incumbent serves at least this many calls before any switch
            is considered. This, not the margin, is what bounds thrashing in *both*
            regimes.
        max_dwell_s: Wall-clock escape hatch from the dwell, for low-frequency callers.
            TOML alias: ``ttl_s``.
    """

    enabled: bool = True
    switch_margin_ratio: float = 1.15
    switch_margin_abs: float = 0.02
    min_dwell_calls: int = 3
    max_dwell_s: float = 900.0


@dataclass(frozen=True, slots=True)
class StalenessConfig:
    """When usage numbers stop being trustworthy.

    Args:
        max_age_s: Older than this, an account is reported as degraded (still routable --
            stale data beats no decision, but the caller is told).
        confidence_floor: Confidence assigned to a snapshot past :attr:`max_age_s`.
        cache_max_age_s: Hard limit on the history-backed fallback used when the oracle
            is unavailable. Past it, the cached reading is dropped rather than believed.
        max_staleness_s: Handed to the selection layer, which shrinks a stale snapshot's
            score toward the field mean rather than trusting or discarding it outright.
    """

    max_age_s: float = 300.0
    confidence_floor: float = 0.3
    cache_max_age_s: float = 3600.0
    max_staleness_s: float = 900.0


@dataclass(frozen=True, slots=True)
class EligibilityConfig:
    """Who is allowed to win.

    Args:
        min_remaining: An account needs at least this fraction left in every applicable
            window to be eligible. Guards against routing to a pool with a sliver of
            quota that the very next call will exhaust mid-stream.
        min_remaining_configured: True when ``min_remaining`` came from
            ``--min-remaining`` or a config
            file rather than the built-in default. The two are the same number but not
            the same claim: the default is *our* sanity guard against a sliver of quota,
            while an explicit value is a bar the CALLER set and expects to bind. Only the
            latter justifies rejecting an account whose remaining quota is unknowable --
            see ``_partition_candidates``. Deliberately absent from ``to_dict``: it is
            provenance about the request, not part of the config contract.
    """

    min_remaining: float = 0.02
    min_remaining_configured: bool = False


@dataclass(frozen=True, slots=True)
class PileupConfig:
    """Concurrency control: stop N simultaneous callers from all picking one pool.

    Each pick writes a short-lived reservation; subsequent picks subtract recent
    reservations from that account's remaining budget before scoring. Without this, ten
    processes launched at once all see the same "claude_b has the most slack" snapshot
    and all pile onto claude_b.

    Args:
        enabled: Master switch.
        window_s: How long a reservation is subtracted for.
        calls_per_window: Default calibration when an account has none of its own.
        max_records: Cap on stored reservations, so the state file cannot grow unbounded.
    """

    enabled: bool = True
    window_s: float = 60.0
    calls_per_window: float = 200.0
    max_records: int = 500


@dataclass(frozen=True, slots=True)
class OracleConfig:
    """How to read ground truth.

    Ground truth is the vendor usage endpoint, reached with the access token the
    account already holds. ``command`` is retained only so an existing config file
    that still sets it keeps parsing; nothing reads it. It was the external oracle
    binary, which was removed after its usage read was found to redeem -- and so
    rotate -- the account's refresh token, logging the operator out.
    """

    command: str = ""
    timeout_ms: int = 5000
    use_cache_on_failure: bool = True


@dataclass(frozen=True, slots=True)
class ExecConfig:
    """Provider -> binary, for ``quotapick exec``."""

    commands: Mapping[str, str] = field(default_factory=lambda: dict(PROVIDER_COMMANDS))
    env_vars: Mapping[str, str] = field(default_factory=lambda: dict(PROVIDER_CONFIG_DIR_ENV))

    def __post_init__(self) -> None:
        object.__setattr__(self, "commands", MappingProxyType(dict(self.commands)))
        object.__setattr__(self, "env_vars", MappingProxyType(dict(self.env_vars)))


# ======================================================================================
# The whole configuration
# ======================================================================================


@dataclass(frozen=True, slots=True)
class Config:
    """Fully-merged, validated configuration."""

    accounts: Mapping[str, AccountConfig] = field(default_factory=dict)
    tiers: Mapping[str, float] = field(default_factory=lambda: dict(TIER_CAPACITY))
    providers: Mapping[str, float] = field(default_factory=dict)
    model_multipliers: Mapping[str, float] = field(default_factory=lambda: dict(mc.DEFAULT_MULTIPLIERS))
    model_patterns: Mapping[str, str] = field(default_factory=lambda: dict(mc.DEFAULT_PATTERNS))
    hysteresis: HysteresisConfig = field(default_factory=HysteresisConfig)
    staleness: StalenessConfig = field(default_factory=StalenessConfig)
    eligibility: EligibilityConfig = field(default_factory=EligibilityConfig)
    pileup: PileupConfig = field(default_factory=PileupConfig)
    oracle: OracleConfig = field(default_factory=OracleConfig)
    exec: ExecConfig = field(default_factory=ExecConfig)
    #: Model to fall back to when the requested one is exhausted on every account.
    #:
    #: Read by ``cl`` only, and **opt-in**: unset means "tell me and start anyway",
    #: never "quietly run something weaker". Substituting a model changes what the
    #: session can do, which is not a decision a quota router should make on the
    #: operator's behalf by default.
    #:
    #: Passed to the CLI verbatim, and that is the point: the value here is a
    #: *selection* (``"opus[1m]"``), whereas a transcript records only the server's
    #: *stamp* (``"claude-opus-5"``), from which the variant cannot be recovered. This
    #: is the one place ``cl`` injects ``--model``, because an override that is not
    #: passed through is not an override.
    fallback_model: str | None = None
    #: Files that were actually read, in application order.
    sources: tuple[str, ...] = ()
    #: Non-fatal complaints (unknown sections, unrecognized tiers, ...).
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "accounts", MappingProxyType(dict(self.accounts)))
        object.__setattr__(self, "tiers", MappingProxyType(dict(self.tiers)))
        object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))
        object.__setattr__(
            self, "model_multipliers", MappingProxyType(dict(self.model_multipliers))
        )
        object.__setattr__(self, "model_patterns", MappingProxyType(dict(self.model_patterns)))
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    # -- lookups -------------------------------------------------------------------------

    @property
    def account_ids(self) -> tuple[str, ...]:
        """Configured account ids, in configuration order."""
        return tuple(self.accounts)

    def account(self, account_id: str) -> AccountConfig | None:
        """Look up one account by id."""
        return self.accounts.get(account_id)

    def enabled_accounts(self) -> tuple[AccountConfig, ...]:
        """Accounts that have not been disabled with ``enabled = false``."""
        return tuple(account for account in self.accounts.values() if account.enabled)

    def capacity_for_tier(self, tier: Any) -> float:
        """Capacity ratio for ``tier``, honoring the ``[tiers]`` overrides."""
        return self.tiers.get(normalize_tier(tier), UNKNOWN_TIER_CAPACITY)

    def provider_weight(self, provider: str) -> float:
        """Policy weight for ``provider`` (unknown providers weigh 1.0, i.e. neutral)."""
        return self.providers.get(provider, 1.0)

    def calls_per_window(self, account_id: str) -> float:
        """Calibrated calls-per-window for one account, falling back to the default."""
        account = self.accounts.get(account_id)
        if account is not None and account.calls_per_window:
            return float(account.calls_per_window)
        return float(self.pileup.calls_per_window)

    def engine_cfg(self, *, sticky: bool = True) -> dict[str, Any]:
        """The policy view the pure decision layer reads.

        That layer may not import this module (it is pure), so it reads settings
        *structurally* by name out of whatever object it is handed. This method is the
        single place those names are spelled, and it deliberately emits a plain mapping:
        handing over ``self`` would expose bound methods where the layer expects values.

        Args:
            sticky: ``False`` (``--no-sticky``) neutralizes hysteresis for one invocation
                -- no dwell, and a switch test of ``challenger > incumbent`` exactly.
        """
        return {
            "provider_weights": dict(self.providers),
            "tier_capacities": dict(self.tiers),
            "min_remaining_floor": self.eligibility.min_remaining,
            "min_dwell_calls": self.hysteresis.min_dwell_calls if sticky else 0,
            "max_dwell_s": self.hysteresis.max_dwell_s,
            "switch_margin_ratio": self.hysteresis.switch_margin_ratio if sticky else 1.0,
            "switch_margin_abs": self.hysteresis.switch_margin_abs if sticky else 0.0,
            "max_staleness_s": self.staleness.max_staleness_s,
        }

    def tier_override_warnings(self) -> tuple[str, ...]:
        """Complain when ``[tiers]`` says something the scoring layer will not honor.

        Capacity is a property of :class:`~quota_router.types.AccountSnapshot`, derived
        from the tier through the shared ``TIER_CAPACITY`` table; the scoring layer reads
        it from there. An operator who edits ``[tiers]`` and sees no behaviour change
        deserves to be told why rather than left to conclude the router ignores config.
        """
        out: list[str] = []
        for tier, capacity in self.tiers.items():
            builtin = TIER_CAPACITY.get(normalize_tier(tier))
            if builtin is not None and abs(builtin - capacity) > 1e-9:
                out.append(
                    f"[tiers] {tier} = {capacity:g} differs from the built-in capacity "
                    f"ratio {builtin:g}; scoring reads capacity from the shared tier "
                    f"table, so this override is not applied"
                )
        return tuple(out)

    # -- CLI overrides -------------------------------------------------------------------

    def with_overrides(
        self,
        *,
        min_remaining: float | None = None,
        no_sticky: bool = False,
        pileup: bool | None = None,
        timeout_ms: int | None = None,
        only: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> Config:
        """Apply the CLI-flag layer -- the last and highest-priority layer.

        ``only``/``exclude`` are applied as ``enabled`` flips rather than by deleting
        accounts, so an account the operator filtered out can still be *reported* (with a
        reason) instead of vanishing from the ranked output.
        """
        out = self
        if min_remaining is not None:
            out = replace(
                out,
                eligibility=EligibilityConfig(
                    min_remaining=min_remaining, min_remaining_configured=True
                ),
            )
        if no_sticky:
            out = replace(out, hysteresis=replace(out.hysteresis, enabled=False))
        if pileup is not None:
            out = replace(out, pileup=replace(out.pileup, enabled=pileup))
        if timeout_ms is not None:
            out = replace(out, oracle=replace(out.oracle, timeout_ms=timeout_ms))

        only_set = {item for item in (only or ()) if item}
        exclude_set = {item for item in (exclude or ()) if item}
        if only_set or exclude_set:
            accounts = {
                account_id: replace(
                    account,
                    enabled=account.enabled
                    and (not only_set or account_id in only_set)
                    and account_id not in exclude_set,
                )
                for account_id, account in out.accounts.items()
            }
            out = replace(out, accounts=accounts)
        return out

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping (plain types only)."""
        return {
            "accounts": {k: v.to_dict() for k, v in self.accounts.items()},
            "tiers": dict(self.tiers),
            "providers": dict(self.providers),
            "model_classes": {
                "multipliers": dict(self.model_multipliers),
                "patterns": dict(self.model_patterns),
            },
            "hysteresis": {
                "enabled": self.hysteresis.enabled,
                "switch_margin_ratio": self.hysteresis.switch_margin_ratio,
                "switch_margin_abs": self.hysteresis.switch_margin_abs,
                "min_dwell_calls": self.hysteresis.min_dwell_calls,
                "max_dwell_s": self.hysteresis.max_dwell_s,
            },
            "staleness": {
                "max_age_s": self.staleness.max_age_s,
                "confidence_floor": self.staleness.confidence_floor,
                "cache_max_age_s": self.staleness.cache_max_age_s,
                "max_staleness_s": self.staleness.max_staleness_s,
            },
            "eligibility": {"min_remaining": self.eligibility.min_remaining},
            "pileup": {
                "enabled": self.pileup.enabled,
                "window_s": self.pileup.window_s,
                "calls_per_window": self.pileup.calls_per_window,
                "max_records": self.pileup.max_records,
            },
            "oracle": {
                "command": self.oracle.command,
                "timeout_ms": self.oracle.timeout_ms,
                "use_cache_on_failure": self.oracle.use_cache_on_failure,
            },
            "sources": list(self.sources),
            "warnings": list(self.warnings),
        }


# ======================================================================================
# Builtin defaults
# ======================================================================================

#: The zero-config layer. Account ids are the wire format from
#: :data:`quota_router.types.ACCOUNT_IDS`; the three Claude directories are the standard
#: multi-account layout (``~/.claude`` plus ``CLAUDE_CONFIG_DIR`` siblings). Tiers are
#: deliberately absent -- the providers layer reads the real tier from each account's own
#: ``.claude.json`` rather than trusting a guess baked into a default.
_BUILTIN: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "accounts": {
            "claude": {"config_dir": "~/.claude"},
            "claude_b": {"config_dir": "~/.claude-b"},
            "claude_c": {"config_dir": "~/.claude-c"},
            "claude_d": {"config_dir": "~/.claude-d"},
            "codex": {"config_dir": "~/.codex"},
            # Cursor has no per-account config directory. A second Cursor account is
            # declared with its own CURSOR_API_KEY in this table's `env` overlay.
            "cursor": {},
            # Antigravity has no per-account config directory: the two pools behind the
            # one CLI are selected by AGY_MODEL, where a value containing "claude" means
            # the Claude pool and anything else means Gemini. An empty overlay is
            # therefore the correct, complete description of the Gemini pool.
            "antigravity_gemini": {},
            "antigravity_claude": {"env": {"AGY_MODEL": "claude"}},
        },
        "tiers": {"max_20x": 1.0, "max_5x": 0.25, "pro": 1.0, "unknown": 1.0},
        "providers": {"claude": 1.0, "codex": 0.7, "cursor": 0.5, "antigravity": 0.3},
        "model_classes": {
            "multipliers": dict(mc.DEFAULT_MULTIPLIERS),
            "patterns": dict(mc.DEFAULT_PATTERNS),
        },
        "hysteresis": {
            "enabled": True,
            "switch_margin_ratio": 1.15,
            "switch_margin_abs": 0.02,
            "min_dwell_calls": 3,
            "max_dwell_s": 900.0,
        },
        "staleness": {
            "max_age_s": 300.0,
            "confidence_floor": 0.3,
            "cache_max_age_s": 3600.0,
            "max_staleness_s": 900.0,
        },
        "eligibility": {"min_remaining": 0.02},
        "pileup": {
            "enabled": True,
            "window_s": 60.0,
            "calls_per_window": 200.0,
            "max_records": 500,
        },
        "oracle": {"command": "", "timeout_ms": 5000, "use_cache_on_failure": True},
        "exec": {
            "commands": dict(PROVIDER_COMMANDS),
            "env_vars": dict(PROVIDER_CONFIG_DIR_ENV),
        },
    }
)

_KNOWN_SECTIONS: Final[frozenset[str]] = frozenset(_BUILTIN)

#: Top-level *scalar* settings. Kept separate from :data:`_KNOWN_SECTIONS`, which is
#: derived from the builtin defaults and therefore lists only tables. A scalar has no
#: builtin entry to be derived from, so without this it reads as an unknown section and
#: is reported as ignored -- while being applied normally. A false "ignored" is worse
#: than a missing warning: it invites someone to "fix" a config that was already right.
_KNOWN_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset({"fallback_model"})


# ======================================================================================
# Loading
# ======================================================================================


def config_search_paths(
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> tuple[tuple[Path, bool], ...]:
    """The file layers to try, in order, as ``(path, required)`` pairs.

    ``required`` marks a path the operator named explicitly: missing or unreadable, it is
    an error rather than a skipped layer.
    """
    environ = dict(os.environ if env is None else env)
    base = Path(cwd) if cwd is not None else Path.cwd()

    xdg = environ.get("XDG_CONFIG_HOME", "").strip()
    home = environ.get("HOME") or str(Path.home())
    config_home = Path(expand_path(xdg, environ)) if xdg else Path(home) / ".config"

    paths: list[tuple[Path, bool]] = [
        (config_home / "quota-router" / "config.toml", False),
        (base / ".quota-router.toml", False),
    ]
    explicit = environ.get("QUOTA_ROUTER_CONFIG", "").strip()
    if explicit:
        paths.append((Path(expand_path(explicit, environ)), True))
    return tuple(paths)


def _read_toml(path: Path, *, required: bool) -> Mapping[str, Any] | None:
    """Read one TOML layer; ``None`` when an optional layer is absent."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise ConfigError(f"config file not found: {path}") from None
        return None
    except OSError as exc:
        if required:
            raise ConfigError(f"cannot read config file {path}: {exc}") from None
        return None
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from None


def _build_accounts(
    raw: Any, env: Mapping[str, str] | None, warnings: list[str]
) -> dict[str, AccountConfig]:
    table = _as_table(raw, "accounts")
    accounts: dict[str, AccountConfig] = {}
    for account_id, body in table.items():
        section = f"accounts.{account_id}"
        body = _as_table(body, section)

        config_dir = body.get("config_dir")
        if config_dir is not None:
            config_dir = expand_path(_as_str(config_dir, section, "config_dir"), env) or None

        raw_tier = body.get("tier")
        tier = TIER_UNKNOWN
        if raw_tier is not None:
            text = _as_str(raw_tier, section, "tier")
            tier = normalize_tier(text)
            if tier == TIER_UNKNOWN and text:
                warnings.append(
                    f"[{section}] tier {text!r} is not a tier this router knows; "
                    f"treating capacity as unknown (1.0)"
                )

        extra_env_raw = body.get("env", {})
        extra_env = _as_str_table(extra_env_raw, f"{section}.env") if extra_env_raw else {}
        for banned in BANNED_EXEC_ENV:
            for key in extra_env:
                if key.strip().upper() == banned:
                    raise ConfigError(
                        f"[{section}.env] must not set {banned}: this router never "
                        f"proxies -- execution is always the vendor CLI with that "
                        f"account's own config directory"
                    )

        accounts[str(account_id)] = AccountConfig(
            id=str(account_id),
            config_dir=config_dir,
            tier=tier,
            identity_email=(
                _as_str(body["identity_email"], section, "identity_email")
                if body.get("identity_email") is not None
                else None
            ),
            provider=(
                _as_str(body["provider"], section, "provider")
                if body.get("provider") is not None
                else ""
            ),
            enabled=(
                _as_bool(body["enabled"], section, "enabled")
                if body.get("enabled") is not None
                else True
            ),
            weekly_to_session=(
                _as_number(
                    body["weekly_to_session"],
                    section,
                    "weekly_to_session",
                    # The weekly window contains the session window, so a ratio below
                    # 1 is not a stricter policy -- it is an impossible measurement.
                    minimum=MIN_WEEKLY_TO_SESSION,
                )
                if body.get("weekly_to_session") is not None
                else None
            ),
            calls_per_window=(
                _as_number(
                    body["calls_per_window"], section, "calls_per_window", minimum=1e-9
                )
                if body.get("calls_per_window") is not None
                else None
            ),
            env=extra_env,
            env_var=(
                _as_str(body["env_var"], section, "env_var")
                if body.get("env_var") is not None
                else None
            ),
            command=(
                _as_str(body["command"], section, "command")
                if body.get("command") is not None
                else None
            ),
            weekly_reset=(
                _as_weekly_reset(body["weekly_reset"], section, "weekly_reset")
                if body.get("weekly_reset") is not None
                else None
            ),
        )

        # An account whose provider resolves to nothing has no adapter behind it, so it
        # can never be read and never be routed to. Saying so here is the difference
        # between a config that does nothing and a config that says it does nothing:
        # the account is accepted by every layer above and then quietly vanishes from
        # `status`, which reads as a broken router rather than a rejected account.
        resolved_provider = accounts[str(account_id)].provider
        if resolved_provider not in PROVIDERS:
            named = (
                f"provider {resolved_provider!r}"
                if resolved_provider != PROVIDER_UNKNOWN
                else "no provider, and none could be inferred from the account id"
            )
            warnings.append(
                f"[{section}] has {named}; this router has adapters for "
                f"{', '.join(PROVIDERS)}. The account is configured but unroutable: "
                f"nothing will read it and nothing can launch on it"
            )
    return accounts


def _build(
    merged: Mapping[str, Any],
    env: Mapping[str, str] | None,
    sources: list[str],
    *,
    eligibility_explicit: bool = False,
) -> Config:
    """Validate a merged mapping into a :class:`Config`."""
    warnings: list[str] = []
    for section in merged:
        if section in _KNOWN_SECTIONS or section in _KNOWN_TOP_LEVEL_KEYS:
            continue
        known = ", ".join(sorted(_KNOWN_SECTIONS | _KNOWN_TOP_LEVEL_KEYS))
        warnings.append(f"unknown config setting [{section}] ignored (known: {known})")

    hysteresis_raw = _as_table(merged.get("hysteresis", {}), "hysteresis")
    staleness_raw = _as_table(merged.get("staleness", {}), "staleness")
    eligibility_raw = _as_table(merged.get("eligibility", {}), "eligibility")
    pileup_raw = _as_table(merged.get("pileup", {}), "pileup")
    oracle_raw = _as_table(merged.get("oracle", {}), "oracle")
    exec_raw = _as_table(merged.get("exec", {}), "exec")
    model_raw = _as_table(merged.get("model_classes", {}), "model_classes")

    return Config(
        accounts=_build_accounts(merged.get("accounts", {}), env, warnings),
        tiers=_as_float_table(merged.get("tiers", {}), "tiers"),
        providers=_as_float_table(merged.get("providers", {}), "providers"),
        model_multipliers=_as_float_table(
            model_raw.get("multipliers", {}), "model_classes.multipliers"
        ),
        model_patterns=_as_str_table(model_raw.get("patterns", {}), "model_classes.patterns"),
        hysteresis=HysteresisConfig(
            enabled=_as_bool(hysteresis_raw.get("enabled", True), "hysteresis", "enabled"),
            switch_margin_ratio=_as_number(
                _first_present(
                    hysteresis_raw, ("switch_margin_ratio", "switch_margin_multiplier"), 1.15
                ),
                "hysteresis",
                "switch_margin_ratio",
                minimum=0.0,
            ),
            switch_margin_abs=_as_number(
                _first_present(
                    hysteresis_raw, ("switch_margin_abs", "switch_margin"), 0.02
                ),
                "hysteresis",
                "switch_margin_abs",
                minimum=0.0,
                maximum=1.0,
            ),
            min_dwell_calls=_as_int(
                hysteresis_raw.get("min_dwell_calls", 3),
                "hysteresis",
                "min_dwell_calls",
                minimum=0,
            ),
            max_dwell_s=_as_number(
                _first_present(hysteresis_raw, ("max_dwell_s", "ttl_s"), 900.0),
                "hysteresis",
                "max_dwell_s",
                minimum=0.0,
            ),
        ),
        staleness=StalenessConfig(
            max_age_s=_as_number(
                staleness_raw.get("max_age_s", 300.0), "staleness", "max_age_s", minimum=0.0
            ),
            confidence_floor=_as_number(
                staleness_raw.get("confidence_floor", 0.3),
                "staleness",
                "confidence_floor",
                minimum=0.0,
                maximum=1.0,
            ),
            cache_max_age_s=_as_number(
                staleness_raw.get("cache_max_age_s", 3600.0),
                "staleness",
                "cache_max_age_s",
                minimum=0.0,
            ),
            max_staleness_s=_as_number(
                staleness_raw.get("max_staleness_s", 900.0),
                "staleness",
                "max_staleness_s",
                minimum=0.0,
            ),
        ),
        eligibility=EligibilityConfig(
            min_remaining=_as_number(
                eligibility_raw.get("min_remaining", 0.02),
                "eligibility",
                "min_remaining",
                minimum=0.0,
                maximum=1.0,
            ),
            min_remaining_configured=eligibility_explicit,
        ),
        pileup=PileupConfig(
            enabled=_as_bool(pileup_raw.get("enabled", True), "pileup", "enabled"),
            window_s=_as_number(
                pileup_raw.get("window_s", 60.0), "pileup", "window_s", minimum=0.0
            ),
            calls_per_window=_as_number(
                pileup_raw.get("calls_per_window", 200.0),
                "pileup",
                "calls_per_window",
                minimum=1e-9,
            ),
            max_records=_as_int(
                pileup_raw.get("max_records", 500), "pileup", "max_records", minimum=0
            ),
        ),
        oracle=OracleConfig(
            command=_as_str(oracle_raw.get("command", ""), "oracle", "command"),
            timeout_ms=_as_int(
                oracle_raw.get("timeout_ms", 5000), "oracle", "timeout_ms", minimum=1
            ),
            use_cache_on_failure=_as_bool(
                oracle_raw.get("use_cache_on_failure", True), "oracle", "use_cache_on_failure"
            ),
        ),
        exec=ExecConfig(
            commands=_as_str_table(exec_raw.get("commands", {}), "exec.commands")
            or dict(PROVIDER_COMMANDS),
            env_vars=_as_str_table(exec_raw.get("env_vars", {}), "exec.env_vars")
            or dict(PROVIDER_CONFIG_DIR_ENV),
        ),
        fallback_model=(
            _as_str(merged["fallback_model"], "", "fallback_model") or None
            if merged.get("fallback_model") is not None
            else None
        ),
        sources=tuple(sources),
        warnings=tuple(warnings),
    )


def builtin_config(env: Mapping[str, str] | None = None) -> Config:
    """The zero-config layer on its own -- what the router uses with no files at all."""
    return _build(_BUILTIN, env, [])


def load_config(
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    explicit_path: str | os.PathLike[str] | None = None,
) -> Config:
    """Load and merge every configuration layer.

    Args:
        env: Environment to read ``XDG_CONFIG_HOME`` / ``QUOTA_ROUTER_CONFIG`` / ``HOME``
            from. Defaults to ``os.environ``; passed explicitly by the CLI so tests never
            touch the real process environment.
        cwd: Directory to look for ``./.quota-router.toml`` in.
        explicit_path: ``--config PATH``. Highest-priority file layer, and required to
            exist.

    Raises:
        ConfigError: A named file is missing/unreadable, the TOML is invalid, or a value
            has the wrong type or an impossible range.
    """
    merged: dict[str, Any] = dict(_BUILTIN)
    sources: list[str] = []

    layers: list[tuple[Path, bool]] = list(config_search_paths(env, cwd))
    if explicit_path is not None:
        layers.append((Path(expand_path(str(explicit_path), env)), True))

    # Provenance, tracked while merging because the merged result cannot carry it:
    # ``_BUILTIN`` always supplies ``eligibility.min_remaining``, so key-presence in
    # ``merged`` is true even with zero config files. Only a real file layer means an
    # operator asked for this bar. Presence of the key, not its value -- a config that
    # spells out the default 0.02 is still someone asking for that floor.
    eligibility_explicit = False

    for path, required in layers:
        data = _read_toml(path, required=required)
        if data is None:
            continue
        eligibility = data.get("eligibility")
        if isinstance(eligibility, Mapping) and "min_remaining" in eligibility:
            eligibility_explicit = True
        merged = _deep_merge(merged, data)
        sources.append(str(path))

    return _build(merged, env, sources, eligibility_explicit=eligibility_explicit)
