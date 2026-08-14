"""Primary Claude source: the ``cswap list --json`` oracle.

``cswap`` already knows every Claude account the operator is logged into and already
fetches each one's usage from the vendor. This adapter reads that, and *only* that.

Two hard rules, both encoded here rather than left to reviewer memory:

* **Oracle, never executor.** The only command this module may ever run is
  :data:`CSWAP_LIST_ARGV` -- ``cswap list --json``. ``cswap run`` / ``switch`` / ``auto``
  / ``add`` / ``remove`` mutate the operator's *global* active account behind their back,
  which is exactly the failure this router exists to prevent. There is a test asserting
  the argv.
* **Accounts key by identity.** ``(email, organizationUuid)`` identifies a quota pool;
  a config directory does not. Directories get re-pointed, re-logged-in and renumbered,
  and none of that may change which pool a decision refers to.

The tier (``max_20x`` vs ``max_5x``) is *not* reported by cswap. It lives in
``organizationRateLimitTier`` inside each account's ``.claude.json``, which is also where
the identity -> ``CLAUDE_CONFIG_DIR`` mapping comes from, so one filesystem pass gives
both. On this machine the default account's file is ``~/.claude.json`` (a sibling of the
config directory, not a file inside it) while ``claude_b`` / ``claude_c`` keep theirs at
``~/.claude-b/.claude.json``; both layouts are probed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ..types import (
    ACCOUNT_CLAUDE,
    ACCOUNT_CLAUDE_B,
    ACCOUNT_CLAUDE_C,
    SOURCE_CSWAP,
    TIER_UNKNOWN,
    AccountSnapshot,
    Identity,
    Window,
    normalize_model_class,
    normalize_tier,
)
from .base import (
    FIVE_HOUR_S,
    SEVEN_DAY_S,
    WINDOW_KEY_5H,
    WINDOW_KEY_7D,
    ProviderAdapter,
    Runner,
    decay_confidence,
    make_window,
    parse_timestamp,
    pct_to_fraction,
    read_json_file,
    run_command,
)

__all__ = [
    "CSWAP_BINARY",
    "CSWAP_LIST_ARGV",
    "DEFAULT_CLAUDE_CONFIG_DIR_NAMES",
    "CLAUDE_CONFIG_DIRS_ENV",
    "ClaudeAccountConfig",
    "discover_claude_configs",
    "ClaudeCswapAdapter",
    "parse_cswap_payload",
]

#: The oracle binary. Overridable per-instance for tests and odd installs.
CSWAP_BINARY: Final[str] = "cswap"

#: The one and only argv this package may run against cswap.
CSWAP_LIST_ARGV: Final[tuple[str, ...]] = ("list", "--json")

#: Account id -> ``CLAUDE_CONFIG_DIR`` directory name under ``$HOME``.
DEFAULT_CLAUDE_CONFIG_DIR_NAMES: Final[Mapping[str, str]] = {
    ACCOUNT_CLAUDE: ".claude",
    ACCOUNT_CLAUDE_B: ".claude-b",
    ACCOUNT_CLAUDE_C: ".claude-c",
}

#: Optional JSON env override: ``{"claude": "/abs/dir", "claude_b": "/abs/dir"}``.
CLAUDE_CONFIG_DIRS_ENV: Final[str] = "QUOTA_ROUTER_CLAUDE_CONFIG_DIRS"

#: Freshness knobs for the cswap read. cswap re-fetches usage on demand, so anything
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

    ``declared_email`` and ``cswap_number`` come from the operator's own policy file and
    are only consulted when the local ``.claude.json`` could not answer: an email the
    operator wrote down is still identity matching, while the oracle's positional number
    is a last-resort fallback, because positions are reassigned when accounts are added
    or removed and identities are not.
    """

    account_id: str
    config_dir: Path
    identity: Identity | None = None
    tier: str = TIER_UNKNOWN
    source_path: Path | None = None
    declared_email: str | None = None
    cswap_number: int | None = None

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


def _match_by_identity(
    identity: Identity,
    configs: Sequence[ClaudeAccountConfig],
    claimed: Container[str],
) -> tuple[ClaudeAccountConfig | None, str]:
    """Identity matching, strongest rung first. Returns ``(config, how)``.

    1. exact identity -- ``(email, organizationUuid)``;
    2. email only, when exactly one config claims it. The operator can be moved between
       organizations (a personal account converted to an org, a seat re-provisioned) and
       the uuid changes while the human, the subscription and the directory do not;
    3. an email the operator declared in their own policy file, used when the local
       ``.claude.json`` was unreadable.
    """
    available = [config for config in configs if config.account_id not in claimed]
    key = identity.key
    for config in available:
        if config.identity is not None and config.identity.key == key:
            return config, "identity"

    email = key[0]
    email_matches = [
        config
        for config in available
        if config.identity is not None and config.identity.key[0] == email
    ]
    if len(email_matches) == 1:
        return email_matches[0], "email"

    declared = [
        config
        for config in available
        if config.declared_email and config.declared_email.strip().casefold() == email
    ]
    if len(declared) == 1:
        return declared[0], "declared-email"

    return None, "none"


def _match_by_number(
    account_number: Any,
    configs: Sequence[ClaudeAccountConfig],
    claimed: Container[str],
) -> tuple[ClaudeAccountConfig | None, str]:
    """The last-resort positional fallback, run only after every identity match is in.

    This is the one rung that is *not* identity matching, so it must never outrank one:
    it only ever sees configs that no identity claimed, and callers lower the snapshot's
    confidence when it fires.
    """
    if isinstance(account_number, bool) or not isinstance(account_number, int):
        return None, "none"
    numbered = [
        config
        for config in configs
        if config.cswap_number == account_number and config.account_id not in claimed
    ]
    if len(numbered) == 1:
        return numbered[0], "cswap-number"
    return None, "none"


# ======================================================================================
# Payload parsing
# ======================================================================================


def _window_from_entry(
    entry: Any,
    *,
    key: str,
    length_s: float,
    observed_at_s: float,
    applies_to: frozenset[str] | None = None,
) -> Window | None:
    """Turn one cswap usage block (``fiveHour`` / ``sevenDay`` / a ``scoped[]`` item) into a Window.

    ``pct`` is a *used* percent. ``expectedPct`` -- present on the 7-day and scoped
    windows, absent on the 5-hour one -- is the upstream pacing baseline and is carried
    through verbatim; when it is missing, :class:`Window` derives the baseline from
    ``resetsAt`` and ``length_s``.

    ``aheadOfPace`` is deliberately ignored. It is self-inconsistent upstream (the real
    capture has ``pct=60`` against ``expectedPct=54.6`` reporting ``false``, while
    ``pct=80`` against the same baseline reports ``true``), so the sign is recomputed
    from the numbers by :meth:`Window.slack`.
    """
    if not isinstance(entry, Mapping):
        return None
    return make_window(
        key=key,
        used_fraction=pct_to_fraction(entry.get("pct")),
        length_s=length_s,
        resets_at_s=parse_timestamp(entry.get("resetsAt")),
        observed_at_s=observed_at_s,
        applies_to=applies_to,
        expected_used_fraction=pct_to_fraction(entry.get("expectedPct")),
    )


def _scoped_windows(
    scoped: Any, *, observed_at_s: float, taken: set[str]
) -> list[Window]:
    """Build the per-model-class windows from ``usage.scoped[]``.

    Each entry's ``name`` (``"Fable"``) becomes both the window key and its
    ``applies_to`` model class -- this is the mechanism by which the requested model
    class gates routing. An entry with no usable name is kept as an *account-wide*
    window: dropping it would delete a real constraint and flatter the account, which is
    the more dangerous direction.
    """
    if not isinstance(scoped, Sequence) or isinstance(scoped, (str, bytes)):
        return []
    windows: list[Window] = []
    for index, entry in enumerate(scoped):
        if not isinstance(entry, Mapping):
            continue
        model_class = normalize_model_class(entry.get("name"))
        key = model_class or "scoped"
        if key in taken:
            key = f"{key}#{index + 1}"
        window = _window_from_entry(
            entry,
            key=key,
            length_s=SEVEN_DAY_S,
            observed_at_s=observed_at_s,
            applies_to=frozenset({model_class}) if model_class else None,
        )
        if window is not None:
            taken.add(window.key)
            windows.append(window)
    return windows


def _observed_at(account: Mapping[str, Any], now_s: float) -> float:
    """When cswap last actually fetched this account's usage."""
    fetched = parse_timestamp(account.get("usageFetchedAt"))
    if fetched is not None:
        return fetched
    age = account.get("usageAgeSeconds")
    if isinstance(age, (int, float)) and not isinstance(age, bool):
        return now_s - float(age)
    return now_s


def parse_cswap_payload(
    payload: Any,
    now_s: float,
    *,
    configs: Iterable[ClaudeAccountConfig] = (),
    warnings: list[str] | None = None,
) -> list[AccountSnapshot]:
    """Turn a decoded ``cswap list --json`` document into snapshots. Never raises.

    An account is *skipped entirely* (no snapshot at all, per the adapter contract) when
    its ``usageStatus`` is anything other than ``ok``, when it carries no parseable usage
    window, or when its identity maps to no local config directory -- in that last case
    the router could not spawn the CLI for it even if it picked it, so offering it as a
    candidate would be a lie.

    Resolution runs in two passes (identity first, then the positional fallback) and each
    config may be claimed once, so the result never contains two snapshots for one account
    id -- a duplicate would count one quota pool twice in the ranking.
    """
    notes = warnings if warnings is not None else []
    if not isinstance(payload, Mapping):
        notes.append("cswap: payload was not a JSON object")
        return []
    accounts = payload.get("accounts")
    if not isinstance(accounts, Sequence) or isinstance(accounts, (str, bytes)):
        notes.append("cswap: payload had no 'accounts' array")
        return []

    known = tuple(configs)

    # Resolve identities to config directories in two passes. Every identity match is
    # made first, and only then may an unclaimed config be picked up by the positional
    # fallback -- otherwise one stale `cswap_number` in the policy file could steal the
    # directory that an identity was about to claim, producing two snapshots for one
    # account id (the same pool counted twice, and a call spent from the wrong one).
    pending: list[tuple[Mapping[str, Any], Identity, str]] = []
    for raw in accounts:
        if not isinstance(raw, Mapping):
            continue
        identity = Identity.from_mapping(raw)
        label = identity.email or f"account #{raw.get('number')}"
        if not identity.email:
            notes.append("cswap: skipped an account with no email (cannot key by identity)")
            continue
        pending.append((raw, identity, label))

    claimed: set[str] = set()
    assignment: dict[int, tuple[ClaudeAccountConfig, str]] = {}
    for index, (_raw, identity, _label) in enumerate(pending):
        config, matched_by = _match_by_identity(identity, known, claimed)
        if config is not None:
            assignment[index] = (config, matched_by)
            claimed.add(config.account_id)
    for index, (raw, _identity, _label) in enumerate(pending):
        if index in assignment:
            continue
        config, matched_by = _match_by_number(raw.get("number"), known, claimed)
        if config is not None:
            assignment[index] = (config, matched_by)
            claimed.add(config.account_id)

    snapshots: list[AccountSnapshot] = []
    for index, (raw, identity, label) in enumerate(pending):
        resolved = assignment.get(index)
        if resolved is None:
            notes.append(
                f"cswap: skipped {label} - no local CLAUDE_CONFIG_DIR maps to this identity; "
                f"add it to {CLAUDE_CONFIG_DIRS_ENV} or log in under ~/.claude*"
            )
            continue
        config, matched_by = resolved

        status = raw.get("usageStatus")
        if isinstance(status, str) and status.strip().casefold() != "ok":
            notes.append(f"cswap: skipped {label} (usageStatus={status!r})")
            continue

        usage = raw.get("usage")
        if not isinstance(usage, Mapping):
            notes.append(f"cswap: skipped {label} (no usage block)")
            continue

        observed_at_s = _observed_at(raw, now_s)
        windows: list[Window] = []
        taken: set[str] = set()

        five_hour = _window_from_entry(
            usage.get("fiveHour"),
            key=WINDOW_KEY_5H,
            length_s=FIVE_HOUR_S,
            observed_at_s=observed_at_s,
        )
        if five_hour is not None:
            windows.append(five_hour)
            taken.add(five_hour.key)

        seven_day = _window_from_entry(
            usage.get("sevenDay"),
            key=WINDOW_KEY_7D,
            length_s=SEVEN_DAY_S,
            observed_at_s=observed_at_s,
        )
        if seven_day is not None:
            windows.append(seven_day)
            taken.add(seven_day.key)

        windows.extend(_scoped_windows(usage.get("scoped"), observed_at_s=observed_at_s, taken=taken))

        if not windows:
            notes.append(f"cswap: skipped {label} (no parseable usage window)")
            continue

        confidence = decay_confidence(
            max(0.0, now_s - observed_at_s), fresh_s=_FRESH_S, stale_s=_STALE_S
        )
        if matched_by == "cswap-number":
            confidence *= _NUMBER_MATCH_CONFIDENCE
            notes.append(
                f"cswap: matched {label} to {config.account_id} by positional number "
                f"{raw.get('number')!r}, not by identity - positions are reassigned when "
                f"accounts are added or removed"
            )
        if config.tier == TIER_UNKNOWN:
            confidence *= _UNKNOWN_TIER_CONFIDENCE
            notes.append(
                f"cswap: {config.account_id} tier unknown (no organizationRateLimitTier in "
                f"{config.config_dir}); capacity stays neutral, confidence lowered"
            )

        try:
            snapshots.append(
                AccountSnapshot(
                    id=config.account_id,
                    windows=tuple(windows),
                    tier=config.tier,
                    source=SOURCE_CSWAP,
                    confidence=confidence,
                    available=True,
                    note=f"cswap oracle; config dir {config.config_dir}",
                    identity=identity,
                )
            )
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            notes.append(f"cswap: skipped {label} ({type(exc).__name__}: {exc})")

    return snapshots


# ======================================================================================
# The adapter
# ======================================================================================


class ClaudeCswapAdapter(ProviderAdapter):
    """Read every Claude account's usage from ``cswap list --json``.

    Args:
        runner: Injected process runner (defaults to :func:`subprocess.run` via
            :func:`~quota_router.providers.base.default_runner`). Tests always inject.
        binary: Path/name of the cswap binary.
        timeout_s: Budget for the oracle call; a timeout yields no snapshots.
        home / config_dirs / env: Passed through to :func:`discover_claude_configs`.
        configs: Pre-resolved configs, for callers that already did the discovery.
    """

    name = "claude_cswap"

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        binary: str = CSWAP_BINARY,
        timeout_s: float = 10.0,
        home: Path | str | None = None,
        config_dirs: Mapping[str, Path | str] | None = None,
        env: Mapping[str, str] | None = None,
        configs: Iterable[ClaudeAccountConfig] | None = None,
    ) -> None:
        self._runner = runner
        self._binary = binary
        self._timeout_s = timeout_s
        self._home = home
        self._config_dirs = config_dirs
        self._env = env
        self._configs = tuple(configs) if configs is not None else None
        #: Human-readable diagnostics from the most recent :meth:`snapshot` call. The
        #: protocol returns snapshots only, so degraded detail rides here for the CLI.
        self.warnings: tuple[str, ...] = ()

    @property
    def argv(self) -> tuple[str, ...]:
        """The exact command this adapter runs. Read-only, and read-only by design."""
        return (self._binary, *CSWAP_LIST_ARGV)

    def configs(self) -> tuple[ClaudeAccountConfig, ...]:
        """Resolved local config directories (cached per instance)."""
        if self._configs is None:
            self._configs = discover_claude_configs(
                home=self._home, config_dirs=self._config_dirs, env=self._env
            )
        return self._configs

    def snapshot(self, now_s: float) -> list[AccountSnapshot]:
        """Run the oracle and map its answer onto snapshots. Never raises."""
        warnings: list[str] = []
        outcome = run_command(
            self.argv, runner=self._runner, timeout_s=self._timeout_s, env=self._env
        )
        if not outcome.ok:
            warnings.append(f"cswap: {outcome.error}")
            self.warnings = tuple(warnings)
            return []

        try:
            payload = json.loads(outcome.stdout)
        except (ValueError, TypeError) as exc:
            warnings.append(f"cswap: unparseable JSON ({exc})")
            self.warnings = tuple(warnings)
            return []

        snapshots = parse_cswap_payload(
            payload, now_s, configs=self.configs(), warnings=warnings
        )
        self.warnings = tuple(warnings)
        return snapshots
