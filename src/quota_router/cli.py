"""``quotapick`` -- the command line entry point.

Subcommands
-----------
``pick``
    Decide which account to spend from and print the decision as JSON on stdout.
``exec``
    ``pick``, then spawn that account's own vendor CLI. The child's exit code is passed
    through **unchanged**; only a router-level failure (nothing to spawn) uses 127.
``status``
    What the router currently believes about every account and window.
``explain``
    The same decision as ``pick``, rendered for a human.
``calibrate``
    Estimate ``calls_per_window`` per account from the recorded history.

Contracts this module is responsible for
----------------------------------------
**``pick`` always answers.** It emits valid JSON and exits 0 on every path except flag and
config errors (exit 2). Oracle down, state file locked, scoring engine missing, every
account exhausted -- all of those produce a *degraded* decision, never a crash and never a
non-zero exit. A router that fails closed just moves the outage into the caller.

**The ranked list is the payload.** The TypeScript consumer maps ``ranked`` onto a retry
order, so every eligible candidate is reported in order, not just the winner.

**Never proxy.** ``exec.env`` carries a config-directory pointer and nothing else;
``ANTHROPIC_BASE_URL`` and ``ANTHROPIC_AUTH_TOKEN`` are never emitted, and ``exec``
actively *removes* them from the child environment if the operator's shell has them set.

**This package never mints a credential.** It reads the access token an account
already holds and calls the vendor usage endpoint with it; it never redeems a
``remove`` change the globally-active account and are never called.

``main()`` takes ``argv``/``env``/``stdout``/``stderr``/``now_s`` so the whole CLI is
directly callable from a test with no subprocess and no clock.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, TextIO

from . import explain as explain_mod
from . import forensics as forensics_mod
from . import history as history_mod
from . import model_classes as mc
from . import pse
from . import waste as waste_mod
from .config import BANNED_EXEC_ENV, Config, ConfigError, PileupConfig, load_config
from .state import GLOBAL_SCOPE, StateSnapshot, StateStore
from .types import (
    PROVIDER_CLAUDE,
    PROVIDERS,
    WINDOW_KEY_5H,
    normalize_model_class,
    QUOTA_ROUTER_CONTRACT_VERSION,
    SOURCE_CACHE,
    AccountSnapshot,
    Decision,
    ScoreBreakdown,
    unreadable_reason,
)

__all__ = ["main", "Deps", "EXIT_OK", "EXIT_USAGE", "EXIT_ROUTER_FAILURE"]

#: Everything went fine -- including every *degraded* path of ``pick``.
EXIT_OK: Final[int] = 0
#: Bad flags or bad config. The one class of failure allowed to stop ``pick`` answering.
EXIT_USAGE: Final[int] = 2
#: Router-level failure in ``exec``: there was nothing to hand the call off to. Child exit
#: codes are passed through untouched, so this is only ever *our* failure.
EXIT_ROUTER_FAILURE: Final[int] = 127

_PROG: Final[str] = "quotapick"

#: Providers whose CLI selects an account by *config directory*. For these, refusing to
#: spawn without one is a safety rail, not pedantry: the alternative is spending from
#: whichever account the ambient environment points at.
_CONFIG_DIR_PROVIDERS: Final[frozenset[str]] = frozenset({"claude", "codex"})


# ======================================================================================
# Injectable dependencies
# ======================================================================================


@dataclass
class Deps:
    """Everything the CLI reaches outside itself, injectable for tests.

    ``rank``/``select`` come from the pure decision layer, ``load_snapshots`` from the
    providers layer, ``run`` from :mod:`subprocess`. All four default to the real thing
    and are resolved *lazily*, so importing this module can never fail because a sibling
    module is missing.
    """

    rank: Callable[..., Any] | None = None
    select: Callable[..., Any] | None = None
    load_snapshots: Callable[..., Any] | None = None
    run: Callable[..., Any] = subprocess.run
    engine_error: str | None = None
    oracle_error: str | None = None

    @classmethod
    def resolve(cls, given: Deps | None = None) -> Deps:
        """Fill in whatever the caller did not inject."""
        deps = given or cls()
        if deps.rank is None or deps.select is None:
            rank, select, error = _load_engine()
            deps = replace(
                deps,
                rank=deps.rank or rank,
                select=deps.select or select,
                engine_error=deps.engine_error or error,
            )
        if deps.load_snapshots is None:
            loader, error = _load_oracle()
            deps = replace(
                deps,
                load_snapshots=loader,
                oracle_error=deps.oracle_error or error,
            )
        return deps


def _load_engine() -> tuple[Callable[..., Any] | None, Callable[..., Any] | None, str | None]:
    """Import the pure decision layer (``scoring.rank`` + ``select.select``)."""
    try:
        from . import scoring  # type: ignore[attr-defined]
        from . import select as select_module  # type: ignore[attr-defined]
    except ImportError as exc:
        return None, None, f"scoring/select unavailable: {exc}"
    rank = getattr(scoring, "rank", None)
    select = getattr(select_module, "select", None)
    if rank is None or select is None:
        return rank, select, "scoring.rank / select.select not found"
    return rank, select, None


def _load_oracle() -> tuple[Callable[..., Any] | None, str | None]:
    """Bind the providers layer into a single ``(**kwargs) -> (snapshots, warnings)`` call.

    The providers layer is a *set* of adapters (live usage, the statusline cache, codex
    sessions, antigravity) merged by account id, so the binding is "build the standard
    adapters, then collect". Adapters are consulted in preference order and a failing one
    is absorbed there, which is why this layer can treat the result as ground truth or
    nothing at all.
    """
    try:
        from . import providers  # type: ignore[attr-defined]
    except ImportError as exc:
        return None, f"providers unavailable: {exc}"

    build = getattr(providers, "build_default_adapters", None)
    collect = getattr(providers, "collect_snapshots", None)
    if callable(build) and callable(collect):

        def load_snapshots(**kwargs: Any) -> Any:
            adapters = _configure_adapters(
                providers,
                build(),
                config=kwargs.get("config"),
                env=kwargs.get("env"),
                run=kwargs.get("run"),
                timeout_s=kwargs.get("timeout_s"),
            )
            return collect(adapters, kwargs["now_s"])

        return load_snapshots, None

    for name in ("load_snapshots", "load_accounts", "snapshots", "load"):
        candidate = getattr(providers, name, None)
        if callable(candidate):
            return candidate, None
    return None, "providers exposes no snapshot loader"


def _configure_adapters(
    providers: Any,
    adapters: Sequence[Any],
    *,
    config: Config | None,
    env: Mapping[str, str] | None,
    run: Callable[..., Any] | None,
    timeout_s: float | None,
) -> tuple[Any, ...]:
    """Rebuild the providers layer's standard adapters with this run's settings.

    The providers layer owns *which* adapters exist and in what preference order; this
    layer owns the settings they should run with -- the injected subprocess runner, the
    environment, ``--timeout-ms``, and the operator's own ``[accounts.*] config_dir``
    entries so a non-standard directory is actually discovered.

    The timeout is applied **only** to the live-usage adapter. It reads the
    Keychain and calls the vendor usage endpoint, so it is the one adapter with a
    latency budget worth configuring; the others read local files. An adapter that
    cannot be rebuilt is kept exactly as the providers layer constructed it.

    The account list is passed as ``configs``, which is what both Claude adapters
    declare. An earlier version built a ``config_dirs`` mapping instead; no adapter
    declares that name, so :func:`_supported_kwargs` dropped it without a word and the
    adapters fell back to the builtin ``~/.claude*`` directories. An operator who
    declared an account beyond those builtins got it silently ignored.
    """
    live_cls = getattr(providers, "ClaudeOAuthAdapter", None)
    from_policy = getattr(providers, "claude_configs_from_policy", None)

    claude_configs: tuple[Any, ...] | None = None
    if config is not None and callable(from_policy):
        claude_configs = tuple(from_policy(config, env=env)) or None

    out: list[Any] = []

    for adapter in adapters:
        cls = type(adapter)
        settings: dict[str, Any] = {"env": env, "runner": run, "configs": claude_configs}
        if live_cls is not None and cls is live_cls and config is not None:
            settings["timeout_s"] = timeout_s

        wanted = {key: value for key, value in settings.items() if value is not None}
        try:
            supported = _supported_kwargs(cls.__init__, wanted)
            out.append(cls(**supported) if supported else adapter)
        except Exception:  # noqa: BLE001 - a rebuild failure must not lose the adapter
            out.append(adapter)
    return tuple(out)


def _supported_kwargs(fn: Callable[..., Any], candidates: Mapping[str, Any]) -> dict[str, Any]:
    """Filter ``candidates`` down to the keyword arguments ``fn`` actually declares.

    The decision and providers layers are written by other hands against the same value
    types; this keeps the CLI from breaking when one of them takes ``timeout_s`` and
    another takes ``timeout_ms``, without any layer having to guess about the others.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return {}
    parameters = signature.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return dict(candidates)
    return {name: value for name, value in candidates.items() if name in parameters}


def _first_param_name(fn: Callable[..., Any]) -> str:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover
        return ""
    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return parameter.name
    return ""


def _call(fn: Callable[..., Any], primary: Any, candidates: Mapping[str, Any]) -> Any:
    """Call ``fn(primary, **supported)``.

    The first positional parameter's name is removed from the keyword set first: a
    ``rank(candidates, **kwargs)`` signature accepts every keyword we offer, and passing
    ``candidates=`` alongside the positional argument would be a ``TypeError`` rather than
    the compatibility this filtering exists to provide.
    """
    supported = dict(_supported_kwargs(fn, candidates))
    supported.pop(_first_param_name(fn), None)
    return fn(primary, **supported)


# ======================================================================================
# Small helpers
# ======================================================================================


def _iso(epoch_s: float) -> str:
    return (
        datetime.fromtimestamp(epoch_s, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_now(value: str | None) -> float | None:
    """``--now`` accepts epoch seconds or an ISO-8601 timestamp."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ConfigError(
            f"--now must be epoch seconds or an ISO-8601 timestamp, got {value!r}"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _split_list(values: Iterable[str] | None) -> tuple[str, ...]:
    """``--only a,b --only c`` -> ``("a", "b", "c")``."""
    out: list[str] = []
    for value in values or ():
        for item in str(value).split(","):
            item = item.strip()
            if item and item not in out:
                out.append(item)
    return tuple(out)


def _dump_json(payload: Any, stream: TextIO) -> None:
    json.dump(payload, stream, indent=2, sort_keys=False, default=str)
    stream.write("\n")


# ======================================================================================
# Pipeline
# ======================================================================================


@dataclass
class Prepared:
    """Everything one invocation computed, before rendering."""

    config: Config
    model: mc.ModelResolution
    now_s: float
    #: What the router works from: the observed snapshot with in-flight pileup
    #: reservations already subtracted.
    snapshots: tuple[AccountSnapshot, ...] = ()
    #: What the oracle actually reported, untouched. History records *this* -- the log is
    #: the observability record of real usage, and writing our own reservation adjustment
    #: into it would both lie about the account and feed calibration its own output.
    raw_snapshots: tuple[AccountSnapshot, ...] = ()
    candidates: tuple[AccountSnapshot, ...] = ()
    #: True when the decision came from _exhausted_fallback rather than the scoring
    #: layer. Distinct from Decision.degraded, which is ALSO set when any input
    #: snapshot is merely stale -- conflating the two made an unrelated account's
    #: 48-minute-old cache report meets_policy=false for a perfectly good winner.
    from_fallback: bool = False
    fallback_pool: tuple[AccountSnapshot, ...] = ()
    decision: Decision = field(default_factory=Decision)
    cli_excluded: tuple[ScoreBreakdown, ...] = ()
    state: StateSnapshot = field(default_factory=StateSnapshot)
    selection_state: dict[str, Any] = field(default_factory=dict)
    reserved: Mapping[str, float] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    degraded: tuple[dict[str, Any], ...] = ()

    @property
    def snapshot_map(self) -> dict[str, AccountSnapshot]:
        return {snapshot.id: snapshot for snapshot in self.snapshots}


def _unclaimed_account_warnings(
    config: Config, snapshots: Sequence[AccountSnapshot]
) -> list[str]:
    """Name every enabled account that no adapter reported on.

    The config layer accepts any account id under any known provider, but the adapters
    decide what they actually read. A Claude account is discovered from its config
    directory, so declaring one works; the codex and antigravity adapters each report a
    fixed set, so declaring a second account under them produces nothing at all.

    Without this the operator gets the worst possible answer to "I added an account":
    the file parses, no error appears, and the account is simply missing from `status`.
    That reads as a broken router rather than an unsupported request, so it is worth a
    line naming the account and saying plainly that nothing will read it.
    """
    reported = {snapshot.id for snapshot in snapshots}
    out: list[str] = []
    for account in config.enabled_accounts():
        if account.id in reported or account.provider not in PROVIDERS:
            # An unknown provider is already reported by the config layer; saying it
            # twice in different words invites a hunt for two separate problems.
            continue
        out.append(
            f"account {account.id!r} is configured under provider {account.provider!r} "
            f"but no adapter reported it, so it cannot be routed to. The {account.provider} "
            f"adapter may support only its default account"
        )
    return out


def _fetch_snapshots(
    config: Config,
    deps: Deps,
    env: Mapping[str, str],
    now_s: float,
    warnings: list[str],
    degraded: list[dict[str, Any]],
) -> tuple[AccountSnapshot, ...]:
    """Read ground truth, falling back to the history cache, then to nothing.

    Three tiers, each strictly worse than the last and each *reported*: a live oracle
    read; the most recent history record replayed as ``source="cache"``; or an empty set,
    which routes on config alone and says so.
    """
    loader = deps.load_snapshots
    if loader is not None:
        candidates = {
            "config": config,
            "env": dict(env),
            "now_s": now_s,
            "timeout_s": config.oracle.timeout_ms / 1000.0,
            "timeout_ms": config.oracle.timeout_ms,
            "run": deps.run,
            "runner": deps.run,
            "accounts": config.enabled_accounts(),
        }
        try:
            result = loader(**_supported_kwargs(loader, candidates))
            snapshots, loader_warnings = _normalize_snapshot_result(result)
            warnings.extend(loader_warnings)
            if snapshots:
                warnings.extend(_unclaimed_account_warnings(config, snapshots))
                return snapshots
            warnings.append("oracle returned no accounts")
        except Exception as exc:  # noqa: BLE001 - the oracle may fail any way it likes
            warnings.append(f"oracle failed: {type(exc).__name__}: {exc}")
    elif deps.oracle_error:
        warnings.append(deps.oracle_error)

    if not config.oracle.use_cache_on_failure:
        degraded.append({"account": None, "reason": "no live usage data; cache disabled"})
        return ()

    record = history_mod.latest_record(env=env)
    if record is not None:
        observed = record.get("t")
        age = now_s - float(observed) if isinstance(observed, (int, float)) else None
        if age is not None and age <= config.staleness.cache_max_age_s:
            cached = history_mod.record_to_snapshots(
                record,
                source=SOURCE_CACHE,
                confidence=config.staleness.confidence_floor,
            )
            if cached:
                warnings.append(
                    f"oracle unavailable; replaying the history snapshot from "
                    f"{explain_mod.format_duration(age)} ago"
                )
                for snapshot in cached:
                    degraded.append(
                        {
                            "account": snapshot.id,
                            "reason": f"cached reading, {explain_mod.format_duration(age)} old",
                        }
                    )
                return cached
        else:
            warnings.append(
                "oracle unavailable and the cached history snapshot is too old to trust"
            )

    degraded.append(
        {"account": None, "reason": "no usage data at all; routing on configuration order"}
    )
    return ()


def _normalize_snapshot_result(result: Any) -> tuple[tuple[AccountSnapshot, ...], list[str]]:
    """Accept the shapes a snapshot loader might reasonably return."""
    warnings: list[str] = []
    payload = result

    if isinstance(result, tuple) and len(result) == 2 and not isinstance(result[0], AccountSnapshot):
        payload, raw_warnings = result
        warnings = [str(item) for item in (raw_warnings or ())]
    elif hasattr(result, "snapshots"):
        payload = result.snapshots
        warnings = [str(item) for item in (getattr(result, "warnings", None) or ())]

    if isinstance(payload, AccountSnapshot):
        return (payload,), warnings
    if isinstance(payload, Mapping):
        payload = list(payload.values())
    if not isinstance(payload, (list, tuple)):
        return (), warnings + [f"oracle returned {type(payload).__name__}, expected snapshots"]

    snapshots = tuple(item for item in payload if isinstance(item, AccountSnapshot))
    if len(snapshots) != len(payload):
        warnings.append("oracle returned entries that were not AccountSnapshots; ignored")
    return snapshots, warnings


#: Ceiling on how much in-flight reservations may add to an account's used fraction.
#:
#: Reservations are sized by an per-call cost that is essentially uncalibrated, and
#: they clear on a 60-second timer rather than on completion -- so a sustained batch
#: keeps dozens alive at once. Observed live: ~79 concurrent reservations at 0.5%
#: each subtracted ~40% of a window whose real consumption was a fraction of that,
#: and the router refused calls against an account that was mostly free.
#:
#: Sizing them correctly needs days of routed traffic to calibrate. A ceiling needs
#: none: pileup still spreads work across accounts, which is the whole point, but it
#: can no longer manufacture exhaustion out of a bad constant.
MAX_PILEUP_FRACTION: Final[float] = 0.25


def _apply_pileup(
    snapshots: Sequence[AccountSnapshot], reserved: Mapping[str, float]
) -> tuple[AccountSnapshot, ...]:
    """Subtract in-flight reservations from each account's remaining budget.

    Implemented as a *snapshot* transformation rather than a scoring rule: the decision
    layer stays pure and keeps ranking exactly what it is given, and "what is left" simply
    accounts for calls this router has already dispatched but the oracle has not seen yet.
    """
    if not reserved:
        return tuple(snapshots)

    out: list[AccountSnapshot] = []
    for snapshot in snapshots:
        raw_cost = reserved.get(snapshot.id, 0.0)
        if raw_cost <= 0.0 or not snapshot.windows:
            out.append(snapshot)
            continue
        cost = min(raw_cost, MAX_PILEUP_FRACTION)
        windows = tuple(
            replace(window, used_fraction=min(1.0, window.used_fraction + cost))
            for window in snapshot.windows
        )
        capped = "" if cost == raw_cost else f" (capped from {raw_cost:.4f})"
        note = (snapshot.note + "; " if snapshot.note else "") + (
            f"{cost:.4f} reserved by in-flight calls{capped}"
        )
        out.append(replace(snapshot, windows=windows, note=note))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Partition:
    """The result of applying CLI/config policy to the fetched snapshots.

    Args:
        candidates: Accounts the decision layer is allowed to rank.
        excluded: Everything filtered out, each with a reason.
        fallback_pool: Who the exhausted-fallback may still name. Accounts dropped for
            being *out of quota* stay in the pool (the fallback's whole job is to name the
            one that frees up first); accounts dropped by *policy* -- disabled,
            ``--exclude``, ``--only``, unavailable -- must never come back, or ``--exclude
            claude_c`` would end up returning ``claude_c``.
    """

    candidates: tuple[AccountSnapshot, ...] = ()
    excluded: tuple[ScoreBreakdown, ...] = ()
    fallback_pool: tuple[AccountSnapshot, ...] = ()


def _partition_candidates(
    config: Config,
    snapshots: Sequence[AccountSnapshot],
    model_class: str | None,
    now_s: float,
) -> Partition:
    """Split snapshots into routable candidates and CLI-level exclusions.

    Exclusions here are *policy* (disabled, ``--only``/``--exclude``, unavailable, below
    ``--min-remaining``), not scoring. They are reported with reasons rather than dropped,
    because "why was claude_c not even considered?" is the second question every operator
    asks.
    """
    candidates: list[AccountSnapshot] = []
    excluded: list[ScoreBreakdown] = []
    fallback_pool: list[AccountSnapshot] = []
    min_remaining = config.eligibility.min_remaining

    for snapshot in snapshots:
        account = config.account(snapshot.id)
        reason = ""
        policy_reject = True
        if account is not None and not account.enabled:
            reason = "disabled by configuration or --only/--exclude"
        elif not snapshot.available:
            # An unread account gets a reason that says so in as many words. The bare
            # note ("acct@example.com: access token expired") reads as an aside next to
            # "only 0.0% left in its tightest applicable window", and the two demand
            # opposite responses: one needs a login, the other needs a wait.
            reason = (
                unreadable_reason(snapshot)
                or snapshot.note
                or "account is not available"
            )
        else:
            remaining = snapshot.min_remaining_fraction(model_class)
            if remaining is None:
                # No applicable window means remaining is UNKNOWN, not plentiful. The
                # comparison below skipped None, so a window-less pool sailed through
                # every floor: with --min-remaining 0.99 the three accounts we can
                # actually read were excluded at 20% and the antigravity pool -- the one
                # account nobody had measured -- was handed the call. An unknown value
                # must never satisfy a bar.
                #
                # Only when the CALLER set a bar. The built-in 0.02 default is our own
                # sanity guard, not a request, and window-less pools are already judged
                # downstream by the scoring layer ("no usage windows in snapshot") --
                # rejecting them here on the default path would change how the normal
                # route treats the antigravity pools for no gain. A floor of exactly 0
                # is likewise nothing to fail.
                if config.eligibility.min_remaining_configured and min_remaining > 0.0:
                    reason = _unverifiable_against_floor_reason(min_remaining)
                    # Kept OUT of the fallback pool, unlike an account that is merely
                    # below the floor. The pool's contract is "name whoever frees up
                    # first", and that is unanswerable here: no window means no reset,
                    # so this account never demonstrably clears the bar. Leaving it in
                    # would let it win the fallback and reinstate the exact bug.
            elif remaining < min_remaining:
                reason = (
                    f"only {remaining:.1%} left in its tightest applicable window "
                    f"(min-remaining {min_remaining:.1%})"
                )
                policy_reject = False

        if reason:
            excluded.append(
                _breakdown_for(
                    snapshot, model_class, now_s=now_s, eligible=False, reason=reason
                )
            )
            if not policy_reject:
                fallback_pool.append(snapshot)
        else:
            candidates.append(snapshot)
            fallback_pool.append(snapshot)

    return Partition(
        candidates=tuple(candidates),
        excluded=tuple(excluded),
        fallback_pool=tuple(fallback_pool),
    )


def _breakdown_for(
    snapshot: AccountSnapshot,
    model_class: str | None,
    *,
    now_s: float,
    eligible: bool,
    reason: str,
    fits: bool | None = None,
) -> ScoreBreakdown:
    """A :class:`ScoreBreakdown` the CLI builds itself (exclusions and fallbacks)."""
    min_slack = snapshot.min_slack(now_s, model_class)
    min_remaining = snapshot.min_remaining_fraction(model_class)
    return ScoreBreakdown(
        account_id=snapshot.id,
        score=0.0,
        min_slack=0.0 if min_slack is None else min_slack,
        binding_window=snapshot.binding_window_key(now_s, model_class),
        capacity=snapshot.capacity,
        fits=(min_remaining is not None and min_remaining > 0.0) if fits is None else fits,
        per_window=snapshot.slacks(now_s, model_class),
        eligible=eligible,
        reason=reason,
        min_remaining=min_remaining,
    )


def _earliest_reset(
    snapshot: AccountSnapshot, model_class: str | None
) -> float:
    """Soonest reset among applicable windows -- ``inf`` when there are none."""
    windows = snapshot.applicable_windows(model_class)
    if not windows:
        return float("inf")
    return min(window.resets_at_s for window in windows)


def _fits(
    snapshot: AccountSnapshot, model_class: str | None, min_remaining: float = 0.0
) -> bool:
    """Does this account satisfy what the CALLER asked for, right now?

    ``min_remaining`` is the caller's own bar. Ignoring it made ``fits`` mean "has
    some quota left", which is a different and much weaker claim -- a batch harness
    that set ``--min-remaining 0.50`` still got ``fits: true`` from an account with
    5% left, so the threshold could not be used as a stop signal. A bar the router
    quietly stops applying is worse than no bar.
    """
    remaining = snapshot.min_remaining_fraction(model_class)
    return remaining is not None and remaining > max(0.0, min_remaining)


def _available_at(
    snapshots: Sequence[AccountSnapshot], model_class: str | None
) -> float | None:
    """Earliest moment any account's applicable windows refill.

    When nothing fits, "no" alone forces the caller to poll blindly. This is the
    soonest reset across the whole field, so a batch daemon can sleep exactly once
    instead of waking every 30 minutes to rediscover the same answer.

    Deliberately optimistic: it is the first moment quota *could* exist, not a
    promise that the caller's whole workload will fit then. A caller that wakes and
    finds it still does not fit gets a later timestamp and sleeps again, which
    converges. A pessimistic estimate would strand usable quota.
    """
    resets = [
        r for r in (_earliest_reset(s, model_class) for s in snapshots) if r != float("inf")
    ]
    return min(resets) if resets else None


def _exhausted_fallback(
    snapshots: Sequence[AccountSnapshot],
    model_class: str | None,
    now_s: float,
    *,
    cause: str | None = None,
    min_remaining: float = 0.0,
) -> Decision:
    """Answer without the decision layer: whoever can serve it, else whoever frees up first.

    Two different situations land here, and they must not be reported the same way:

    * **Everything is spent.** The honest answer is "nowhere right now, and claude_b is
      12m from being able to" -- ``fits=False`` and a reset deadline, which is far more
      actionable than an error and lets the consumer schedule a retry.
    * **The decision layer is unavailable or threw.** The accounts may be perfectly
      healthy; only the scoring is missing. Reporting ``fits=False`` here would be a lie
      that tells the consumer not to bother trying, so ``fits`` is always computed from
      the snapshot rather than assumed.

    Ordering is deliberately *not* a re-implementation of the scoring algorithm: accounts
    that can serve the call come first, and ties break on the soonest reset. There is
    exactly one scoring algorithm in this package and it lives in the pure layer.
    """
    if not snapshots:
        return Decision(
            chosen=None,
            reason=cause or "no accounts are configured or visible",
            degraded=True,
            warnings=("no candidates at all",),
        )

    ordered = sorted(
        snapshots,
        key=lambda item: (
            not _fits(item, model_class, min_remaining),
            _earliest_reset(item, model_class),
        ),  # ordering may use the caller's bar; the reported `fits` may not
    )
    winner = ordered[0]
    winner_fits = _fits(winner, model_class)

    reset_s = _earliest_reset(winner, model_class)
    when = (
        f"; earliest reset in {explain_mod.format_duration(reset_s - now_s)}"
        if reset_s != float("inf")
        else ""
    )
    if winner_fits:
        reason = cause or "ranked without scoring; first account able to serve the call"
        warnings = ("decided without the scoring layer",)
    else:
        reason = f"every candidate is out of quota{when}"
        warnings = ("no candidate can serve this call right now",)

    ranked = [
        _breakdown_for(
            snapshot,
            model_class,
            now_s=now_s,
            eligible=True,
            # Pass the caller's bar through explicitly. Left to its own devices
            # _breakdown_for computes "has any quota at all", which is a weaker claim
            # than the caller made and silently turns a threshold into a suggestion.
            fits=_fits(snapshot, model_class),
            reason=(reason if snapshot is winner else "degraded fallback ordering"),
        )
        for snapshot in ordered
    ]
    return Decision(
        chosen=winner.id,
        ranked=tuple(ranked),
        reason=reason,
        degraded=True,
        warnings=warnings,
    )


def _mark_fallback(flag: list[bool] | None) -> None:
    """Record that the fallback path ran. Returns None so it composes with `or`."""
    if flag is not None:
        flag.append(True)
    return None


def _decide(
    prepared: Prepared,
    deps: Deps,
    warnings: list[str],
    degraded: list[dict[str, Any]],
    *,
    record: bool,
    fallback_flag: list[bool] | None = None,
) -> Decision:
    """Hand the candidates to the pure decision layer and normalize what comes back."""
    config = prepared.config
    model_class = prepared.model.model_class
    now_s = prepared.now_s
    candidates = prepared.candidates
    bar = config.eligibility.min_remaining

    if not candidates:
        return _mark_fallback(fallback_flag) or _exhausted_fallback(prepared.fallback_pool, model_class, now_s, min_remaining=bar)

    if deps.rank is None or deps.select is None:
        warnings.append(
            (deps.engine_error or "decision layer unavailable")
            + "; falling back to earliest-reset order"
        )
        degraded.append({"account": None, "reason": "scoring engine unavailable"})
        return _mark_fallback(fallback_flag) or _exhausted_fallback(
            candidates, model_class, now_s, cause="scoring layer unavailable"
        )

    sticky = config.hysteresis.enabled
    # The selection layer owns hysteresis and keeps its dwell counters in a plain dict it
    # mutates in place; this layer only persists that dict. --no-sticky withholds it
    # entirely, so the incumbent cannot influence this one invocation.
    selection_state: dict[str, Any] = dict(prepared.state.selection) if sticky else {}
    prepared.selection_state = selection_state

    shared: dict[str, Any] = {
        "now_s": now_s,
        "cfg": config.engine_cfg(sticky=sticky),
        "model_class": model_class,
        "state": selection_state if sticky else None,
        "record": record,
    }

    try:
        ranked = _call(deps.rank, candidates, shared)
    except Exception as exc:  # noqa: BLE001 - never let scoring take the CLI down
        warnings.append(f"scoring failed: {type(exc).__name__}: {exc}")
        degraded.append({"account": None, "reason": "scoring raised"})
        return _mark_fallback(fallback_flag) or _exhausted_fallback(candidates, model_class, now_s, cause="scoring failed", min_remaining=bar)

    # ``select`` may be written to take either the ranked breakdowns or the raw snapshots
    # as its subject; both are offered and the first positional parameter's name decides.
    wants_snapshots = _first_param_name(deps.select).startswith(
        ("snap", "account", "candidate")
    )
    primary: Any = candidates if wants_snapshots else ranked
    extra = dict(shared)
    extra["ranked"] = ranked
    extra["breakdowns"] = ranked
    extra["snapshots"] = candidates

    try:
        decision = _call(deps.select, primary, extra)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"selection failed: {type(exc).__name__}: {exc}")
        degraded.append({"account": None, "reason": "selection raised"})
        return _mark_fallback(fallback_flag) or _exhausted_fallback(candidates, model_class, now_s, cause="selection failed", min_remaining=bar)

    if not isinstance(decision, Decision):
        warnings.append(
            f"selection returned {type(decision).__name__}, expected Decision; "
            f"falling back to earliest-reset order"
        )
        return _mark_fallback(fallback_flag) or _exhausted_fallback(candidates, model_class, now_s, min_remaining=bar)

    if decision.chosen is None:
        # The scoring layer ran and qualified nobody. That is a fallback exactly like
        # the other five sites, and it must be marked as one -- otherwise meets_policy
        # reports TRUE for a decision no candidate qualified for, which is the whole
        # failure this field exists to prevent.
        _mark_fallback(fallback_flag)
        fallback = _exhausted_fallback(candidates, model_class, now_s, min_remaining=bar)
        return replace(
            fallback,
            excluded=tuple(decision.excluded) + fallback.excluded,
            warnings=tuple(decision.warnings) + fallback.warnings,
            reason=decision.reason or fallback.reason,
        )
    return decision



def exclude_accounts_blind_to_model_class(
    snapshots: Sequence[AccountSnapshot], model_class: str | None
) -> tuple[list[AccountSnapshot], list[dict[str, Any]]]:
    """Drop accounts that cannot observe a scoped limit the rest of the fleet reports.

    A model-scoped window missing from a snapshot has two possible meanings, and
    they are opposite:

    * the plan has no such limit -- absence is correct, score normally;
    * this source could not see it -- absence is a blind spot, and scoring the
      account as unconstrained is certainly wrong.

    The fleet distinguishes them. If any account reports a window scoped to the
    requested class, that limit is real on this plan, and an account without one is
    unmeasured rather than free. If none do, absence is normal everywhere.

    This matters because the two error directions are not symmetric: excluding a
    healthy account costs one routing option, while including a blind one hands a
    caller an account that will wall out. Observed live -- an account with 20% Fable
    headroom reported 57% and passed a 50% eligibility gate, because the only source
    that could answer had been rate-limited.
    """
    resolved = normalize_model_class(model_class)
    if not resolved:
        return list(snapshots), []

    def sees(snapshot: AccountSnapshot) -> bool:
        return any(w.applies_to and resolved in w.applies_to for w in snapshot.windows)

    if not any(sees(s) for s in snapshots):
        return list(snapshots), []

    kept: list[AccountSnapshot] = []
    dropped: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if sees(snapshot):
            kept.append(snapshot)
            continue
        if unreadable_reason(snapshot) is not None:
            # An account nobody could read has no windows of any kind, so it trivially
            # fails ``sees`` -- but "no fable window while other accounts report one"
            # blames the data shape and sends the reader hunting for a parsing bug when
            # the truth was an expired token. Worse, consuming it here dropped it from
            # the pipeline entirely: it reached neither ranked nor excluded, and a dark
            # account is exactly the thing an operator most needs to see. Hand it on to
            # the eligibility pass, which reports the cause the provider recorded.
            kept.append(snapshot)
            continue
        dropped.append(
            {
                "account": snapshot.id,
                "reason": (
                    f"no {resolved} window in this snapshot while other accounts report "
                    f"one, so its {resolved} limit is unmeasured, not unconstrained "
                    f"(source {snapshot.source})"
                ),
            }
        )
    return kept, dropped


def _prepare(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    cwd: str | None,
    *,
    event: str,
    write_state: bool,
) -> Prepared:
    """Config -> oracle -> history -> state -> pileup -> filter -> decide."""
    config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    config = config.with_overrides(
        min_remaining=getattr(args, "min_remaining", None),
        no_sticky=bool(getattr(args, "no_sticky", False)),
        timeout_ms=getattr(args, "timeout_ms", None),
        only=_split_list(getattr(args, "only", None)),
        exclude=_split_list(getattr(args, "exclude", None)),
    )

    warnings: list[str] = list(config.warnings) + list(config.tier_override_warnings())
    degraded: list[dict[str, Any]] = []

    model = mc.resolve(
        getattr(args, "model", None),
        patterns=config.model_patterns,
        multipliers=config.model_multipliers,
    )
    if model.warning:
        warnings.append(model.warning)

    for banned in BANNED_EXEC_ENV:
        if env.get(banned):
            warnings.append(
                f"{banned} is set in this environment; the router never proxies and "
                f"`{_PROG} exec` unsets it for the child process"
            )

    snapshots = _fetch_snapshots(config, deps, env, now_s, warnings, degraded)

    for snapshot in snapshots:
        # An account the router could not read is degraded data, not merely an
        # ineligible candidate: the fleet is smaller than it looks and the operator has
        # something to fix. Reported here rather than at eligibility so it survives even
        # on the paths that never reach the decision layer, and so `status --json` --
        # which renders no exclusions at all -- still names it.
        unreadable = unreadable_reason(snapshot)
        if unreadable is not None:
            degraded.append({"account": snapshot.id, "reason": unreadable})
        age = snapshot.staleness_s(now_s)
        if age is not None and age > config.staleness.max_age_s:
            degraded.append(
                {
                    "account": snapshot.id,
                    "reason": f"usage reading is {explain_mod.format_duration(age)} old",
                }
            )

    # State is always *read* (a dry run still reports what stickiness and pileup would
    # have done); only the write below is suppressed by --dry-run.
    store = StateStore(env=env)
    state = store.load()
    warnings.extend(state.warnings)

    reserved: dict[str, float] = {}
    if config.pileup.enabled and not state.stateless:
        # Net each claim against usage that has already appeared on the bar. A
        # reservation exists only because the endpoint has not caught up; once it has,
        # counting both the estimate and the measurement charges the same call twice.
        observed_used = {
            snap.id: window.used_fraction
            for snap in snapshots
            for window in snap.windows
            if window.key == WINDOW_KEY_5H
        }
        raw = state.reserved_by_account(
            now_s,
            config.pileup.window_s,
            observed_used=observed_used,
            calls_per_window={
                snap.id: config.calls_per_window(snap.id) for snap in snapshots
            },
        )
        for account_id, weighted in raw.items():
            cost = weighted / config.calls_per_window(account_id)
            if cost > 0:
                reserved[account_id] = cost
        if reserved:
            warnings.append(
                "pileup: "
                + ", ".join(
                    f"{account} -{cost:.1%}" for account, cost in sorted(reserved.items())
                )
                + f" reserved by calls dispatched in the last "
                f"{explain_mod.format_duration(config.pileup.window_s)}"
            )

    # Pileup exists to stop N concurrent callers converging on one pool. When the
    # candidate set IS one pool -- pinned with --only, or everything else disabled --
    # there is nowhere else the router could have sent the work, so the subtraction
    # has no upside and only makes the account look unservable.
    #
    # Reported live: a batch pinned to one account at concurrency 3 accumulated 79
    # reservations inside the 60s window (0.5% each, from the UNCALIBRATED
    # calls_per_window default of 200) and drove it to -39.5% reserved while its real
    # window was ~12% used. Reservations are also released on a timer rather than on
    # completion, so sustained throughput inflates them without bound -- that is a
    # separate fault worth fixing, but it cannot bite at all when there is only one
    # candidate.
    spreadable = len([a for a in config.enabled_accounts()]) > 1
    adjusted = _apply_pileup(snapshots, reserved) if spreadable else tuple(snapshots)
    if not spreadable and reserved:
        warnings.append(
            "single candidate: pileup reservations not applied (nothing to spread to)"
        )
    # Before eligibility: an account that cannot SEE the requested class's limit must
    # not be scored as though that limit did not exist.
    adjusted_list, blind = exclude_accounts_blind_to_model_class(adjusted, model.model_class)
    adjusted = tuple(adjusted_list)
    for entry in blind:
        warnings.append(f"{entry['account']}: {entry['reason']}")
    partition = _partition_candidates(config, adjusted, model.model_class, now_s)

    prepared = Prepared(
        config=config,
        model=model,
        now_s=now_s,
        snapshots=adjusted,
        raw_snapshots=tuple(snapshots),
        candidates=partition.candidates,
        fallback_pool=partition.fallback_pool,
        state=state,
        reserved=reserved,
        warnings=tuple(warnings),
        degraded=tuple(degraded),
    )

    records_a_pick = bool(write_state and not getattr(args, "dry_run", False))

    fallback_flag: list[bool] = []
    decision = _decide(
        prepared, deps, warnings, degraded,
        record=records_a_pick, fallback_flag=fallback_flag,
    )
    prepared.decision = decision
    prepared.from_fallback = bool(fallback_flag)
    prepared.cli_excluded = partition.excluded
    prepared.warnings = tuple(warnings) + tuple(decision.warnings)
    prepared.degraded = tuple(degraded)

    records_a_pick = records_a_pick and bool(decision.chosen)

    if records_a_pick:
        result = store.record_pick(
            decision.chosen or "",
            now_s=now_s,
            cost=model.multiplier,
            scope=model.model_class or GLOBAL_SCOPE,
            window_s=config.pileup.window_s,
            max_records=config.pileup.max_records,
            sticky_ttl_s=config.hysteresis.max_dwell_s,
            record_sticky=config.hysteresis.enabled,
            record_reservation=config.pileup.enabled,
            # What the bar read when this claim was written, so the next invocation can
            # net off whatever has become visible since instead of double-charging it.
            observed_used=next(
                (
                    w.used_fraction
                    for snap in snapshots
                    if snap.id == decision.chosen
                    for w in snap.windows
                    if w.key == WINDOW_KEY_5H
                ),
                None,
            ),
            # ``or None`` means "leave what is on disk alone". An empty blob is what a
            # decision that never reached the selection layer leaves behind (no
            # candidates, engine missing), and writing that through would silently erase
            # the dwell counters that stop the router flapping.
            selection=(prepared.selection_state or None)
            if config.hysteresis.enabled
            else None,
        )
        prepared.warnings = prepared.warnings + result.warnings

    # Exactly one history record per invocation, written after the decision so the
    # snapshot and what it was used for land together. ``chosen`` is set only when this
    # invocation really spent something -- calibration divides recorded picks by observed
    # burn, so a dry run or a `status` call must not inflate the numerator.
    if snapshots:
        write = history_mod.append_snapshots(
            prepared.raw_snapshots,
            now_s=now_s,
            event=event,
            chosen=decision.chosen if records_a_pick else None,
            model_class=model.model_class,
            multiplier=model.multiplier if records_a_pick else None,
            cost=model.multiplier if records_a_pick else None,
            regime=decision.regime,
            degraded=bool(prepared.degraded) or decision.degraded,
            env=env,
        )
        prepared.warnings = prepared.warnings + write.warnings

    return prepared


# ======================================================================================
# Rendering
# ======================================================================================


def _ranked_entry(
    breakdown: ScoreBreakdown,
    snapshots: Mapping[str, AccountSnapshot],
    now_s: float,
    model_class: str | None = None,
    *,
    with_reason: bool = False,
) -> dict[str, Any]:
    """One row of the ``ranked``/``excluded`` arrays.

    ``ranked`` carries exactly the keys the TypeScript consumer maps onto a retry order.
    """
    snapshot = snapshots.get(breakdown.account_id)
    age = snapshot.staleness_s(now_s) if snapshot is not None else None
    remaining = breakdown.min_remaining
    if remaining is None and snapshot is not None:
        remaining = snapshot.min_remaining_fraction(model_class)
    entry: dict[str, Any] = {
        "account": breakdown.account_id,
        "score": breakdown.score,
        "min_slack": breakdown.min_slack,
        "binding_window": breakdown.binding_window,
        "remaining": remaining,
        "eligible": breakdown.eligible,
        "source": snapshot.source if snapshot is not None else "unknown",
        "age_s": age,
        "fits": breakdown.fits,
    }
    if with_reason:
        entry["reason"] = breakdown.reason
    return entry


def _effective_margin(prepared: Prepared) -> float | None:
    """The switch bar that actually applied, in ``min_slack`` units, for rendering.

    The selection layer's test is ``challenger > incumbent * ratio + abs``, so the bar a
    challenger had to clear depends on the incumbent's own slack. Rendering the raw
    ``switch_margin_abs`` instead would understate it, and rendering anything derived from
    the capacity-scaled score would misreport which test ran at all.
    """
    hysteresis = prepared.config.hysteresis
    if not hysteresis.enabled:
        return None
    decision = prepared.decision
    ranked = decision.ranked
    if not ranked:
        return None
    if decision.sticky_applied:
        incumbent = decision.chosen_breakdown or ranked[0]
    elif len(ranked) > 1:
        incumbent = ranked[1]
    else:
        return hysteresis.switch_margin_abs
    return incumbent.min_slack * (hysteresis.switch_margin_ratio - 1.0) + (
        hysteresis.switch_margin_abs
    )


def _decision_for_render(prepared: Prepared) -> Decision:
    """The decision, with the CLI's own policy exclusions folded back into ``excluded``.

    :func:`_partition_candidates` filters accounts out *before* the selection layer ever
    sees them (disabled, ``--only``/``--exclude``, unavailable, below ``--min-remaining``),
    so their :class:`ScoreBreakdown` rows land on :attr:`Prepared.cli_excluded` and never
    appear in :attr:`Decision.excluded`. :func:`_pick_payload` already concatenates the
    two; every other renderer has to as well, or an account that was dropped by policy
    simply vanishes from the output -- neither ranked nor excluded -- which is precisely
    the "why was claude_c not even considered?" question ``explain`` exists to answer.
    """
    decision = prepared.decision
    if not prepared.cli_excluded:
        return decision
    return replace(
        decision, excluded=tuple(decision.excluded) + prepared.cli_excluded
    )


def _exec_env(prepared: Prepared) -> dict[str, str]:
    """The environment overlay for the winner.

    Never contains a proxy variable: this is a config-directory pointer, which is the
    whole execution mechanism (spawn the vendor's own CLI, authenticated as that account).
    """
    chosen = prepared.decision.chosen
    if not chosen:
        return {}
    account = prepared.config.account(chosen)
    if account is None:
        return {}
    overlay = account.exec_env()
    return {
        key: value
        for key, value in overlay.items()
        if key.strip().upper() not in BANNED_EXEC_ENV
    }



def _meets_policy(prepared: Prepared) -> bool:
    """Did the chosen account satisfy every constraint the CALLER supplied?

    Distinct from ``fits`` on purpose. ``fits`` is about the account ("is there
    budget left?"); this is about the request ("is the answer one you asked for?").
    They diverge exactly when a policy floor excludes everyone: the router still
    returns its least-bad option, that option may be perfectly usable, and a caller
    who set a floor still needs to know the floor was not met.

    Reported by a batch harness that set ``--min-remaining 0.50``, got a winner with
    5% left and ``fits: true``, and had no way to distinguish that from a real answer.
    """
    decision = prepared.decision
    chosen = decision.chosen if decision is not None else None
    if not chosen:
        return False
    # NOT decision.degraded: that is also set when any input snapshot is stale, which
    # says nothing about whether the winner satisfies the caller's constraints. Only
    # the fallback path means "no candidate qualified".
    if prepared.from_fallback:
        return False
    return any(row.account_id == chosen and row.eligible for row in (decision.ranked or ()))


def _pick_payload(prepared: Prepared) -> dict[str, Any]:
    decision = prepared.decision
    snapshots = prepared.snapshot_map
    winner = decision.chosen_breakdown
    account = prepared.config.account(decision.chosen or "")

    return {
        "contract_version": QUOTA_ROUTER_CONTRACT_VERSION,
        "decision": {
            "account": decision.chosen,
            "provider": (
                account.provider
                if account is not None
                else (snapshots[decision.chosen].provider if decision.chosen in snapshots else None)
            ),
            "reason": decision.reason
            or explain_mod.explain_decision(
                decision, margin=_effective_margin(prepared)
            ),
            "score": winner.score if winner is not None else 0.0,
            "binding_window": winner.binding_window if winner is not None else None,
            "regime": decision.regime,
            # `fits` answers "can this account physically serve the call". A policy
            # floor is not exhaustion, and a consumer must still be able to tell the
            # two apart -- 20% remaining under a 99% floor is ineligible but usable.
            "fits": winner.fits if winner is not None else False,
            # `meets_policy` answers the DIFFERENT question a batch caller asks: did
            # the winner satisfy the constraints I supplied? A threshold that quietly
            # stops binding cannot be used as a stop signal, so this one is allowed to
            # say no while `fits` says yes.
            "meets_policy": _meets_policy(prepared),
            # Only meaningful when policy was NOT met; offering a retry time alongside
            # a usable answer would invite callers to wait for no reason.
            "available_at": (
                None
                if _meets_policy(prepared)
                else _available_at(prepared.snapshots, prepared.model.model_class)
            ),
            "sticky": decision.sticky_applied,
        },
        "exec": {"env": _exec_env(prepared)},
        "ranked": [
            _ranked_entry(row, snapshots, prepared.now_s, prepared.model.model_class)
            for row in decision.ranked
        ],
        "excluded": [
            _ranked_entry(
                row,
                snapshots,
                prepared.now_s,
                prepared.model.model_class,
                with_reason=True,
            )
            for row in tuple(decision.excluded) + prepared.cli_excluded
        ],
        "degraded": list(prepared.degraded),
        "warnings": list(dict.fromkeys(prepared.warnings)),
        "generated_at": _iso(prepared.now_s),
    }


def _degraded_payload(now_s: float, message: str) -> dict[str, Any]:
    """The last-resort payload: still valid, still contract-versioned, still exit 0."""
    return {
        "contract_version": QUOTA_ROUTER_CONTRACT_VERSION,
        "decision": {
            "account": None,
            "provider": None,
            "reason": message,
            "score": 0.0,
            "binding_window": None,
            "regime": None,
            "fits": False,
            "sticky": False,
        },
        "exec": {"env": {}},
        "ranked": [],
        "excluded": [],
        "degraded": [{"account": None, "reason": message}],
        "warnings": [message],
        "generated_at": _iso(now_s),
    }


# ======================================================================================
# Subcommands
# ======================================================================================


def _cmd_pick(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    try:
        prepared = _prepare(
            args, env, now_s, deps, cwd, event="pick", write_state=True
        )
        payload = _pick_payload(prepared)
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - pick must always emit valid JSON
        payload = _degraded_payload(now_s, f"router failure: {type(exc).__name__}: {exc}")
        _dump_json(payload, stdout)
        return EXIT_OK

    _dump_json(payload, stdout)
    if getattr(args, "explain", False):
        stderr.write(
            explain_mod.explain_verbose(
                _decision_for_render(prepared), margin=_effective_margin(prepared)
            )
            + "\n"
        )
    return EXIT_OK


def _cmd_explain(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    prepared = _prepare(args, env, now_s, deps, cwd, event="explain", write_state=False)
    decision = _decision_for_render(prepared)
    if getattr(args, "json", False):
        payload = decision.to_dict()
        payload["generated_at"] = _iso(now_s)
        payload["warnings"] = list(dict.fromkeys(prepared.warnings))
        _dump_json(payload, stdout)
        return EXIT_OK

    stdout.write(
        explain_mod.explain_verbose(decision, margin=_effective_margin(prepared)) + "\n"
    )
    return EXIT_OK


def _plans_for(config: Config) -> tuple[dict[str, pse.Plan], list[str]]:
    """Per-account calibrated denominators, for stamping ``k`` onto a waste row.

    Read through the scoring layer rather than off the config directly, so the value
    that lands in the measurement is the same one the router routes on -- including its
    refusal of a physically impossible ratio. Imported lazily because the pure decision
    layer is an optional dependency of the CLI (see :func:`_load_engine`).
    """
    plans: dict[str, pse.Plan] = {}
    warnings: list[str] = []
    try:
        from . import scoring  # type: ignore[attr-defined]
    except ImportError as exc:  # pragma: no cover - the pure layer ships with the CLI
        return plans, [f"cannot resolve per-account k: {exc}"]
    for account_id in config.accounts:
        try:
            plan = scoring.plan_for(config, account_id)
        except ValueError as exc:
            warnings.append(f"{account_id}: {exc}")
            continue
        if plan is not None:
            plans[account_id] = plan
    return plans, warnings


def _record_waste(config: Config, env: Mapping[str, str]) -> list[str]:
    """Append any window reset the retained history can attest to.

    Run from ``status`` because that is what the launchd poller invokes: it already
    reads every account on a schedule, so a reset is at most one poll interval stale
    when it is seen, and no new job has to exist. Deliberately NOT on ``pick`` -- the
    launcher caps its whole routing decision at three seconds, and scanning retained
    history there is exactly the work that turns a decision into a timeout.
    """
    plans, warnings = _plans_for(config)
    records = history_mod.iter_records(env=env, include_rotated=True)
    write = waste_mod.update_from_history(records, plans=plans, env=env)
    return warnings + list(write.warnings)


def _cmd_status(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    prepared = _prepare(args, env, now_s, deps, cwd, event="status", write_state=False)
    model_class = prepared.model.model_class
    prepared.warnings = prepared.warnings + tuple(_record_waste(prepared.config, env))

    if getattr(args, "json", False):
        payload = {
            "contract_version": QUOTA_ROUTER_CONTRACT_VERSION,
            "generated_at": _iso(now_s),
            "model_class": model_class,
            "accounts": [
                {
                    **snapshot.to_dict(),
                    "age_s": snapshot.staleness_s(now_s),
                    "min_slack": snapshot.min_slack(now_s, model_class),
                    "min_remaining": snapshot.min_remaining_fraction(model_class),
                    "binding_window": snapshot.binding_window_key(now_s, model_class),
                }
                for snapshot in prepared.snapshots
            ],
            "state": prepared.state.to_dict(),
            "config_sources": list(prepared.config.sources),
            "degraded": list(prepared.degraded),
            "warnings": list(dict.fromkeys(prepared.warnings)),
        }
        _dump_json(payload, stdout)
        return EXIT_OK

    if not prepared.snapshots:
        stdout.write("no accounts visible\n")
    for snapshot in prepared.snapshots:
        age = snapshot.staleness_s(now_s)
        stdout.write(
            f"{snapshot.id}  [{snapshot.tier}, capacity {snapshot.capacity:g}]  "
            f"source={snapshot.source}"
            + (f" age={explain_mod.format_duration(age)}" if age is not None else "")
            + ("" if snapshot.available else "  UNAVAILABLE")
            + "\n"
        )
        for row in snapshot.slacks(now_s, model_class):
            mark = "*" if row.binding else " "
            if not row.applicable:
                stdout.write(
                    f"  {mark} {explain_mod.window_label(row.key):<8} "
                    f"{row.reason or 'not applicable'}\n"
                )
                continue
            stdout.write(
                f"  {mark} {explain_mod.window_label(row.key):<8} "
                f"{explain_mod.explain_window(row)}\n"
            )
        if snapshot.note:
            stdout.write(f"    note: {snapshot.note}\n")

    for warning in dict.fromkeys(prepared.warnings):
        stderr.write(f"! {warning}\n")
    return EXIT_OK


def _cmd_exec(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    prepared = _prepare(args, env, now_s, deps, cwd, event="exec", write_state=True)
    decision = prepared.decision
    chosen = decision.chosen

    if not chosen:
        stderr.write(f"{_PROG}: no account could serve this call: {decision.reason}\n")
        return EXIT_ROUTER_FAILURE

    account = prepared.config.account(chosen)
    if account is None:
        stderr.write(
            f"{_PROG}: chose {chosen}, which has no configuration entry; cannot spawn it\n"
        )
        return EXIT_ROUTER_FAILURE
    # A config directory is how the claude and codex CLIs are pointed at one specific
    # account, so spawning them without one would spend from whichever account happens to
    # be ambient -- the exact mistake this router exists to prevent. Providers that select
    # an account by other means (Antigravity picks its pool from AGY_MODEL) are allowed
    # through on their environment overlay alone.
    if not account.config_dir and account.provider in _CONFIG_DIR_PROVIDERS:
        stderr.write(
            f"{_PROG}: {chosen} has no config_dir; execution requires the account's own "
            f"CLI directory, because that is what selects the account (this router never "
            f"proxies). Set [accounts.{chosen}] config_dir in your config.\n"
        )
        return EXIT_ROUTER_FAILURE

    forwarded = list(getattr(args, "command_args", None) or [])
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    if not forwarded or forwarded[0].startswith("-"):
        argv = [account.exec_command, *forwarded]
    else:
        argv = forwarded

    child_env = {
        key: value
        for key, value in env.items()
        if key.strip().upper() not in BANNED_EXEC_ENV
    }
    child_env.update(_exec_env(prepared))

    if getattr(args, "explain", False):
        stderr.write(
            explain_mod.explain_decision(
                decision, margin=_effective_margin(prepared)
            )
            + "\n"
        )

    if getattr(args, "dry_run", False):
        payload = {
            "contract_version": QUOTA_ROUTER_CONTRACT_VERSION,
            "account": chosen,
            "argv": argv,
            "env": _exec_env(prepared),
            "generated_at": _iso(now_s),
        }
        if getattr(args, "json", False):
            _dump_json(payload, stdout)
        else:
            overlay = " ".join(f"{k}={v}" for k, v in payload["env"].items())
            stdout.write(f"{overlay} {' '.join(argv)}\n".lstrip())
        return EXIT_OK

    try:
        completed = deps.run(argv, env=child_env)
    except FileNotFoundError:
        stderr.write(f"{_PROG}: cannot execute {argv[0]!r}: command not found\n")
        return EXIT_ROUTER_FAILURE
    except OSError as exc:
        stderr.write(f"{_PROG}: cannot execute {argv[0]!r}: {exc}\n")
        return EXIT_ROUTER_FAILURE

    returncode = getattr(completed, "returncode", 0)
    return int(returncode) if isinstance(returncode, int) else EXIT_ROUTER_FAILURE



def _unverifiable_against_floor_reason(min_remaining: float) -> str:
    """Why an account with no applicable window fails a CALLER-SET floor.

    Names the floor's provenance because that is the part a reader cannot otherwise
    see: the identical value as a built-in default does not reject this account. The
    built-in 0.02 is our own sanity guard; a floor you typed is a bar you expect to
    bind, and only the second one is a reason to refuse an account whose remaining
    quota is unknowable. Two setups that look the same in a config file diverge here,
    so the divergence is stated where it is acted on.
    """
    return (
        "no applicable usage window, so its remaining quota cannot be verified "
        f"against the min-remaining floor ({min_remaining:.1%}) that you set "
        "(the identical built-in default does not reject unmeasurable accounts)"
    )


def _calls_per_window_report(usable: Mapping[str, Any]) -> str:
    """Report the calls-per-window estimate WITHOUT telling anyone to adopt it.

    This used to print a paste-ready TOML stanza. The estimate does not deserve that,
    for a reason no amount of data fixes: the numerator counts only the picks the
    router made, while the denominator moves for every kind of consumption on that
    account, the operator's own interactive sessions included. The confound is
    structural, and it biases the estimate low -- which makes each pileup reservation
    larger, in the direction that has already caused harm. A batch produced 79
    reservations subtracting 39.5% of a window and self-throttled the router into
    refusing to route; halving calls_per_window would have doubled every one of them.

    The number stays, because knowing roughly how many calls fit in a window is
    genuinely useful. What is withheld is the instruction to act on it. A tool that
    recommends what its own authors consider harmful is worse than one that says
    nothing, because the recommendation is the part people trust.
    """
    lines = ["\n# calls per window, measured -- NOT recommended for adoption\n"]
    for account_id, calibration in usable.items():
        per_pick = 100.0 / calibration.calls_per_window
        lines.append(
            f"#   {account_id}: ~{calibration.calls_per_window:g} calls/window "
            f"-> each pick would reserve {per_pick:.1f}% of it "
            f"(built-in default reserves {100.0 / PileupConfig().calls_per_window:.1f}%)\n"
        )
    lines.append(
        "# Not emitted as config on purpose. The estimate counts only router picks in\n"
        "# its numerator while its denominator moves for ALL consumption of the account,\n"
        "# interactive sessions included, so it is confounded low -- and a low value makes\n"
        "# every concurrent pick reserve MORE. Set calls_per_window by hand only if you\n"
        "# have a reason the default is wrong for your workload.\n"
    )
    return "".join(lines)


def _adoption_verdict(
    *, k: float, low: float, high: float, median_step_s: float, max_gap_s: float
) -> str:
    """Say whether this estimate is adoptable, and if not, name the blocker.

    Extracted so the message can be tested against the decision it describes. It
    could previously contradict itself inside one sentence -- a width of 15.5% was
    rounded to "15%" for display and compared at full precision, so the reader was
    told the estimate met a bound it had just failed. A tool that argues with itself
    teaches you to distrust the parts that are right.

    Reporting the condition that actually failed matters as much: quoting width when
    density is the blocker sends the reader to collect more burn, which will never
    help -- the fix there is denser sampling, or waiting for sparse history to age out.
    """
    # The real interval is taken, never reconstructed from k and a width: round-
    # tripping through `k +/- width/2` reintroduces float error and can refuse an
    # estimate sitting exactly on the bound, which then prints as satisfying it.
    width = high - low
    if history_mod.adoption_ready(
        k=k, low=low, high=high, max_gap_s=max_gap_s, median_step_s=median_step_s
    ):
        return "ADOPT"
    if median_step_s > history_mod.ADOPTION_MAX_STEP_S:
        return (
            f"keep default (typical sample step {median_step_s / 60:.0f}m > "
            f"{history_mod.ADOPTION_MAX_STEP_S / 60:.0f}m; increments lost to unseen "
            f"resets bias k low)"
        )
    # One decimal, and rounded UP, so the printed number can never look like it
    # satisfies the bound that just rejected it.
    shown = math.ceil(1000.0 * width / k) / 10.0
    return (
        f"keep default (interval {shown:.1f}% wide, want "
        f"<={100 * history_mod.ADOPTION_RELATIVE_WIDTH:.0f}%)"
    )

def _cmd_calibrate(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    since_s = (now_s - float(args.days) * 86400.0) if args.days else None
    records = list(
        history_mod.iter_records(env=env, include_rotated=True, since_s=since_s)
    )
    results = history_mod.calibrate(records, window_key=args.window)
    # The same cutoff, applied again on the observation clock: the record filter above
    # can only see write time, and a republished reading is written now while
    # describing hours ago.
    k_results = history_mod.estimate_weekly_to_session(records, since_s=since_s)

    if getattr(args, "json", False):
        _dump_json(
            {
                "contract_version": QUOTA_ROUTER_CONTRACT_VERSION,
                "generated_at": _iso(now_s),
                "records": len(records),
                "window": args.window,
                "accounts": {key: value.to_dict() for key, value in results.items()},
                "weekly_to_session": {
                    key: {
                        "k": value.k,
                        "low": value.low,
                        "high": value.high,
                        "session_increment_pp": value.session_increment_pp,
                        "weekly_consumed_pp": value.weekly_consumed_pp,
                        "samples": value.samples,
                        "max_gap_s": value.max_gap_s,
                        "span_s": value.span_s,
                        "dropped_pairs": value.dropped_pairs,
                        "undersampled": value.undersampled,
                        "reason": value.reason,
                    }
                    for key, value in k_results.items()
                },
            },
            stdout,
        )
        return EXIT_OK

    stdout.write(f"{len(records)} history record(s), window {args.window!r}\n\n")
    if not results:
        stdout.write(
            "no usable history yet -- run some picks first; the estimate needs recorded\n"
            "picks *and* the usage deltas that followed them.\n"
        )
        return EXIT_OK

    for account_id, calibration in results.items():
        if calibration.calls_per_window is None:
            stdout.write(f"{account_id}: not enough evidence ({calibration.reason})\n")
            continue
        stdout.write(
            f"{account_id}: ~{calibration.calls_per_window:g} calls/window "
            f"({calibration.demand:g} weighted picks over {calibration.consumed:.1%} "
            f"of the window, {calibration.samples} samples)\n"
        )

    stdout.write(
        f"\nk (weekly:session capacity ratio) -- default {pse.DEFAULT_WEEKLY_TO_SESSION:g}, "
        f"hand-measured +/-2:\n"
    )
    for account_id, est in k_results.items():
        if not account_id.startswith("claude"):
            continue
        if est.k is None:
            stdout.write(f"  {account_id}: not usable yet -- {est.reason}\n")
            continue
        width = est.high - est.low
        span_h = est.span_s / 3600.0
        verdict = _adoption_verdict(
            k=est.k,
            low=est.low,
            high=est.high,
            median_step_s=est.median_step_s,
            max_gap_s=est.max_gap_s,
        )
        stdout.write(
            f"  {account_id}: k={est.k:.1f} [{est.low:.1f}-{est.high:.1f}] "
            f"from {est.session_increment_pp:.0f}pp session / {est.weekly_consumed_pp:.0f}pp "
            f"weekly over {est.samples} samples spanning {span_h:.0f}h -> {verdict}\n"
        )

    usable = {k: v for k, v in results.items() if v.calls_per_window is not None}
    if usable:
        stdout.write(_calls_per_window_report(usable))
    if config.warnings:
        for warning in config.warnings:
            stderr.write(f"! {warning}\n")
    return EXIT_OK


def _cmd_waste(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    """Report what expired unused, per account per window, in PSE.

    A reader, not a statistics package: the absolute number is the thing that was never
    measured, and a total plus an honest observed/missed split is what a publishing
    decision turns on.
    """
    config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    warnings: list[str] = []

    if getattr(args, "backfill", False):
        warnings.extend(_record_waste(config, env))

    path = waste_mod.waste_path(env)
    records, read_warnings = waste_mod.read_records(env=env)
    warnings.extend(read_warnings)
    summary = waste_mod.summarize(records)
    totals = summary["totals"]
    scoped = summary["scoped_totals"]

    if getattr(args, "json", False):
        _dump_json(
            {
                "contract_version": QUOTA_ROUTER_CONTRACT_VERSION,
                "generated_at": _iso(now_s),
                "path": str(path),
                "records": len(records),
                **summary,
                "warnings": list(dict.fromkeys(warnings)),
            },
            stdout,
        )
        return EXIT_OK

    if not records:
        stdout.write(
            f"no window reset has been recorded yet ({path}).\n"
            f"The series is written by `{_PROG} status`, which the poller already runs; "
            f"the first row appears at the first reset after it starts.\n"
        )
        for warning in dict.fromkeys(warnings):
            stderr.write(f"! {warning}\n")
        return EXIT_OK

    stdout.write(f"{len(records)} reset(s) recorded in {path}\n\n")
    stdout.write(f"{'account':<12}{'window':<10}{'resets':>7}{'observed':>10}"
                 f"{'missed':>8}{'wasted PSE':>12}{'worst gap':>11}\n")
    for account_id in sorted(summary["accounts"]):
        for key in sorted(summary["accounts"][account_id]):
            row = summary["accounts"][account_id][key]
            gap = row["max_observation_gap_s"]
            stdout.write(
                f"{account_id:<12}{key:<10}{row['resets']:>7}{row['observed']:>10}"
                f"{row['missed']:>8}{row['wasted_pse']:>12.2f}"
                f"{(explain_mod.format_duration(gap) if gap is not None else '-'):>11}\n"
            )

    stdout.write(
        f"\n{totals['wasted_pse']:.2f} PSE expired unused across {totals['resets']} "
        f"weekly reset(s) -- {totals['observed']} observed, {totals['missed']} missed.\n"
    )
    if totals["missed"]:
        stdout.write(
            "A missed reset has no remainder and is counted, never averaged over: its "
            "quota is gone and unmeasured, so the total above is a LOWER bound.\n"
        )
    if scoped["resets"]:
        stdout.write(
            f"Model-scoped sub-caps: {scoped['wasted_pse']:.2f} PSE across "
            f"{scoped['resets']} reset(s). Listed, not added -- that budget is a slice "
            f"of the same weekly pool and is already inside the total above.\n"
        )
    for warning in dict.fromkeys(warnings):
        stderr.write(f"! {warning}\n")
    return EXIT_OK


# ======================================================================================
# Argument parsing
# ======================================================================================


def _common_parser() -> argparse.ArgumentParser:
    """Flags shared by every subcommand."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--model",
        metavar="MODEL",
        help=(
            "model id or class name (e.g. 'claude-fable-5[1m]' or 'fable'). Model class "
            "gates routing: a window scoped to another class is skipped."
        ),
    )
    common.add_argument(
        "--only",
        action="append",
        metavar="IDS",
        help="restrict routing to these account ids (comma-separated, repeatable)",
    )
    common.add_argument(
        "--exclude",
        action="append",
        metavar="IDS",
        help="never route to these account ids (comma-separated, repeatable)",
    )
    common.add_argument(
        "--min-remaining",
        type=float,
        metavar="FRACTION",
        help="minimum remaining budget an account needs to be eligible (0..1)",
    )
    common.add_argument(
        "--no-sticky",
        action="store_true",
        help="ignore the sticky incumbent and hysteresis for this invocation",
    )
    common.add_argument(
        "--dry-run",
        action="store_true",
        help="decide, but write no state and spawn nothing",
    )
    common.add_argument(
        "--explain",
        action="store_true",
        help="write a human-readable explanation to stderr",
    )
    common.add_argument(
        "--now",
        metavar="TIME",
        help="evaluate as of this time (epoch seconds or ISO-8601) instead of now",
    )
    common.add_argument(
        "--config",
        metavar="PATH",
        help="additional config file, applied last (must exist)",
    )
    common.add_argument(
        "--timeout-ms",
        type=int,
        metavar="MS",
        help="oracle timeout in milliseconds",
    )
    common.add_argument(
        "--json",
        action="store_true",
        help="machine-readable output (pick is always JSON)",
    )
    return common


def _build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Pick which of your own already-authorized LLM accounts should serve a call, "
            "preferring quota that would otherwise expire unused."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    subparsers.add_parser(
        "pick",
        parents=[common],
        help="print the routing decision as JSON (always exits 0 unless flags are bad)",
    )
    exec_parser = subparsers.add_parser(
        "exec",
        parents=[common],
        help="pick, then run that account's own CLI; the child's exit code passes through",
    )
    exec_parser.add_argument(
        "command_args",
        nargs=argparse.REMAINDER,
        metavar="-- COMMAND ...",
        help="command to run (defaults to the winning provider's CLI)",
    )
    subparsers.add_parser(
        "status", parents=[common], help="show every account's windows and slack"
    )
    subparsers.add_parser(
        "explain", parents=[common], help="explain the decision in prose"
    )
    calibrate_parser = subparsers.add_parser(
        "calibrate",
        parents=[common],
        help="estimate calls_per_window per account from recorded history",
    )
    calibrate_parser.add_argument(
        "--window",
        default="five_hour",
        metavar="KEY",
        help="window key to measure (default: five_hour)",
    )
    calibrate_parser.add_argument(
        "--days",
        type=float,
        default=14.0,
        metavar="N",
        help="only use history from the last N days (default: 14; 0 = all)",
    )
    subparsers.add_parser(
        "forensics",
        parents=[common],
        help=(
            "sample every Claude credential once and record any state change "
            "(local only: Keychain + process table, no network)"
        ),
    )
    waste_parser = subparsers.add_parser(
        "waste",
        parents=[common],
        help="report quota that expired unused at each window reset",
    )
    waste_parser.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "before reporting, re-scan retained history for resets missing from the "
            "series (append-only; an already-recorded reset is never written twice)"
        ),
    )
    return parser


def _cmd_forensics(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    """Sample every Claude credential once and record any change of state.

    Deliberately does NOT go through ``_prepare``. That path fetches usage over
    the network, and this command must stay local, cheap, and safe to run every
    sixty seconds. It reads the Keychain and the process table, nothing else.
    """
    config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))

    accounts: list[tuple[str, str]] = []
    default_config_dir = os.path.expanduser("~/.claude")
    for account in config.accounts.values():
        if account.provider != PROVIDER_CLAUDE or not account.config_dir:
            continue
        accounts.append((account.id, account.config_dir))
        if account.is_default_config_dir:
            default_config_dir = str(Path(os.path.expanduser(account.config_dir)))

    if not accounts:
        print("no Claude accounts configured; nothing to sample", file=stderr)
        return EXIT_OK

    sample, transitions = forensics_mod.run_once(
        accounts,
        now_s=now_s,
        now_iso=_iso(now_s),
        default_config_dir=default_config_dir,
        state_path=forensics_mod.forensics_state_path(env),
        log_path=forensics_mod.forensics_log_path(env),
        runner=deps.run,
    )

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "contract_version": QUOTA_ROUTER_CONTRACT_VERSION,
                    "generated_at": _iso(now_s),
                    "sample": sample.to_dict(),
                    "transitions": [asdict(t) for t in transitions],
                }
            ),
            file=stdout,
        )
        return EXIT_OK

    for account_id, cred in sorted(sample.credentials.items()):
        held = len(sample.holders.get(cred.config_dir, ()))
        print(
            f"{account_id:<10} {cred.state:<10} {held} live session(s)  {cred.config_dir}",
            file=stdout,
        )
    for transition in transitions:
        marker = "LOST" if transition.lost else "changed"
        print(
            f"  {marker}: {transition.account_id} "
            f"{transition.previous_state} -> {transition.current_state} "
            f"within {transition.gap_s:.0f}s",
            file=stdout,
        )
    return EXIT_OK


_COMMANDS: Final[Mapping[str, Callable[..., int]]] = {
    "pick": _cmd_pick,
    "exec": _cmd_exec,
    "status": _cmd_status,
    "explain": _cmd_explain,
    "calibrate": _cmd_calibrate,
    "waste": _cmd_waste,
    "forensics": _cmd_forensics,
}


# ======================================================================================
# Entry point
# ======================================================================================


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    now_s: float | None = None,
    *,
    deps: Deps | None = None,
    cwd: str | None = None,
) -> int:
    """Run the CLI and return an exit code.

    Every input the process would normally take from the outside world is a parameter, so
    a test can call ``main(["pick"], env={...}, stdout=io.StringIO(), now_s=1_700_000_000)``
    and assert on parsed JSON without a subprocess, a real clock, or the operator's own
    machine state.
    """
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    environ = dict(os.environ if env is None else env)

    parser = _build_parser()
    # argparse writes to the real streams and raises SystemExit; both are redirected so
    # main() stays a plain function call.
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else EXIT_USAGE

    if not getattr(args, "command", None):
        with contextlib.redirect_stdout(out):
            parser.print_help()
        return EXIT_USAGE

    try:
        resolved_now = _parse_now(getattr(args, "now", None))
    except ConfigError as exc:
        err.write(f"{_PROG}: {exc}\n")
        return EXIT_USAGE

    # Precedence: --now beats an injected now_s beats the wall clock. This is the only
    # place the real clock is read; everything downstream takes now_s as a parameter.
    if resolved_now is not None:
        effective_now = resolved_now
    elif now_s is not None:
        effective_now = float(now_s)
    else:
        effective_now = time.time()

    handler = _COMMANDS[args.command]
    try:
        return handler(args, environ, float(effective_now), Deps.resolve(deps), out, err, cwd)
    except ConfigError as exc:
        err.write(f"{_PROG}: {exc}\n")
        return EXIT_USAGE
    except BrokenPipeError:  # pragma: no cover - `quotapick status | head`
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - a router bug must not look like a CLI crash
        err.write(f"{_PROG}: unexpected failure: {type(exc).__name__}: {exc}\n")
        return EXIT_ROUTER_FAILURE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
