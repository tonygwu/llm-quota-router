"""``quotapick`` -- the command line entry point.

Subcommands
-----------
``pick``
    Decide which account to spend from and print the decision as JSON on stdout.
``exec``
    ``pick``, then spawn that account's own vendor CLI. The child's exit code is passed
    through **unchanged**; only a router-level failure (nothing to spawn) uses 127.
``status``
    What quota is left on every account, one line each, with the tightest window
    stated as pace against an even burn. ``--verbose`` restores the per-window slack
    arithmetic and every note the history scan produced.
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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Final, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import explain as explain_mod
from . import forecast as forecast_mod
from . import forensics as forensics_mod
from . import history as history_mod
from types import SimpleNamespace

from . import doctor as doctor_mod
from . import plan_end as plan_end_mod
from . import reserve as reserve_mod
from . import model_classes as mc
from . import pse
from . import waste as waste_mod
from .config import BANNED_EXEC_ENV, Config, ConfigError, PileupConfig, load_config
from .state import GLOBAL_SCOPE, StateSnapshot, StateStore
from .types import (
    MODEL_CLASS_FABLE,
    PROVIDER_ANTIGRAVITY,
    PROVIDER_CLAUDE,
    PROVIDER_CURSOR,
    PROVIDERS,
    WINDOW_KEY_5H,
    WINDOW_KEY_7D,
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
    #: I/O for ``capabilities --refresh`` (``model/list``, ``agy models``). ``pick``
    #: never uses it; tests inject it so no process is spawned.
    capability_refresh: Any = None
    #: Reads a credit pool's key by Keychain item; ``run`` only. ``None`` means
    #: :func:`quota_router.secrets.read_api_key`.
    read_secret: Callable[[str], Any] | None = None

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
            # The config decides *which* adapters exist, not only how they are
            # configured: an opt-in adapter is constructed only when the operator's
            # config names it, and `_configure_adapters` below can only rebuild
            # adapters that are already in the set. Building the set with no config
            # dropped every opt-in adapter -- Antigravity's declared pools reached
            # `status` as "no adapter reported it" and could not be routed to.
            # Passed positionally by name rather than through `_supported_kwargs`:
            # a providers layer that cannot take the config must fail loudly here,
            # because silently dropping it is exactly this bug.
            adapters = _configure_adapters(
                providers,
                build(config=kwargs.get("config"), env=kwargs.get("env")),
                config=kwargs.get("config"),
                env=kwargs.get("env"),
                run=kwargs.get("run"),
                timeout_s=kwargs.get("timeout_s"),
                allow_slow_reads=bool(kwargs.get("allow_slow_reads")),
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
    allow_slow_reads: bool = False,
) -> tuple[Any, ...]:
    """Rebuild the providers layer's standard adapters with this run's settings.

    The providers layer owns *which* adapters exist and in what preference order; this
    layer owns the settings they should run with -- the injected subprocess runner, the
    environment, ``--timeout-ms``, and the operator's own ``[accounts.*] config_dir``
    entries so a non-standard directory is actually discovered.

    The timeout is applied **only** to the two live readers. The Claude one reads the
    Keychain and calls the vendor usage endpoint; the Codex one spawns ``codex
    app-server``. The others read local files. For the Codex reader the timeout can
    only shorten its own cap (``default_timeout_s``), never stretch it: the oracle
    default is 5s and ``cl``/``cdx`` give the whole pick 3s, and a slow live read has a
    transcript fallback to lose to. An adapter that cannot be rebuilt is kept exactly
    as the providers layer constructed it.

    The account list is passed as ``configs``, which is what both Claude adapters
    declare. An earlier version built a ``config_dirs`` mapping instead; no adapter
    declares that name, so :func:`_supported_kwargs` dropped it without a word and the
    adapters fell back to the builtin ``~/.claude*`` directories. An operator who
    declared an account beyond those builtins got it silently ignored.

    ``allow_slow_reads`` is true for ``status`` alone. It lets the Antigravity adapter
    spawn ``agy -p /usage``, which takes about 4s: more than the 3s ``cl`` and ``cdx``
    give a whole pick. Every other command serves that adapter's cached reading. The
    ``--timeout-ms`` oracle budget does not shorten that read: it bounds the pick path,
    and this read is never on it.
    """
    live_cls = getattr(providers, "ClaudeOAuthAdapter", None)
    cursor_cls = getattr(providers, "CursorAdapter", None)
    codex_cls = getattr(providers, "CodexSessionsAdapter", None)
    codex_live_cls = getattr(providers, "CodexAppServerAdapter", None)
    antigravity_cls = getattr(providers, "AntigravityAdapter", None)
    from_policy = getattr(providers, "claude_configs_from_policy", None)
    codex_from_policy = getattr(providers, "codex_accounts_from_policy", None)
    ids_for_provider = getattr(providers, "account_ids_for_provider", None)
    plans_for_provider = getattr(providers, "accounts_for_provider", None)

    claude_configs: tuple[Any, ...] | None = None
    if config is not None and callable(from_policy):
        claude_configs = tuple(from_policy(config, env=env)) or None

    # Same shape, same reason: a Codex account is a directory to be read, and the
    # adapter reads only the directories it is handed. Until 2026-09-14 nothing was
    # handed, so a second Codex account was listed by the config and never measured.
    codex_accounts: tuple[Any, ...] | None = None
    if config is not None and callable(codex_from_policy):
        codex_accounts = tuple(codex_from_policy(config, env=env)) or None

    cursor_accounts: tuple[str, ...] | None = None
    antigravity_accounts: tuple[str, ...] | None = None
    if config is not None and callable(ids_for_provider):
        cursor_accounts = ids_for_provider(config, PROVIDER_CURSOR) or None
        antigravity_accounts = ids_for_provider(config, PROVIDER_ANTIGRAVITY) or None

    # The Antigravity pools' launch plans: one /usage read per Google account, made the
    # way `exec` would reach that account.
    antigravity_plans: tuple[Any, ...] | None = None
    if config is not None and callable(plans_for_provider):
        antigravity_plans = tuple(plans_for_provider(config, PROVIDER_ANTIGRAVITY)) or None

    out: list[Any] = []

    for adapter in adapters:
        cls = type(adapter)
        settings: dict[str, Any] = {"env": env, "runner": run, "configs": claude_configs}
        if live_cls is not None and cls is live_cls and config is not None:
            settings["timeout_s"] = timeout_s
        # ``account_ids`` means a different population to each adapter that declares it:
        # operator accounts for Cursor, pools behind one binary for Antigravity. Each
        # therefore gets the list drawn for its own provider, and handing either the
        # other's list would invent accounts or pools that do not exist.
        if cursor_cls is not None and cls is cursor_cls:
            settings["account_ids"] = cursor_accounts
        elif antigravity_cls is not None and cls is antigravity_cls:
            settings["account_ids"] = antigravity_accounts
            settings["accounts"] = antigravity_plans
            settings["live"] = allow_slow_reads
        elif codex_cls is not None and cls is codex_cls:
            settings["codex_accounts"] = codex_accounts
        elif codex_live_cls is not None and cls is codex_live_cls:
            settings["codex_accounts"] = codex_accounts
            cap = getattr(cls, "default_timeout_s", None)
            if timeout_s is not None and isinstance(cap, (int, float)):
                settings["timeout_s"] = min(float(timeout_s), float(cap))

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


def _display_zone(env: Mapping[str, str]) -> tuple[tzinfo | None, str | None]:
    """The zone a human-readable table prints an absolute time in, and a note if any.

    ``TZ`` in the env wins; without it, ``None`` means this machine's own zone, which
    the C library reads from ``/etc/localtime`` (macOS sets no ``TZ``). A ``TZ`` that is
    not an IANA zone name prints UTC, and the note says so: never a guessed zone. JSON
    output is UTC whatever this returns, because scripts on other machines read it.
    """
    raw = (env.get("TZ") or "").strip().removeprefix(":")
    if not raw:
        return None, None
    try:
        return ZoneInfo(raw), None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return timezone.utc, f"TZ={raw!r} is not an IANA zone name, so times print in UTC"


def _wall_stamp(epoch_s: float, zone: tzinfo | None) -> str:
    """``Sun 2026-10-18 17:00 PDT``: one instant on the reader's clock.

    Each instant gets its own offset, so a stamp past a daylight-saving change is right.
    """
    moment = datetime.fromtimestamp(epoch_s, tz=timezone.utc).astimezone(zone)
    return moment.strftime("%a %Y-%m-%d %H:%M %Z")


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
    #: Every manual-use reserve applied to :attr:`snapshots`, for rendering. The reserve
    #: is already inside the snapshots; this is the arithmetic behind it.
    manual_reserves: tuple[reserve_mod.ManualReserve, ...] = ()
    warnings: tuple[str, ...] = ()
    degraded: tuple[dict[str, Any], ...] = ()
    #: Set only when the caller asked for a capability; ``None`` keeps every path and
    #: every byte of output exactly as it was before capabilities existed.
    capability: "CapabilityContext | None" = None
    #: The joint objective's context (:class:`quota_router.joint.JointContext`) when
    #: ``[engine]`` configures it or shadow mode, else ``None``.
    joint: Any = None
    #: What objective ranked this decision and why; ``None`` under today's default so
    #: no payload or history line changes.
    objective: dict[str, Any] | None = None
    #: The working rate the waste term credited each candidate provider with, and its
    #: provenance, as :func:`_working_rates` built it. Rendered as ``rate`` in the
    #: payload; only providers whose candidates carry a weekly window appear. Under the
    #: joint objective the block is informational: that scorer reads the forecast itself.
    rates: dict[str, dict[str, Any]] = field(default_factory=dict)

    def rates_by_provider(self) -> dict[str, float]:
        """The measured rates for the scorer; a provider left out scores at the default."""
        return {
            provider: float(report["measured_pse_per_h"])
            for provider, report in self.rates.items()
            if report.get("source") == "measured"
        }

    @property
    def snapshot_map(self) -> dict[str, AccountSnapshot]:
        mapping = {snapshot.id: snapshot for snapshot in self.snapshots}
        if self.capability is not None:
            for snapshot in self.capability.excluded_snapshots:
                mapping.setdefault(snapshot.id, snapshot)
        return mapping


def _unclaimed_account_warnings(
    config: Config, snapshots: Sequence[AccountSnapshot]
) -> list[str]:
    """Name every enabled account that no adapter reported on.

    The config layer accepts any account id under any known provider, but the adapters
    decide what they actually read. A Claude or Codex account is read from its config
    directory, so declaring one works once the directory holds something to read; the
    antigravity adapter reports on exactly the pools the config declares, so a second
    pool is reported too, and reaching a second Google account needs `macos_user` to
    name the macOS user whose Keychain holds that account's credential.

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
            f"but no adapter reported it, so it cannot be routed to. Look for a warning "
            f"above that names it; if there is none, the {account.provider} adapter does "
            f"not read declared accounts"
        )
    return out


def _fetch_snapshots(
    config: Config,
    deps: Deps,
    env: Mapping[str, str],
    now_s: float,
    warnings: list[str],
    degraded: list[dict[str, Any]],
    *,
    slow_reads: bool = False,
) -> tuple[AccountSnapshot, ...]:
    """Read ground truth, falling back to the history cache, then to nothing.

    Three tiers, each strictly worse than the last and each *reported*: a live oracle
    read; the most recent history record replayed as ``source="cache"``; or an empty set,
    which routes on config alone and says so.

    ``slow_reads`` lets a source that takes seconds read live (``status`` only); see
    :func:`_configure_adapters`.
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
            "allow_slow_reads": slow_reads,
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

    The cost goes in :attr:`Window.reserved_fraction`, never on top of ``used_fraction``.
    Both are netted off by ``remaining_fraction``, so routing is unchanged, but the
    vendor's reading survives for anyone reading the window. Writing it onto
    ``used_fraction`` forged that reading: on 2026-09-20 ``status`` published 57% used
    for an account the vendor reported at 56%, and 1% used for a bucket nothing had ever
    touched, with the reservation named only in a note the columns did not carry.
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
        # Capped at what the window still has after any manual-use hold: a window cannot
        # be more than fully accounted for, and the same cap already guards the hold.
        windows = tuple(
            replace(window, reserved_fraction=min(cost, window.remaining_fraction))
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
    *,
    capability_only: Collection[str] = (),
) -> Partition:
    """Split snapshots into routable candidates and CLI-level exclusions.

    Exclusions here are *policy* (disabled, ``--only``/``--exclude``, unavailable, below
    ``--min-remaining``, capability-only), not scoring. They are reported with reasons
    rather than dropped, because "why was claude_c not even considered?" is the second
    question every operator asks.

    Args:
        capability_only: Accounts this pick may not return, because they serve
            capability picks only. See :func:`_capability_only_ids`.
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
        elif snapshot.id in capability_only:
            reason = (
                'unmetered = "fallback": serves capability picks only, so a pick without '
                "--capability returns it only when --only names it"
            )
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


def _capability_only_ids(
    config: Config, capability: str | None, only: Sequence[str] | None
) -> frozenset[str]:
    """Accounts a pick without ``--capability`` may not return, unless ``--only`` names them.

    ``unmetered = "fallback"`` declares an account for capability picks only. Its
    callers launch work through the plan a capability pick hands back, model included.
    A plain pick's callers instead assume the provider they asked for: until the
    Antigravity ``/usage`` reader, a plain pick never returned one of these pools,
    because it had no windows, and an audit of the operator's repos on 2026-10-12 found
    four call sites that would mishandle one. A reading makes the pool compete inside
    capability picks (:func:`_fallback_only` steps aside) and leaves this rule alone.
    """
    if capability:
        return frozenset()
    named = set(only or ())
    return frozenset(
        account.id
        for account in config.enabled_accounts()
        if account.unmetered == "fallback" and account.id not in named
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


def _build_joint(
    config: Config,
    base_config: Config,
    fleet: Sequence[AccountSnapshot],
    candidates: Sequence[AccountSnapshot],
    state: StateSnapshot,
    now_s: float,
    env: Mapping[str, str],
    args: argparse.Namespace,
    model: mc.ModelResolution,
    capability_ctx: "CapabilityContext | None",
    warnings: list[str],
    *,
    objective_override: str | None = None,
    stream: str = "other",
) -> tuple[Any, dict[str, Any] | None]:
    """The joint objective's context, or why the decision falls back to ``own_waste``.

    Returns ``(None, None)`` under today's default, so nothing downstream changes. A
    missing, stale or thin forecast falls back for the WHOLE decision -- mixing two
    objectives in one ranking would compare unlike numbers -- and says why: a warning
    when ``joint`` was asked for, and the ``fallback`` field either way.
    """
    engine = config.engine
    wanted_objective = objective_override or engine.objective
    if wanted_objective == "own_waste" and not engine.shadow:
        return None, None
    job_pse = getattr(args, "job_pse", None) or engine.default_job_pse
    report: dict[str, Any] = {
        "configured": wanted_objective,
        "used": "own_waste",
        "fallback": None,
        "shadow": engine.shadow,
        "job_pse": job_pse,
        "forecast_age_s": None,
        "partial_fleet": False,
    }
    try:
        from . import joint as joint_mod  # type: ignore[attr-defined]
        from . import scoring  # type: ignore[attr-defined]
    except ImportError as exc:  # pragma: no cover - the pure layer ships with the CLI
        return _joint_fallback(report, f"decision layer unavailable: {exc}", warnings)

    cached, age, problem = _usable_forecast(engine, now_s, env)
    report["forecast_age_s"] = age
    if cached is None:
        return _joint_fallback(report, problem or "demand forecast unavailable", warnings)
    wanted = {
        joint_mod.GROUP_FOR_PROVIDER.get(snap.provider) for snap in candidates
    } - {None}
    for group in sorted(wanted):
        group_problem = _group_problem(engine, cached, group)
        if group_problem is not None:
            return _joint_fallback(report, group_problem, warnings)

    # The projection sees the whole configured fleet; `--only`/`--exclude` narrow the
    # read, so the projection is then partial for any group the narrowing cut.
    seen = {snap.id for snap in fleet}
    report["partial_fleet"] = any(
        account.id not in seen
        and joint_mod.GROUP_FOR_PROVIDER.get(account.provider) in wanted
        for account in base_config.enabled_accounts()
    )

    plan_ends = config.plan_ends()
    cooldowns = state.selection.get("exhausted_until") if isinstance(state.selection, Mapping) else None
    stocks = []
    for snap in fleet:
        if joint_mod.GROUP_FOR_PROVIDER.get(snap.provider) is None:
            continue
        try:
            plan = scoring.plan_for(config, snap.id)
        except ValueError as exc:
            warnings.append(f"{snap.id}: {exc}")
            continue
        until = cooldowns.get(snap.id) if isinstance(cooldowns, Mapping) else None
        stock = joint_mod.subscription_stock(
            snap,
            now_s=now_s,
            capacity=snap.capacity,
            plan=plan,
            refills=True,
            available_from_s=float(until) if isinstance(until, (int, float)) else None,
        )
        if stock is None:
            continue
        end = plan_ends.get(snap.id)
        if end is not None and end <= stock.deadline_s + 60.0:
            stock = replace(stock, refills=False)
        stocks.append(stock)

    streams = []
    for group, measured in sorted(cached.groups.items()):
        streams.append(
            joint_mod.Stream(group, joint_mod.STREAM_OTHER, measured.other.pse_per_h,
                             measured.other.fable_share)
        )
        streams.append(
            joint_mod.Stream(group, joint_mod.STREAM_RUN, measured.run.pse_per_h,
                             measured.run.fable_share)
        )
    if capability_ctx is not None:
        fable_ids = frozenset(
            account_id
            for account_id, cls in capability_ctx.classes.items()
            if cls == MODEL_CLASS_FABLE
        )
    elif normalize_model_class(model.model_class) == MODEL_CLASS_FABLE:
        fable_ids = frozenset(stock.id for stock in stocks)
    else:
        fable_ids = frozenset()
    context = joint_mod.JointContext(
        stocks=stocks,
        streams=streams,
        now_s=now_s,
        horizon_s=engine.horizon_s,
        step_s=engine.step_s,
        job_pse=job_pse,
        stream=stream,
        fable_ids=fable_ids,
        tie_fraction=engine.tie_fraction,
    )
    report["used"] = "joint" if wanted_objective == "joint" else "own_waste"
    return context, report


def _usable_forecast(
    engine: Any, now_s: float, env: Mapping[str, str]
) -> tuple[Any, float | None, str | None]:
    """The cached demand forecast, if it is fresh enough to act on.

    Returns ``(forecast, age_s, problem)``: the forecast and its age when usable; the
    age (when the file was readable) and why not otherwise. One gate for both readers
    -- the joint objective and the working rate -- so they can never disagree about
    which forecast is current.
    """
    cached, problem = forecast_mod.read_cache(env=env)
    if cached is None:
        return None, None, f"demand forecast unavailable: {problem}"
    age = now_s - cached.computed_at_s
    if age < 0 or age > engine.forecast_max_age_s:
        return None, age, (
            f"demand forecast stale (computed {_format_age(age)} ago, limit "
            f"{_format_age(engine.forecast_max_age_s)})"
        )
    return cached, age, None


def _group_problem(engine: Any, cached: Any, group: str) -> str | None:
    """Why ``group``'s measured demand cannot be trusted, or ``None`` when it can."""
    measured = cached.groups.get(group)
    if measured is None:
        return f"demand forecast has no {group} group"
    if measured.coverage < engine.forecast_min_coverage:
        return (
            f"demand forecast for {group} has coverage {measured.coverage:.0%}, "
            f"below {engine.forecast_min_coverage:.0%}"
        )
    return None


def _working_rates(
    config: Config,
    candidates: Sequence[AccountSnapshot],
    now_s: float,
    env: Mapping[str, str],
    warnings: list[str],
) -> dict[str, dict[str, Any]]:
    """The working rate to credit each candidate provider with, and where it came from.

    The waste term is ``weekly left - rate x hours to reset``, so the rate decides
    whether a long horizon can drain a big pool. The measured demand of the provider's
    group (``quotapick forecast``, both streams) is that rate. The hardcoded default is
    a guess for when nothing was measured, and every fall-through to it is reported,
    as ``reason`` here and as a warning: on the evening of 2026-10-17 PT the default
    left both ``claude`` (2.8 PSE, 41h to reset) and ``claude_e`` (1.5 PSE, 8h to reset)
    looking undrainable and ranked the bigger pile first, while the measured 0.16 PSE/h
    drains ``claude`` in time and loses 0.27 PSE of ``claude_e``.

    ``config`` is the fleet as configured, before ``--only``/``--exclude`` flip accounts
    off: those flags narrow the candidates, never the demand a fallback pool stands behind.

    Two rules, both stated in the entry's ``reason``:

    * Only providers whose candidates publish a weekly window appear. The rate is read
      by nothing else (``scoring.stocks_from_snapshot`` returns ``None`` without one),
      so a provider that cannot be normalized gets no entry and no warning.
    * A provider whose enabled accounts are all ``unmetered = "fallback"`` is credited
      with the demand it stands behind: the sum over the metered providers' groups,
      capped at the five-hour ceiling. Its own measured demand is an artifact of being
      last in line, not a forecast of what it could absorb. On this fleet the Antigravity
      pools measure 0.005 PSE/h; at that rate their placeholder-sized pools (BACKLOG #9)
      read as the biggest waste in the fleet and took every capability pick.
    """
    from . import joint as joint_mod  # type: ignore[attr-defined]
    from . import scoring  # type: ignore[attr-defined]

    engine = config.engine
    ceiling = scoring.MAX_SUSTAINED_RATE_PSE_PER_HOUR
    default = scoring.DEFAULT_RATE_PSE_PER_HOUR
    cached, age, problem = _usable_forecast(engine, now_s, env)
    normalizable = {
        snap.provider
        for snap in candidates
        if any(window.key == WINDOW_KEY_7D for window in snap.windows)
    }
    metered: set[str] = set()
    fallback: set[str] = set()
    for account in config.enabled_accounts():
        if getattr(account, "unmetered", None) == "fallback":
            fallback.add(account.provider)
        else:
            metered.add(account.provider)
    fallback_only = fallback - metered

    def measured_for(group: str) -> tuple[float | None, str | None]:
        if cached is None:
            return None, problem
        group_problem = _group_problem(engine, cached, group)
        if group_problem is not None:
            return None, group_problem
        streams = cached.groups[group]
        return streams.other.pse_per_h + streams.run.pse_per_h, None

    # The demand every fallback pool stands behind: each metered group, counted once.
    behind: dict[str, float] = {}
    for provider in sorted(metered):
        group = joint_mod.GROUP_FOR_PROVIDER.get(provider)
        if group is None or group in behind:
            continue
        measured, _why = measured_for(group)
        if measured is not None:
            behind[group] = measured

    rates: dict[str, dict[str, Any]] = {}
    for provider in sorted(normalizable):
        measured: float | None = None
        if provider in fallback_only:
            if behind:
                measured = sum(behind.values())
                reason: str | None = (
                    "fallback pool: credited with the demand it stands behind ("
                    + " + ".join(f"{group} {rate:.3f}" for group, rate in sorted(behind.items()))
                    + " PSE/h), not with its own, which reaches it only when nothing "
                    "measured fits"
                )
            else:
                reason = (
                    "fallback pool stands behind no measured demand "
                    f"({problem or 'no metered provider has a usable forecast group'})"
                )
        else:
            group = joint_mod.GROUP_FOR_PROVIDER.get(provider)
            if group is None:
                reason = f"no demand forecast group covers provider {provider}"
            else:
                measured, reason = measured_for(group)
        capped = measured is not None and measured > ceiling
        rates[provider] = {
            "pse_per_h": default if measured is None else min(measured, ceiling),
            "measured_pse_per_h": measured,
            "source": "default" if measured is None else "measured",
            "forecast_age_s": age,
            "capped": capped,
            "reason": reason,
        }
        if measured is None:
            warnings.append(
                f"{provider}: working rate defaulted to {default:.3f} PSE/h ({reason}); "
                f"the waste term credits each {provider} account with that guess, not "
                f"with measured demand"
            )
        elif capped and provider not in fallback_only:
            # A fallback pool is expected to sit at the ceiling: it is credited with
            # every metered group at once. Its entry still says ``capped``.
            warnings.append(
                f"{provider}: measured demand {measured:.3f} PSE/h exceeds the five-hour "
                f"ceiling of {ceiling:.3f} PSE/h; the ceiling is applied"
            )
        elif measured < default:
            # The forecast is a trailing average, so a week away reads as 0.000 with
            # full coverage. Applied as measured -- it is the truth about demand -- but
            # said, because at this rate the ranking is the bigger pile first again.
            warnings.append(
                f"{provider}: measured demand {measured:.3f} PSE/h over the forecast "
                f"window is below the {default:.3f} PSE/h default; at this rate every "
                f"{provider} pool reads as waste and the bigger pile ranks first"
            )
    return rates


def _joint_fallback(
    report: dict[str, Any], reason: str, warnings: list[str]
) -> tuple[Any, dict[str, Any]]:
    report["fallback"] = reason
    if report["configured"] == "joint":
        warnings.append(f"joint objective unavailable: {reason}; ranked by own_waste")
    return None, report


def _format_age(seconds: float) -> str:
    seconds = abs(seconds)
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}h{rest // 60:02d}m"


def _shadow_choice(prepared: Prepared, deps: Deps) -> str | None:
    """Who the joint objective would have picked among the same eligible accounts."""
    if prepared.joint is None or deps.rank is None:
        return None
    eligible = {row.account_id for row in prepared.decision.ranked}
    field_ = [snap for snap in prepared.candidates if snap.id in eligible]
    if not field_:
        return None
    try:
        rows = deps.rank(
            field_,
            prepared.now_s,
            prepared.config.engine_cfg(),
            prepared.model.model_class,
            joint=prepared.joint,
        )
    except Exception:  # noqa: BLE001 - shadow scoring must never touch the real decision
        return None
    return rows[0].account_id if rows else None


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

    cfg = config.engine_cfg(sticky=sticky)
    # One working rate per provider, measured by _working_rates from the demand
    # forecast. A provider missing here scores at the documented default, and
    # ``prepared.rates`` says which ones those were and why.
    cfg["rate_pse_per_hour_by_provider"] = prepared.rates_by_provider()
    shared: dict[str, Any] = {
        "now_s": now_s,
        "cfg": cfg,
        "model_class": model_class,
        "state": selection_state if sticky else None,
        "record": record,
    }
    if prepared.objective is not None and prepared.objective.get("used") == "joint":
        shared["joint"] = prepared.joint

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


@dataclass
class CapabilityContext:
    """What a ``--capability`` request resolved, per account."""

    name: str
    table_path: str
    table_error: str | None
    choices: dict[str, Any] = field(default_factory=dict)
    classes: dict[str, str | None] = field(default_factory=dict)
    excluded: list[ScoreBreakdown] = field(default_factory=list)
    excluded_snapshots: list[AccountSnapshot] = field(default_factory=list)
    pinned: str | None = None
    effort: str | None = None


def _capability_view(snapshot: AccountSnapshot, model_class: str | None, model_id: str) -> AccountSnapshot:
    """The snapshot as it constrains a call for THIS account's model.

    Windows scoped to another class are dropped, and the ones that apply become
    account-wide, so the unchanged scoring layer can judge each account for its own
    model under one shared label. A window applies when it is account-wide, or scoped
    to the model's class, or to the concrete id (Codex scopes carry slug aliases). An
    id with no known class keeps every window: guessing must never drop a limit.
    """
    wanted = {
        text
        for text in (normalize_model_class(model_class), model_id.strip().casefold())
        if text
    }
    if normalize_model_class(model_class) is None:
        kept = tuple(replace(w, applies_to=None) for w in snapshot.windows)
    else:
        kept = tuple(
            replace(w, applies_to=None)
            for w in snapshot.windows
            if w.applies_to is None
            or wanted & {str(a).strip().casefold() for a in w.applies_to}
        )
    return replace(snapshot, windows=kept)


def _apply_capability(
    config: Config,
    snapshots: Sequence[AccountSnapshot],
    capability: str,
    pinned: str | None,
    env: Mapping[str, str],
    now_s: float,
    warnings: list[str],
    effort: str | None = None,
) -> tuple[tuple[AccountSnapshot, ...], CapabilityContext]:
    """Resolve each account's model from the table alone, then build its view.

    Reads ONE file. Never reads a model list, never spawns: digital-twin kills pick at
    3000 ms and a timeout silently drops its routing.

    With ``effort``, each model is translated to the caller's level for its account. A
    clamp is reported, never hidden, and does not change the ranking; a model with no
    known levels is excluded with the reason.
    """
    from . import capability as cap_mod

    path = cap_mod.table_path(env)
    table, table_error = cap_mod.read_table(path)
    if table_error:
        warnings.append(table_error)
    ctx = CapabilityContext(
        name=capability, table_path=str(path), table_error=table_error, pinned=pinned,
        effort=effort,
    )
    pin_provider = None
    if pinned:
        pin_class = mc.classify(pinned, config.model_patterns)
        pin_provider = cap_mod.PROVIDER_FOR_CLASS.get(pin_class or "")

    def exclude(snapshot: AccountSnapshot, why: str) -> None:
        ctx.excluded.append(
            _breakdown_for(
                snapshot, None, now_s=now_s, eligible=False,
                reason=f"capability {capability}: {why}",
            )
        )
        ctx.excluded_snapshots.append(snapshot)

    kept: list[AccountSnapshot] = []
    for snapshot in snapshots:
        account = config.account(snapshot.id)
        if account is not None and not account.enabled:
            kept.append(snapshot)  # policy exclusion; _partition_candidates says why
            continue
        provider = account.provider if account is not None else snapshot.provider
        if provider not in cap_mod.CAPABILITY_PROVIDERS:
            exclude(snapshot, f"provider {provider} publishes no model list")
            continue
        if pinned:
            if pin_provider is None:
                exclude(snapshot, f"cannot tell which provider serves pinned model {pinned!r}")
                continue
            if provider != pin_provider:
                exclude(snapshot, f"pinned to {pinned}, a {pin_provider} model")
                continue
            listing = cap_mod.fresh_listing(table, snapshot.id, now_s=now_s, path=path)
            if isinstance(listing, cap_mod.Unresolved):
                exclude(snapshot, listing.reason)
                continue
            if not cap_mod.listing_offers(listing, pinned):
                exclude(snapshot, f"{snapshot.id} does not list {pinned}")
                continue
            choice: Any = cap_mod.ModelChoice(
                id=pinned, family=pin_class, provider=provider, source="pinned",
                source_fetched_at_s=listing.fetched_at_s, listed=True,
                effort_ids=cap_mod.effort_ids(listing, pinned),
            )
        else:
            choice = cap_mod.lookup(table, snapshot.id, capability, now_s=now_s, path=path)
            if isinstance(choice, cap_mod.Unresolved):
                exclude(snapshot, choice.reason)
                continue
        if effort:
            choice = cap_mod.apply_effort(choice, effort)
            if isinstance(choice, cap_mod.Unresolved):
                exclude(snapshot, f"effort {effort}: {choice.reason}")
                continue
        warnings.extend(cap_mod.pick_notes(table, snapshot.id, capability, now_s=now_s))
        ctx.choices[snapshot.id] = choice
        ctx.classes[snapshot.id] = mc.classify(choice.id, config.model_patterns)
        kept.append(snapshot)

    # The blind-account guard, per class: an account is compared only with the accounts
    # that would request the same class. Fleet-wide, a frontier request would drop both
    # Codex accounts for lacking the Fable window that Claude reports.
    by_class: dict[str | None, list[AccountSnapshot]] = {}
    for snapshot in kept:
        if snapshot.id in ctx.choices:
            by_class.setdefault(ctx.classes[snapshot.id], []).append(snapshot)
    blind_ids: set[str] = set()
    for model_class, group in by_class.items():
        _visible, blind = exclude_accounts_blind_to_model_class(group, model_class)
        for entry in blind:
            blind_ids.add(entry["account"])
            warnings.append(f"{entry['account']}: {entry['reason']}")
    views: list[AccountSnapshot] = []
    for snapshot in kept:
        if snapshot.id in blind_ids:
            exclude(snapshot, "cannot see a limit its model is subject to")
            ctx.choices.pop(snapshot.id, None)
            continue
        if snapshot.id in ctx.choices:
            views.append(
                _capability_view(snapshot, ctx.classes[snapshot.id], ctx.choices[snapshot.id].id)
            )
        else:
            views.append(snapshot)
    clamped = sorted(
        (account_id, choice.effort["applied"])
        for account_id, choice in ctx.choices.items()
        if choice.effort is not None and choice.effort["clamped"]
    )
    if clamped:
        warnings.append(
            f"effort {effort} clamped on {len(clamped)} account"
            f"{'' if len(clamped) == 1 else 's'}: "
            + ", ".join(f"{account_id} -> {applied}" for account_id, applied in clamped)
        )
    return tuple(views), ctx


#: Cooldown for an exhaustion message that names no reset time. Long enough that a
#: batch does not hammer a spent pool; short enough that a wrong guess costs one hour.
UNKNOWN_DEADLINE_COOLDOWN_S: Final[float] = 3600.0


def _fallback_only(prepared: Prepared, ctx: CapabilityContext, warnings: list[str]) -> Decision:
    """Let an ``unmetered = "fallback"`` account serve, only when nothing measured can.

    This governs such an account only while its snapshot has no windows. Its quota is
    then invisible, so it never competes with a measured account: it is eligible only
    when no measured account fits and ``report-failure`` has not put it in cooldown.
    Once its source reports windows (an Antigravity pool read through agy's ``/usage``
    since 2026-10-12), it is scored like any other account and is not touched here.
    """
    config = prepared.config
    decision = prepared.decision
    now_s = prepared.now_s
    snapshots = prepared.snapshot_map
    # Only accounts that survived the policy checks: a caller floor (an unmeasured
    # figure can never satisfy one), --exclude, an account its source marks
    # unavailable. _partition_candidates has already said why the others are out.
    candidates = {snapshot.id for snapshot in prepared.candidates}
    ids = [
        account.id
        for account in config.enabled_accounts()
        if account.unmetered == "fallback"
        and account.id in ctx.choices
        and account.id in candidates
        and not snapshots[account.id].windows
    ]
    if not ids:
        return decision

    winner = decision.chosen_breakdown
    measured_fits = (
        bool(decision.chosen)
        and not prepared.from_fallback
        and winner is not None
        and winner.fits
        and decision.chosen not in ids
    )
    cooldowns = (prepared.state.selection or {}).get("exhausted_until") or {}
    usable: list[str] = []
    blocked: dict[str, str] = {}
    for account_id in ids:
        try:
            deadline = float(cooldowns.get(account_id))
        except (TypeError, ValueError):
            deadline = None
        if deadline is not None and deadline > now_s:
            blocked[account_id] = (
                f"fallback-only, cooling off until {_iso(deadline)} (from report-failure)"
            )
        elif measured_fits:
            blocked[account_id] = (
                f"fallback-only: used only when no measured account can serve {ctx.name}"
            )
        else:
            usable.append(account_id)

    excluded = tuple(
        replace(row, reason=blocked[row.account_id]) if row.account_id in blocked else row
        for row in decision.excluded
        if row.account_id not in usable
    )
    if not usable:
        return replace(decision, excluded=excluded)

    reason = (
        f"fallback-only: no measured account can serve {ctx.name}; quota unmeasured"
    )
    rows = tuple(
        ScoreBreakdown(account_id=account_id, score=0.0, min_slack=0.0, fits=True,
                       eligible=True, reason=reason, min_remaining=None)
        for account_id in usable
    )
    rest = tuple(row for row in decision.ranked if row.account_id not in usable)
    warnings.append(
        f"{usable[0]} chosen as fallback-only for {ctx.name}: no measured account can "
        f"serve it, and its own quota is unmeasured"
    )
    prepared.from_fallback = False
    return replace(
        decision,
        chosen=usable[0],
        ranked=rows + rest,
        excluded=excluded,
        reason=f"{usable[0]} wins as {reason}",
        sticky_applied=False,
    )


def _prepare(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    cwd: str | None,
    *,
    event: str,
    write_state: bool,
    objective: str | None = None,
    job_stream: str = "other",
) -> Prepared:
    """Config -> oracle -> history -> state -> pileup -> filter -> decide.

    ``objective`` overrides ``[engine] objective`` for this call (``run`` ranks by the
    joint projection whatever ``pick`` defaults to); ``job_stream`` says which demand
    stream the job belongs to.
    """
    config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    base_config = config
    config = config.with_overrides(
        min_remaining=getattr(args, "min_remaining", None),
        no_sticky=bool(getattr(args, "no_sticky", False)),
        timeout_ms=getattr(args, "timeout_ms", None),
        only=_split_list(getattr(args, "only", None)),
        exclude=_split_list(getattr(args, "exclude", None)),
    )

    warnings: list[str] = list(config.warnings) + list(config.tier_override_warnings())
    degraded: list[dict[str, Any]] = []

    capability = getattr(args, "capability", None)
    if capability:
        # Each account is judged for its own model (see _apply_capability), so the
        # pipeline runs under one label. The label is also the sticky scope, so
        # capability picks keep their own incumbent.
        model = mc.ModelResolution(
            raw=getattr(args, "model", None),
            model_class=f"capability:{capability}",
            source="capability",
            multiplier=mc.DEFAULT_MULTIPLIER,
        )
    else:
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

    # Only `status` may wait on a slow live read: the launchd poller runs it, nobody is
    # waiting, and it keeps the cache that every pick then reads.
    snapshots = _fetch_snapshots(
        config, deps, env, now_s, warnings, degraded, slow_reads=event == "status"
    )

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

    # The manual-use reserve, applied once, here, so eligibility, scoring and `status`
    # all read the same spendable figure. `raw_snapshots` stays exactly as read: history
    # records it, and a reserve written into history would read as usage.
    rates = config.manual_rates()
    # A plan that is ending moves its windows' deadlines first, so the reserve below
    # counts days to the deadline routing will actually see. Like the reserve, it never
    # reaches history: the waste series reads recorded deadlines as the vendor's.
    ending = plan_end_mod.apply_plan_ends(snapshots, config.plan_ends(), now_s)
    held_snapshots, manual_reserves = reserve_mod.apply_manual_reserve(ending, rates, now_s)
    for account_id in reserve_mod.accounts_without_weekly_window(snapshots, rates):
        warnings.append(
            f"{account_id}: manual_rate_per_day is set but the account reports no 7-day "
            f"window, so no reserve is held"
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
    adjusted = (
        _apply_pileup(held_snapshots, reserved) if spreadable else tuple(held_snapshots)
    )
    if not spreadable and reserved:
        warnings.append(
            "single candidate: pileup reservations not applied (nothing to spread to)"
        )
    # The joint projection needs the fleet as routing sees it -- reserves, plan ends and
    # pileup applied -- but before capability views drop Fable windows it must keep.
    fleet = tuple(adjusted)
    capability_ctx: CapabilityContext | None = None
    if capability:
        adjusted, capability_ctx = _apply_capability(
            config, adjusted, capability, getattr(args, "model", None), env, now_s, warnings,
            effort=getattr(args, "effort", None),
        )
    # Before eligibility: an account that cannot SEE the requested class's limit must
    # not be scored as though that limit did not exist.
    adjusted_list, blind = exclude_accounts_blind_to_model_class(adjusted, model.model_class)
    adjusted = tuple(adjusted_list)
    for entry in blind:
        warnings.append(f"{entry['account']}: {entry['reason']}")
    partition = _partition_candidates(
        config,
        adjusted,
        model.model_class,
        now_s,
        capability_only=_capability_only_ids(
            config, capability, _split_list(getattr(args, "only", None))
        ),
    )

    joint_ctx, objective_report = _build_joint(
        config,
        base_config,
        fleet,
        partition.candidates,
        state,
        now_s,
        env,
        args,
        model,
        capability_ctx,
        warnings,
        objective_override=objective,
        stream=job_stream,
    )
    # The fleet BEFORE --only/--exclude: the demand a fallback pool stands behind is
    # a property of the fleet, not of this pick's candidate set. `status` skips it: it
    # surfaces no waste-term result, and it is the command that refreshes the forecast
    # (below, after this returns), so its warning would tell the operator to run the
    # command they are running.
    rates = (
        {}
        if event == "status"
        else _working_rates(base_config, partition.candidates, now_s, env, warnings)
    )

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
        manual_reserves=manual_reserves,
        warnings=tuple(warnings),
        degraded=tuple(degraded),
        capability=capability_ctx,
        joint=joint_ctx,
        objective=objective_report,
        rates=rates,
    )

    records_a_pick = bool(write_state and not getattr(args, "dry_run", False))

    fallback_flag: list[bool] = []
    decision = _decide(
        prepared, deps, warnings, degraded,
        record=records_a_pick, fallback_flag=fallback_flag,
    )
    prepared.decision = decision
    prepared.from_fallback = bool(fallback_flag)
    if objective_report is not None:
        objective_report["shadow_chosen"] = _shadow_choice(prepared, deps) if objective_report.get(
            "shadow"
        ) else None
        if prepared.from_fallback:
            # The exhausted fallback orders by earliest reset; no objective ranked it.
            objective_report["used"] = None
    if capability_ctx is not None:
        decision = _fallback_only(prepared, capability_ctx, warnings)
        prepared.decision = decision
    prepared.cli_excluded = partition.excluded + (
        tuple(capability_ctx.excluded) if capability_ctx is not None else ()
    )
    prepared.warnings = tuple(warnings) + tuple(decision.warnings)
    prepared.degraded = tuple(degraded)

    records_a_pick = records_a_pick and bool(decision.chosen)

    # What this call will actually spend: under a capability, the chosen account's own
    # model, not the shared label the pipeline ran under.
    spent_class, spent_multiplier = model.model_class, model.multiplier
    if capability_ctx is not None:
        spent_class = capability_ctx.classes.get(decision.chosen or "")
        spent_multiplier = mc.demand_multiplier(spent_class, config.model_multipliers)

    if records_a_pick:
        result = store.record_pick(
            decision.chosen or "",
            now_s=now_s,
            cost=spent_multiplier,
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
            model_class=spent_class,
            multiplier=spent_multiplier if records_a_pick else None,
            cost=spent_multiplier if records_a_pick else None,
            regime=decision.regime,
            degraded=bool(prepared.degraded) or decision.degraded,
            objective=prepared.objective,
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


def _launch_plan(account: Any) -> dict[str, Any]:
    """The full launch recipe for ONE account, as plain JSON types.

    Three fields, and a consumer that reads only the first is the bug this exists to
    stop. ``env`` is an overlay to merge. ``argv_prefix`` goes BEFORE the binary and
    is how an account selected by a macOS user is reached at all. ``unset_env`` is a
    deletion, which an overlay cannot express, so it travels separately or not at all.

    Emitted even when it is empty, which is the common case. A consumer that must
    branch on presence writes a different code path for the ordinary account and the
    unusual one, and only the ordinary one gets exercised.
    """
    if account is None:
        return {"env": {}, "argv_prefix": [], "unset_env": []}
    overlay = {
        key: value
        for key, value in account.exec_env().items()
        if key.strip().upper() not in BANNED_EXEC_ENV
    }
    return {
        "env": overlay,
        "argv_prefix": list(account.launch_argv_prefix),
        "unset_env": list(account.launch_unset_env),
    }


def _exec_block(prepared: Prepared) -> dict[str, Any]:
    """The ``exec`` object in the wire payload: the winner's launch recipe."""
    chosen = prepared.decision.chosen
    if not chosen:
        return _launch_plan(None)
    return _launch_plan(prepared.config.account(chosen))


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
    payload = _pick_payload_v1(prepared)
    if prepared.objective is not None:
        # Additive, and only when [engine] configures the joint objective or shadow
        # mode: today's payload is byte-identical (tests/test_contract_golden.py).
        payload["objective"] = dict(prepared.objective)
    # Additive: the working rate the waste term credited each provider with, and where
    # it came from. Always present -- the fall-through to the default is the thing a
    # caller most needs to see, so it is not hidden behind a flag.
    payload["rate"] = {provider: dict(report) for provider, report in prepared.rates.items()}
    ctx = prepared.capability
    if ctx is None:
        return payload
    # Additive, and only here: a caller that never passes --capability never sees
    # these keys (tests/test_contract_golden.py pins that).

    def model_of(account_id: Any) -> dict[str, Any] | None:
        choice = ctx.choices.get(account_id or "")
        return choice.to_dict() if choice is not None else None

    payload["capability"] = {
        "requested": ctx.name,
        "pinned": ctx.pinned,
        "table_path": ctx.table_path,
        "table_error": ctx.table_error,
    }
    payload["decision"]["model"] = model_of(payload["decision"]["account"])
    for row in payload["ranked"]:
        row["model"] = model_of(row["account"])
    for row in payload["excluded"]:
        row["model"] = model_of(row["account"])
    return payload


def _pick_payload_v1(prepared: Prepared) -> dict[str, Any]:
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
                decision,
                margin=_effective_margin(prepared),
                deprioritized=prepared.config.deprioritized_accounts(),
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
        "exec": _exec_block(prepared),
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
        "exec": {"env": {}, "argv_prefix": [], "unset_env": []},
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
    if getattr(args, "effort", None) and not getattr(args, "capability", None):
        stderr.write(f"{_PROG}: --effort needs --capability\n")
        return EXIT_USAGE
    try:
        prepared = _prepare(
            args, env, now_s, deps, cwd, event="pick", write_state=True
        )
        payload = _pick_payload(prepared)
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - pick must always emit valid JSON
        payload = _degraded_payload(now_s, f"router failure: {type(exc).__name__}: {exc}")
        if getattr(args, "capability", None):
            payload["capability"] = {"requested": args.capability}
            payload["decision"]["model"] = None
        _dump_json(payload, stdout)
        return EXIT_OK

    _dump_json(payload, stdout)
    if getattr(args, "explain", False):
        stderr.write(
            explain_mod.explain_verbose(
                _decision_for_render(prepared),
                margin=_effective_margin(prepared),
                deprioritized=prepared.config.deprioritized_accounts(),
                rates=prepared.rates,
            )
            + "\n"
            + _reserve_block(prepared)
            + _deprioritized_block(prepared)
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
        if prepared.objective is not None:
            payload["projection"] = _projection_payload(prepared)
        payload["rate"] = {
            provider: dict(report) for provider, report in prepared.rates.items()
        }
        _dump_json(payload, stdout)
        return EXIT_OK

    stdout.write(
        explain_mod.explain_verbose(
            decision,
            margin=_effective_margin(prepared),
            deprioritized=prepared.config.deprioritized_accounts(),
            objective=(prepared.objective or {}).get("used"),
            rates=prepared.rates,
        )
        + "\n"
        + _reserve_block(prepared)
        + _deprioritized_block(prepared)
        + _projection_block(prepared)
    )
    return EXIT_OK


def _projection_payload(prepared: Prepared) -> dict[str, Any]:
    """The joint projection behind a decision: leftovers, unmet demand, avoided waste."""
    payload: dict[str, Any] = {"objective": dict(prepared.objective or {})}
    ctx = prepared.joint
    if ctx is None:
        return payload
    now_s = prepared.now_s
    payload["streams"] = [
        {"group": s.group, "name": s.name, "pse_per_h": s.pse_per_h,
         "fable_share": s.fable_share}
        for s in ctx.streams
    ]
    settled: list[dict[str, Any]] = []
    unmet: list[dict[str, Any]] = []
    for group in sorted({stock.group for stock in ctx.stocks}):
        base = ctx.baseline(group)
        settled.extend(
            {"id": row.id, "kind": row.kind, "deadline": _iso(row.deadline_s),
             "hours": (row.deadline_s - now_s) / 3600.0, "leftover_pse": row.leftover}
            for row in base.settled
        )
        unmet.extend(
            {"group": key[0], "stream": key[1], "pse": amount}
            for key, amount in sorted(base.unmet.items())
        )
    payload["settled"] = settled
    payload["unmet"] = unmet
    avoided: list[dict[str, Any]] = []
    for stock in ctx.stocks:
        result = ctx.score(stock.id)
        if result is None:
            continue
        avoided.append(
            {"id": stock.id, "avoided_pse": result.avoided, "job_pse": result.job_pse,
             "raw_pse": result.raw, "clamped": result.clamped}
        )
    payload["avoided"] = avoided
    return payload


def _projection_block(prepared: Prepared) -> str:
    report = prepared.objective
    if report is None:
        return ""
    used = report.get("used") or "none (exhausted fallback)"
    lines = [
        "",
        f"objective: {used} (configured {report['configured']}"
        + (", shadow" if report.get("shadow") else "")
        + ")",
    ]
    if report.get("fallback"):
        lines.append(f"  fell back to own_waste: {report['fallback']}")
    if report.get("shadow_chosen"):
        lines.append(f"  joint objective would pick: {report['shadow_chosen']}")
    payload = _projection_payload(prepared)
    if "settled" not in payload:
        return "\n".join(lines) + "\n"
    age = report.get("forecast_age_s")
    lines.append(
        f"  job {report['job_pse']:.4f} PSE; demand forecast "
        f"{_format_age(age) if isinstance(age, (int, float)) else '?'} old"
        + ("; partial fleet (--only/--exclude)" if report.get("partial_fleet") else "")
    )
    for stream in payload["streams"]:
        lines.append(
            f"  demand {stream['group']}/{stream['name']}: {stream['pse_per_h']:.4f} PSE/h"
            f" ({stream['fable_share']:.0%} Fable)"
        )
    lines.append("  projection, leftover at each deadline inside the horizon:")
    for row in payload["settled"]:
        lines.append(
            f"    {row['id']:<22} {row['kind']:<12} in {row['hours']:6.1f}h  "
            f"leftover {row['leftover_pse']:.3f} PSE"
        )
    for row in payload["unmet"]:
        lines.append(f"    unmet {row['group']}/{row['stream']}: {row['pse']:.3f} PSE")
    lines.append("  waste this job avoids:")
    for row in payload["avoided"]:
        lines.append(
            f"    {row['id']:<22} {row['avoided_pse']:.4f} of {row['job_pse']:.4f} PSE"
            + ("  (clamped)" if row["clamped"] else "")
        )
    return "\n".join(lines) + "\n"


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


#: Read this much history before the forecast window, so the reading that straddles the
#: window's start is still paired with the one after it.
_FORECAST_LEAD_S: Final[float] = 6 * 3600.0


def _forecast_records(
    env: Mapping[str, str], now_s: float, window_s: float, path: str | None = None
) -> Callable[[], Any]:
    def load() -> Any:
        return history_mod.iter_records(
            path,
            env=env,
            include_rotated=True,
            since_s=now_s - window_s - _FORECAST_LEAD_S,
        )

    return load


def _refresh_forecast(config: Config, env: Mapping[str, str], now_s: float) -> list[str]:
    """Keep the demand forecast warm for the joint objective.

    Run from ``status`` for the same reason as :func:`_record_waste`: the poller already
    calls it on a schedule, and a pick must never scan history. ``forecast_refresh_s``
    keeps the scan to once per quarter hour.
    """
    plans, warnings = _plans_for(config)
    result = forecast_mod.refresh_if_due(
        _forecast_records(env, now_s, forecast_mod.DEFAULT_WINDOW_S),
        now_s=now_s,
        plans=plans,
        manual_rates=config.manual_rates(),
        window_s=forecast_mod.DEFAULT_WINDOW_S,
        refresh_s=config.engine.forecast_refresh_s,
        env=env,
    )
    return warnings + list(result.warnings)


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

    # Two populations of warning, and only one of them is about this reading. The waste
    # scan re-walks every retained history record on every run, so its notes describe
    # window transitions observed days ago and their count only grows. Printing them
    # beside a warning about the snapshot just taken buries the second in the first --
    # on the operator's fleet, seven live warnings under thirty-four history notes. The
    # default view prints every live warning and states how many history notes exist;
    # --verbose prints both in full, and --json still carries both, in that order.
    notes = _record_waste(prepared.config, env)
    forecast_notes = _refresh_forecast(prepared.config, env, now_s)
    # The forecast feeds only the joint objective. Until it is configured, or shadowed,
    # its notes would be noise in a status nobody asked to change.
    engine = prepared.config.engine
    if engine.objective != "own_waste" or engine.shadow:
        notes = notes + forecast_notes
    history_notes = tuple(dict.fromkeys(notes))
    live_warnings = tuple(
        note for note in dict.fromkeys(prepared.warnings) if note not in set(history_notes)
    )
    prepared.warnings = live_warnings + history_notes

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
                    "manual_reserve": _reserve_dict(prepared, snapshot.id),
                    "deprioritized": snapshot.id
                    in prepared.config.deprioritized_accounts(),
                }
                for snapshot in prepared.snapshots
            ],
            "state": prepared.state.to_dict(),
            "config_sources": list(prepared.config.sources),
            "degraded": list(prepared.degraded),
            "warnings": list(dict.fromkeys(prepared.warnings)),
        }
        pools, pool_notes = _credit_pool_rows(prepared.config, env, now_s, record_expiries=True)
        if pools:
            # Additive, and only when [credit_pools] is configured.
            payload["credit_pools"] = pools
            payload["warnings"] = payload["warnings"] + [
                n for n in pool_notes if n not in payload["warnings"]
            ]
        _dump_json(payload, stdout)
        return EXIT_OK

    if not prepared.snapshots:
        stdout.write("no accounts visible\n")
    elif getattr(args, "verbose", False):
        _write_status_verbose(prepared, now_s, model_class, stdout)
    else:
        _write_status_compact(prepared, now_s, model_class, stdout)
    pools, pool_notes = _credit_pool_rows(prepared.config, env, now_s, record_expiries=True)
    if pools:
        zone, zone_note = _display_zone(env)
        stdout.write("\n" + _credit_pool_table(pools, now_s, zone))
        if zone_note:
            pool_notes = [*pool_notes, zone_note]
    for note in pool_notes:
        stderr.write(f"! {note}\n")

    _flush(stdout)
    for warning in live_warnings:
        stderr.write(f"! {warning}\n")
    if getattr(args, "verbose", False):
        for note in history_notes:
            stderr.write(f"! {note}\n")
    elif history_notes:
        stderr.write(
            f"! {len(history_notes)} note(s) about past window transitions in retained "
            f"history; {_PROG} status --verbose prints them\n"
        )
    return EXIT_OK


def _reserve_dict(prepared: Prepared, account_id: str) -> dict[str, Any] | None:
    """The manual-use reserve behind one account's figures, or ``None`` when it sets none."""
    held = next((r for r in prepared.manual_reserves if r.account_id == account_id), None)
    return None if held is None else held.to_dict()


def _reserve_block(prepared: Prepared) -> str:
    """One line per manual-use reserve, newline-terminated; empty when there are none."""
    lines = [explain_mod.format_manual_reserve(held) for held in prepared.manual_reserves]
    return "".join(line + "\n" for line in lines)


def _deprioritized_block(prepared: Prepared) -> str:
    """One line naming every deprioritized account, newline-terminated; empty when none."""
    line = explain_mod.format_deprioritized(prepared.config.deprioritized_accounts())
    return line + "\n" if line else ""


def _plan_end_block(prepared: Prepared) -> str:
    """One line per configured plan end, newline-terminated; empty when there are none."""
    lines = plan_end_mod.describe_plan_ends(prepared.config.plan_ends(), prepared.now_s)
    return "".join(line + "\n" for line in lines)


def _flush(stream: TextIO) -> None:
    """Flush a stream that has a ``flush``; a test's StringIO does, a pipe may not."""
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def _write_status_verbose(
    prepared: Prepared,
    now_s: float,
    model_class: str | None,
    stdout: TextIO,
) -> None:
    """One block per account with the full slack arithmetic -- the pre-table rendering.

    Kept verbatim because it is the view that shows the quantity the router actually
    ranks on. The compact view restates that quantity as pace, which is easier to read
    and loses the decomposition; this is where the decomposition still lives.
    """
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
    stdout.write(_reserve_block(prepared))
    stdout.write(_deprioritized_block(prepared))


def _write_status_compact(
    prepared: Prepared,
    now_s: float,
    model_class: str | None,
    stdout: TextIO,
) -> None:
    """The default view: one line per account, one screen for the fleet."""
    scope = "" if model_class is None else f" (model class {model_class})"
    stdout.write(
        f'quota LEFT + time to reset ("-" = no such window); '
        f"TIGHTEST = pace vs an even burn{scope}\n\n"
    )
    reserves = {held.account_id: held for held in prepared.manual_reserves}
    table = explain_mod.format_status_table(
        prepared.snapshots, now_s, model_class, reserves,
        max_age_s=prepared.config.staleness.max_age_s,
    )
    if table:
        stdout.write(table + "\n")
    held = _reserve_block(prepared) + _deprioritized_block(prepared) + _plan_end_block(prepared)
    if held:
        stdout.write(("\n" if table else "") + held)
    unmeasurable = explain_mod.format_unmeasurable(prepared.snapshots)
    if unmeasurable:
        stdout.write(("\n" if table else "") + unmeasurable + "\n")


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

    # An account selected by a macOS user is reached by WHO runs the binary, so the
    # prefix wraps whatever argv we just built -- including a caller-supplied command.
    # The wrapper allowlists the binary it will run, so a forwarded command it does
    # not recognize is refused there rather than run as the wrong user.
    plan = _launch_plan(account)
    argv = [*plan["argv_prefix"], *argv]

    child_env = {
        key: value
        for key, value in env.items()
        if key.strip().upper() not in BANNED_EXEC_ENV
    }
    child_env.update(plan["env"])
    # Deletions come last: an inherited HOME would otherwise point the binary at the
    # CALLING user's profile directory while it runs as somebody else.
    for key in plan["unset_env"]:
        child_env.pop(key, None)

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
            "env": plan["env"],
            "unset_env": plan["unset_env"],
            "generated_at": _iso(now_s),
        }
        if getattr(args, "json", False):
            _dump_json(payload, stdout)
        else:
            overlay = " ".join(
                [f"-u {k}" for k in payload["unset_env"]]
                + [f"{k}={v}" for k, v in payload["env"].items()]
            )
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
                "superseded": len(records) - len(waste_mod.current_records(records)),
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

    current = waste_mod.current_records(records)
    superseded = len(records) - len(current)
    stdout.write(
        f"{len(current)} reset(s) recorded in {path}"
        + (f" ({superseded} older row(s) superseded by a newer version)" if superseded else "")
        + "\n\n"
    )
    stdout.write(f"{'account':<12}{'window':<10}{'resets':>7}{'observed':>10}"
                 f"{'missed':>8}{'wasted PSE':>12}{'worst gap':>11}{'early':>7}\n")
    for account_id in sorted(summary["accounts"]):
        for key in sorted(summary["accounts"][account_id]):
            row = summary["accounts"][account_id][key]
            gap = row["max_observation_gap_s"]
            stdout.write(
                f"{account_id:<12}{key:<10}{row['resets']:>7}{row['observed']:>10}"
                f"{row['missed']:>8}{row['wasted_pse']:>12.2f}"
                f"{(explain_mod.format_duration(gap) if gap is not None else '-'):>11}"
                f"{row['early']:>7}\n"
            )

    stdout.write(
        f"\n{totals['wasted_pse']:.2f} PSE expired unused across {totals['resets']} "
        f"scheduled weekly reset(s) -- {totals['observed']} observed, "
        f"{totals['missed']} missed.\n"
    )
    if totals["missed"]:
        stdout.write(
            "A missed reset has no remainder and is counted, never averaged over: its "
            "quota is gone and unmeasured, so the total above is a LOWER bound.\n"
        )
    if totals["unclassified"]:
        stdout.write(
            f"{totals['unclassified']} of those resets were recorded before early resets "
            f"were told apart, and history no longer reaches them to check. They are "
            f"counted as scheduled.\n"
        )
    if scoped["resets"]:
        stdout.write(
            f"Model-scoped sub-caps: {scoped['wasted_pse']:.2f} PSE across "
            f"{scoped['resets']} reset(s). Listed, not added -- that budget is a slice "
            f"of the same weekly pool and is already inside the total above.\n"
        )

    early = summary["early_totals"]
    events = summary["early_events"]
    if early["resets"]:
        stdout.write(
            f"\nEarly resets, kept out of the total above: {early['resets']} across "
            f"{len(events)} event(s), {early['forfeited_pse']:.2f} PSE forfeited.\n"
            f"The provider rolled these windows over before their published boundary "
            f"and opened a fresh one. Several accounts at once points to a one-off "
            f"reset the provider granted to everyone.\n\n"
        )
        stdout.write(f"  {'reset between (UTC)':<36}{'accounts':<24}{'early by':>14}"
                     f"{'forfeited':>11}\n")
        for event in events:
            low = explain_mod.format_duration(event["min_early_by_s"])
            high = explain_mod.format_duration(event["max_early_by_s"])
            when = (
                f"{event['reset_not_before'][:16].replace('T', ' ')} - "
                f"{event['reset_not_after'][11:16]}"
                if event["reset_not_before"][:10] == event["reset_not_after"][:10]
                else f"{event['reset_not_before'][:16]} - {event['reset_not_after'][:16]}"
            )
            accounts = ", ".join(event["accounts"])
            stdout.write(
                f"  {when:<36}{accounts:<24}{(low if low == high else f'{low}-{high}'):>14}"
                f"{event['forfeited_pse']:>11.2f}"
                f"{'  ' + str(len(event['accounts'])) + ' accounts at once' if event['shared'] else ''}"
                f"\n"
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
    from .capability import EFFORTS as _EFFORTS

    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Pick which of your own already-authorized LLM accounts should serve a call, "
            "preferring quota that would otherwise expire unused."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    pick_parser = subparsers.add_parser(
        "pick",
        parents=[common],
        help="print the routing decision as JSON (always exits 0 unless flags are bad)",
    )
    pick_parser.add_argument(
        "--capability",
        choices=("fast", "standard", "premium", "frontier"),
        default=None,
        help=(
            "route by capability across providers: each account is judged for the model "
            "it resolves to (from the table `quotapick capabilities --refresh` keeps), "
            "and the payload names that model. With --model, the model is pinned and "
            "only its provider's accounts are considered"
        ),
    )
    pick_parser.add_argument(
        "--effort",
        choices=_EFFORTS,
        default=None,
        help=(
            "with --capability: the reasoning effort YOU chose, translated per account. "
            "Each model gains effort {requested, applied, clamped, supported}; applied is "
            "the highest supported level at or below the request, else the lowest above "
            "it. Never changes which account wins"
        ),
    )
    pick_parser.add_argument(
        "--job-pse",
        type=float,
        default=None,
        metavar="PSE",
        help=(
            "size of this job in PSE, for the joint objective ([engine] objective = "
            "\"joint\"); default [engine] default_job_pse"
        ),
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
    status_parser = subparsers.add_parser(
        "status",
        parents=[common],
        help="show what quota is left on every account, and how it is pacing",
    )
    status_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "one block per account with the full slack arithmetic, plus every note the "
            "history scan produced"
        ),
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
    launch_plan_parser = subparsers.add_parser(
        "launch-plan",
        parents=[common],
        help=(
            "print how to launch ONE named account (argv prefix, env overlay, env to "
            "unset); config only, no routing and no measurement"
        ),
    )
    launch_plan_parser.add_argument(
        "account",
        metavar="ACCOUNT",
        help="account id to describe (for example antigravity_gemini_b)",
    )
    doctor_parser = subparsers.add_parser(
        "doctor",
        parents=[common],
        help=(
            "check that this machine's routing is in force: that a configured "
            "reserve reaches the decision, and that other environments carry a "
            "copy new enough to obey it"
        ),
    )
    doctor_parser.add_argument(
        "--consumer",
        action="append",
        default=None,
        metavar="PYTHON",
        help=(
            "a python interpreter to probe for its quota_router (repeatable). "
            "Probing runs that interpreter, so it is never implied"
        ),
    )
    doctor_parser.add_argument(
        "--scan",
        action="append",
        default=None,
        metavar="DIR",
        help=(
            "search DIR for .venv interpreters and probe each one (repeatable; "
            "bounded to 3 directories deep)"
        ),
    )
    doctor_parser.add_argument(
        "--probe-timeout",
        type=float,
        default=20.0,
        metavar="SECONDS",
        help="per-consumer probe timeout (default: 20)",
    )
    report_parser = subparsers.add_parser(
        "report-failure",
        parents=[common],
        help=(
            "classify a vendor CLI's failure text for an `unmetered = \"fallback\"` "
            "account and, if it says the pool is spent, keep pick off it until it resets"
        ),
    )
    report_parser.add_argument("account", metavar="ACCOUNT", help="the account that failed")
    source = report_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", metavar="TEXT", help="the failure text itself")
    source.add_argument(
        "--text-file", metavar="PATH", help="a file holding the failure text, or - for stdin"
    )
    capabilities_parser = subparsers.add_parser(
        "capabilities",
        parents=[common],
        help=(
            "show which model each account resolves for each capability "
            "(fast, standard, premium, frontier), from the table the poller keeps"
        ),
    )
    capabilities_parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "read every account's model list first (Claude catalogs; Codex model/list "
            "and agy models at most every 15 minutes) and update the table"
        ),
    )
    capabilities_parser.add_argument(
        "--force",
        action="store_true",
        help="with --refresh, call the network sources even if read in the last 15 minutes",
    )
    capabilities_parser.add_argument(
        "--effort",
        choices=_EFFORTS,
        default=None,
        help="translate each cell's model to this reasoning effort, as pick --effort does",
    )
    waste_parser = subparsers.add_parser(
        "waste",
        parents=[common],
        help="report quota that expired unused at each window reset",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="execute one tool-less call from a JSON request on stdin; print what served it",
    )
    # Only the flags `run` honors, so argparse refuses the rest. The model, effort and
    # timeout belong in the request, and a run always spends: on 2026-10-16 a shared
    # --dry-run was accepted, ignored, and a real call ran.
    run_parser.add_argument(
        "--only",
        action="append",
        metavar="IDS",
        help="run only on these accounts or credit pools (comma-separated, repeatable)",
    )
    run_parser.add_argument(
        "--exclude",
        action="append",
        metavar="IDS",
        help="never run on these accounts or credit pools (comma-separated, repeatable)",
    )
    run_parser.add_argument(
        "--config",
        metavar="PATH",
        help="additional config file, applied last (must exist)",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="accepted for symmetry; the response is always JSON",
    )
    credits_parser = subparsers.add_parser(
        "credits",
        parents=[common],
        help="show API credit pool balances, or record the Console figure",
    )
    credits_actions = credits_parser.add_subparsers(dest="credits_action")
    set_parser = credits_actions.add_parser("set", help="record the balance the Console shows")
    set_parser.add_argument("pool")
    set_parser.add_argument("--remaining", type=float, required=True, metavar="USD")
    set_parser.add_argument("--expires", default=None, metavar="ISO",
                            help="e.g. 2026-10-19T00:00:00Z (the Console shows UTC dates)")
    grant_parser = credits_actions.add_parser("grant", help="record a new grant")
    grant_parser.add_argument("pool")
    grant_parser.add_argument("--amount", type=float, required=True, metavar="USD")
    grant_parser.add_argument("--expires", required=True, metavar="ISO")
    replay_parser = subparsers.add_parser(
        "replay",
        parents=[common],
        help="route the last N days of real demand through each policy and compare waste",
    )
    replay_parser.add_argument("--days", type=float, default=None,
                               help="days to count, ending now (default 21)")
    replay_parser.add_argument("--policies", default=None,
                               help="comma-separated (default: all)")
    replay_parser.add_argument("--job-pse", type=float, default=None,
                               help="job size in PSE (default 0.01)")
    replay_parser.add_argument("--forecast-scale", type=float, default=None,
                               help="multiply the forecast, to test robustness (default 1)")
    replay_parser.add_argument("--group", default=None,
                               help="provider group (default claude)")
    replay_parser.add_argument("--history", default=None, metavar="PATH",
                               help="history file to read instead of the live one")
    replay_parser.add_argument("--waste", default=None, metavar="PATH",
                               help="waste series to check fidelity against")
    replay_parser.add_argument("--bin-minutes", type=float, default=None,
                               help="demand bin size (default 15)")
    replay_parser.add_argument("--without", action="append", default=None, metavar="ACCOUNT",
                               help="what-if: drop this account's pool, keep its demand")
    forecast_parser = subparsers.add_parser(
        "forecast",
        parents=[common],
        help="measure demand per provider group, in PSE per hour, from history",
    )
    forecast_parser.add_argument(
        "--refresh",
        action="store_true",
        help="also write the cache the joint objective reads (status does this every 15 min)",
    )
    forecast_parser.add_argument(
        "--history",
        default=None,
        metavar="PATH",
        help="read this history file and its rotated siblings instead of the live one",
    )
    forecast_parser.add_argument(
        "--days",
        type=float,
        default=None,
        help="look-back in days (default 7)",
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


def _cmd_launch_plan(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    """How to launch ONE NAMED account. No routing, no measurement, no reservation.

    This is the command a consumer calls when it already knows which account it
    wants and only needs to be told how to reach it -- for example an Antigravity
    pool pinned by a caller. Asking `pick --only <id>` instead would book a
    reservation and could still answer with a different account, or none.

    It reads the config and nothing else: no Keychain, no network, no history. So it
    stays answerable on a machine where every oracle is down, which is precisely when
    a consumer most needs to know how to spawn something.
    """
    try:
        config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    except ConfigError as exc:
        stderr.write(f"{_PROG}: {exc}\n")
        return EXIT_ROUTER_FAILURE

    account_id = getattr(args, "account", None)
    account = config.account(account_id) if account_id else None
    if account is None:
        known = ", ".join(sorted(config.accounts)) or "(none configured)"
        stderr.write(
            f"{_PROG}: no account {account_id!r} in this config. Configured: {known}\n"
        )
        return EXIT_ROUTER_FAILURE

    plan = _launch_plan(account)
    payload = {
        "contract_version": QUOTA_ROUTER_CONTRACT_VERSION,
        "account": account.id,
        "provider": account.provider,
        # The account this SHOULD be, so a consumer can assert the identity the call
        # actually served against the identity the operator declared. Two macOS users
        # signed into one Google account look exactly like two accounts from here.
        "identity_email": account.identity_email,
        "enabled": account.enabled,
        "command": account.exec_command,
        **plan,
        "generated_at": _iso(now_s),
    }
    if getattr(args, "json", False):
        _dump_json(payload, stdout)
    else:
        overlay = " ".join(
            [f"-u {k}" for k in plan["unset_env"]]
            + [f"{k}={v}" for k, v in plan["env"].items()]
        )
        argv = " ".join([*plan["argv_prefix"], account.exec_command])
        stdout.write(f"{overlay} {argv}\n".lstrip())
    return EXIT_OK


def report_failure(
    config: Config,
    account_id: str,
    text: str,
    *,
    env: Mapping[str, str],
    now_s: float,
) -> dict[str, Any]:
    """Classify one failure and record a cooldown when it means exhaustion.

    Raises:
        ValueError: the account is unknown, disabled, or measured. A measured account
            already has live readings; a second, failure-learned source could
            disagree with them, so it is refused until there is evidence to want it.
    """
    from .failure_text import classify_failure_text

    account = config.account(account_id)
    if account is None or not account.enabled:
        raise ValueError(f"{account_id} is not an enabled account")
    if account.unmetered != "fallback":
        raise ValueError(
            f"{account_id} has live usage windows; report-failure accepts only "
            f'accounts with unmetered = "fallback"'
        )
    verdict = classify_failure_text(text, now_s)
    until_s = None
    written = False
    problems: list[str] = []
    if verdict.is_exhaustion and not verdict.blocks_account:
        # A limit on one model class ("Fable 5 limit") leaves the account's other
        # models routable; benching the whole account would over-react.
        problems.append(
            f"exhaustion is scoped to model class {verdict.model_class!r}, not the "
            f"account; no cooldown recorded"
        )
    elif verdict.is_exhaustion:
        until_s = verdict.exhausted_until_s or now_s + UNKNOWN_DEADLINE_COOLDOWN_S
        result = StateStore(env=env).record_cooldown(account_id, until_s=until_s, now_s=now_s)
        written = result.ok
        problems.extend(result.warnings)
    return {
        "account": account_id,
        "kind": verdict.kind,
        "reason": verdict.reason,
        "matched": verdict.matched,
        "deadline_known": verdict.exhausted_until_s is not None,
        "cooldown_until": _iso(until_s) if until_s is not None else None,
        "written": written,
        "warnings": problems,
    }


def _cmd_report_failure(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    """Exit 0 when classified (written or not), 2 when refused, 127 on a write failure."""
    try:
        config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    except ConfigError as exc:
        stderr.write(f"{_PROG}: {exc}\n")
        return EXIT_ROUTER_FAILURE
    if args.text is not None:
        text = args.text
    elif args.text_file == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            stderr.write(f"{_PROG}: cannot read {args.text_file}: {exc}\n")
            return EXIT_USAGE
    try:
        outcome = report_failure(config, args.account, text, env=env, now_s=now_s)
    except ValueError as exc:
        stderr.write(f"{_PROG}: {exc}\n")
        return EXIT_USAGE
    stdout.write(json.dumps(outcome, indent=2) + "\n")
    if outcome["cooldown_until"] and not outcome["written"]:
        return EXIT_ROUTER_FAILURE
    return EXIT_OK


def _cmd_capabilities(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    """Show the capability table, refreshing it first with ``--refresh``.

    Exit 0 when every account resolved, 1 when a refresh read failed or the table is
    missing or unreadable, 3 on a config error.
    """
    from . import capability as cap_mod
    from . import capability_refresh as refresh_mod

    try:
        config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    except ConfigError as exc:
        stderr.write(f"{_PROG}: {exc}\n")
        return EXIT_ROUTER_FAILURE

    report = None
    if args.refresh:
        report = refresh_mod.refresh(
            config,
            env=env,
            now_s=now_s,
            deps=deps.capability_refresh,
            force=bool(getattr(args, "force", False)),
        )

    path = cap_mod.table_path(env)
    table, error = cap_mod.read_table(path)
    rows = _capability_rows(config, table, path, now_s, effort=getattr(args, "effort", None))
    if args.json:
        payload = {
            "generated_at": _iso(now_s),
            "table_path": str(path),
            "table_error": error,
            "refresh": report.to_dict() if report else None,
            "accounts": rows,
        }
        # One line per run: the poller appends this to capability-refresh.log, and a
        # log is read with `tail -1`.
        stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    else:
        stdout.write(_format_capability_rows(rows, path, report, error))
    failed = bool(report and (report.failed or report.locked))
    return EXIT_OK if (table is not None and not failed and not error) else 1


def _capability_rows(
    config: Config,
    table: Mapping[str, Any] | None,
    path: Any,
    now_s: float,
    *,
    effort: str | None = None,
) -> list[dict[str, Any]]:
    from . import capability as cap_mod

    rows = []
    for account in config.enabled_accounts():
        if account.provider not in cap_mod.CAPABILITY_PROVIDERS:
            continue
        entry = (table or {}).get("accounts", {}).get(account.id)
        listing = cap_mod.Listing.from_json(entry.get("list")) if isinstance(entry, Mapping) else None
        row: dict[str, Any] = {
            "account": account.id,
            "provider": account.provider,
            "refreshed_at": (
                _iso(entry["refreshed_at"])
                if isinstance(entry, Mapping) and isinstance(entry.get("refreshed_at"), (int, float))
                else None
            ),
            "list_source": listing.source if listing else None,
            "list_fetched_at": cap_mod._iso(listing.fetched_at_s) if listing else None,
            "warnings": list(entry.get("warnings", [])) if isinstance(entry, Mapping) else [],
            "capabilities": {},
        }
        for name in cap_mod.CAPABILITIES:
            result: Any = cap_mod.lookup(table, account.id, name, now_s=now_s, path=path)
            if effort and isinstance(result, cap_mod.ModelChoice):
                result = cap_mod.apply_effort(result, effort)
                if isinstance(result, cap_mod.Unresolved):
                    result = cap_mod.Unresolved(f"effort {effort}: {result.reason}")
            cap_entry = (
                entry.get("capabilities", {}).get(name) if isinstance(entry, Mapping) else None
            )
            candidate = cap_entry.get("candidate") if isinstance(cap_entry, Mapping) else None
            row["capabilities"][name] = {
                "model": result.to_dict() if isinstance(result, cap_mod.ModelChoice) else None,
                "reason": result.reason if isinstance(result, cap_mod.Unresolved) else None,
                "candidate": (
                    {
                        "id": candidate.get("id"),
                        "reason": candidate.get("reason"),
                        "first_seen": cap_mod._iso(candidate.get("first_seen")),
                        "seen": candidate.get("seen"),
                    }
                    if isinstance(candidate, Mapping)
                    else None
                ),
                "moved_at": (
                    cap_mod._iso(cap_entry.get("moved_at")) if isinstance(cap_entry, Mapping) else None
                ),
            }
        rows.append(row)
    return rows


def _format_capability_rows(
    rows: Sequence[Mapping[str, Any]], path: Any, report: Any, error: str | None
) -> str:
    from . import capability as cap_mod

    lines = [f"capability table: {path}"]
    if error:
        lines.append(f"  ERROR: {error}")
    if report is not None:
        summary = report.to_dict()
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(summary["failed_by_kind"].items()))
        lines.append(
            f"refresh: attempted {summary['attempted']}, succeeded {summary['succeeded']}, "
            f"failed {summary['failed']}" + (f" ({kinds})" if kinds else "")
            + (" [skipped: another refresh holds the lock]" if summary["locked"] else "")
        )
        for outcome in summary["outcomes"]:
            if outcome["outcome"] == "failed":
                lines.append(f"  {outcome['account']}: FAILED {outcome['kind']}: {outcome['detail']}")
        for move in summary["moves"]:
            lines.append(
                f"  MOVED {move['account']} {move['capability']}: "
                f"{move['from'] or move['from_reason']} -> {move['to'] or move['to_reason']} ({move['cause']})"
            )
    lines.append("")
    header = ["ACCOUNT", *cap_mod.CAPABILITIES, "SOURCE", "REFRESHED"]
    table_rows = []
    notes = []
    for row in rows:
        cells = [row["account"]]
        for name in cap_mod.CAPABILITIES:
            item = row["capabilities"][name]
            if item["model"]:
                text = item["model"]["id"]
                if not item["model"]["listed"]:
                    text += " (unlisted)"
                effort = item["model"].get("effort")
                if effort and effort["clamped"]:
                    text += "!"
                    notes.append(
                        f"{row['account']} {name}: effort {effort['requested']} clamped to "
                        f"{effort['applied']} (supports {', '.join(effort['supported'])})"
                    )
            else:
                text = "-"
                notes.append(f"{row['account']} {name}: {item['reason']}")
            if item["candidate"]:
                text += "*"
                cand = item["candidate"]
                notes.append(
                    f"{row['account']} {name}: candidate {cand['id'] or cand['reason']} "
                    f"(seen {cand['seen']}x since {cand['first_seen']})"
                )
            cells.append(text)
        cells += [row["list_source"] or "-", row["refreshed_at"] or "-"]
        table_rows.append(cells)
        notes.extend(row["warnings"])
    widths = [max(len(str(r[i])) for r in [header, *table_rows]) for i in range(len(header))]
    for cells in [header, *table_rows]:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip())
    if notes:
        lines.append("")
        lines.extend(f"  {note}" for note in dict.fromkeys(notes))
    return "\n".join(lines) + "\n"


def _cmd_doctor(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    """Check that the operator's routing policy is actually being obeyed.

    Not "is quotapick installed". Every failure this looks for produces a
    well-formed decision that names the wrong account, so nothing downstream can
    see it: an old copy of this package in a consumer's environment IGNORES
    manual_rate_per_day rather than rejecting it.

    With no flags it touches only this process and the operator's own config.
    Probing another environment executes that interpreter, so it is opt-in
    through --consumer and --scan.
    """
    checks: list[doctor_mod.Check] = []

    try:
        config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    except ConfigError as exc:
        stderr.write(f"{_PROG}: {exc}\n")
        return EXIT_ROUTER_FAILURE
    rates = config.manual_rates()
    checks.append(
        doctor_mod.Check(
            "config",
            "ok",
            f"{len(config.enabled_accounts())} enabled account(s), "
            f"{len(rates)} with a manual reserve",
        )
    )

    # A real pick, not a recomputation. The question is whether the reserve
    # reaches the DECISION, so the decision is what has to be asked.
    pick_args = SimpleNamespace(
        model=None, only=None, exclude=None, min_remaining=None, no_sticky=True,
        timeout_ms=None, config=getattr(args, "config", None), dry_run=True,
        json=True, explain=False, now=None,
    )
    try:
        prepared = _prepare(
            pick_args, env, now_s, deps, cwd, event="pick", write_state=False
        )
    except ConfigError as exc:
        stderr.write(f"{_PROG}: {exc}\n")
        return EXIT_ROUTER_FAILURE
    checks.extend(
        doctor_mod.check_reserve_is_enforced(
            reserves=prepared.manual_reserves,
            winner=prepared.decision.chosen,
        )
    )
    # A rate in the config that produced no reserve never reached routing. That
    # is the skew failure seen from this side, and it is silent.
    unapplied = sorted(set(rates) - {held.account_id for held in prepared.manual_reserves})
    for account_id in unapplied:
        checks.append(
            doctor_mod.Check(
                f"reserve[{account_id}]",
                "fail",
                f"config sets manual_rate_per_day={rates[account_id]:g} but the "
                f"decision applied no reserve for it",
                remedy=(
                    "the account has no 7-day window to hold back from, or it was "
                    "excluded before the reserve ran; see `quotapick status`"
                ),
            )
        )

    cached_forecast, forecast_problem = forecast_mod.read_cache(env=env)
    checks.extend(
        doctor_mod.check_forecast(
            computed_at_s=cached_forecast.computed_at_s if cached_forecast else None,
            problem=forecast_problem,
            engine=config.engine,
            credit_pools=len(config.credit_pools),
            now_s=now_s,
        )
    )
    pool_rows, _pool_notes = _credit_pool_rows(config, env, now_s)
    checks.extend(
        doctor_mod.check_credit_pools(
            pool_rows,
            now_s=now_s,
            keychain_runner=deps.run,
            services={p.id: p.keychain_service for p in config.credit_pools.values()},
        )
    )

    from . import capability as cap_mod

    cap_path = cap_mod.table_path(env)
    cap_table, cap_error = cap_mod.read_table(cap_path)
    checks.extend(
        doctor_mod.check_capabilities(
            table=cap_table,
            table_error=cap_error,
            path=str(cap_path),
            accounts=[
                (account.id, cap_mod.pool(account))
                for account in config.enabled_accounts()
                if account.provider in cap_mod.CAPABILITY_PROVIDERS
            ],
            now_s=now_s,
        )
    )

    named = list(getattr(args, "consumer", None) or [])
    targets = list(named)
    for root in getattr(args, "scan", None) or []:
        targets.extend(doctor_mod.discover_consumer_pythons([root]))
    seen: set[str] = set()
    ordered = [t for t in targets if not (t in seen or seen.add(t))]
    if ordered:
        reports = [
            doctor_mod.inspect_consumer(
                target, timeout_s=float(getattr(args, "probe_timeout", 20.0))
            )
            for target in ordered
        ]
        checks.extend(
            doctor_mod.check_consumers(
                reports, own_version=doctor_mod.own_version(), named=named
            )
        )
    else:
        checks.append(
            doctor_mod.Check(
                "consumers",
                "warn",
                "none probed; this only checked the copy running right now",
                remedy=(
                    "pass --consumer <python> or --scan <dir>. The reserve lives in "
                    "one config file, but every consumer carries its own copy of the "
                    "code that reads it"
                ),
            )
        )

    stdout.write(doctor_mod.format_report(checks))
    return doctor_mod.worst_exit_code(checks)


def _cmd_forecast(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    """Measure demand per provider group from history; ``--refresh`` also caches it.

    Read-only towards history: it never appends to, rotates or prunes it.
    """
    config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    days = getattr(args, "days", None) or forecast_mod.DEFAULT_WINDOW_S / 86400.0
    if not days > 0:
        stderr.write(f"{_PROG} forecast: --days must be positive, got {days}\n")
        return EXIT_USAGE
    window_s = days * 86400.0
    plans, warnings = _plans_for(config)
    result = forecast_mod.measure(
        _forecast_records(env, now_s, window_s, getattr(args, "history", None))(),
        plans=plans,
        now_s=now_s,
        window_s=window_s,
        manual_rates=config.manual_rates(),
    )
    written: str | None = None
    if getattr(args, "refresh", False):
        try:
            written = str(forecast_mod.write_cache(result, env=env))
        except OSError as exc:
            warnings.append(f"cannot write the demand forecast: {exc}")
    notes = list(dict.fromkeys(warnings + list(result.warnings)))

    if getattr(args, "json", False):
        _dump_json({**result.to_dict(), "written": written, "warnings": notes}, stdout)
        return EXIT_OK

    stdout.write(
        f"demand over the last {days:g} day(s), ending {_iso(now_s)}\n\n"
        f"{'group':<12} {'other PSE/h':>11} {'fable':>6} {'run PSE/h':>10} "
        f"{'coverage':>9}  accounts\n"
    )
    for name, group in sorted(result.groups.items()):
        stdout.write(
            f"{name:<12} {group.other.pse_per_h:>11.4f} {group.other.fable_share:>6.0%} "
            f"{group.run.pse_per_h:>10.4f} {group.coverage:>9.0%}  "
            f"{', '.join(group.accounts) or '-'}\n"
        )
    if not result.groups:
        stdout.write("(no measurable demand in the window)\n")
    if written:
        stdout.write(f"\nwritten to {written}\n")
    for note in notes:
        stderr.write(f"! {note}\n")
    return EXIT_OK


def _cmd_replay(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    """Route the last N days of real demand through each policy and compare the waste.

    Read-only towards history and the waste series. The comparison counts only when the
    ``actual`` policy reproduces the recorded waste (the fidelity gate).
    """
    from . import replay as replay_mod

    config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    days = float(getattr(args, "days", None) or 21.0)
    if not days > 0:
        stderr.write(f"{_PROG} replay: --days must be positive, got {days}\n")
        return EXIT_USAGE
    policies = [
        name.strip()
        for name in (getattr(args, "policies", None) or ",".join(replay_mod.POLICIES)).split(",")
        if name.strip()
    ]
    count_from = now_s - days * 86400.0
    history_path = getattr(args, "history", None)
    records = list(
        history_mod.iter_records(
            history_path,
            env=env,
            include_rotated=True,
            since_s=count_from - forecast_mod.DEFAULT_WINDOW_S - _FORECAST_LEAD_S,
        )
    )
    waste_rows, waste_warnings = waste_mod.read_records(
        getattr(args, "waste", None) or waste_mod.waste_path(env), env=env
    )
    plans, warnings = _plans_for(config)
    warnings.extend(waste_warnings)
    try:
        result = replay_mod.run_replay(
            records,
            waste_rows=waste_rows,
            plans=plans,
            plan_ends=config.plan_ends(),
            count_from_s=count_from,
            end_s=now_s,
            policies=policies,
            group=getattr(args, "group", None) or "claude",
            job_pse=float(getattr(args, "job_pse", None) or 0.01),
            forecast_scale=float(getattr(args, "forecast_scale", None) or 1.0),
            bin_s=float(getattr(args, "bin_minutes", None) or 15.0) * 60.0,
            without=_split_list(getattr(args, "without", None)),
        )
    except ValueError as exc:
        stderr.write(f"{_PROG} replay: {exc}\n")
        return EXIT_USAGE

    if getattr(args, "json", False):
        _dump_json({**result.to_dict(), "warnings": warnings}, stdout)
        return EXIT_OK

    actual = next((p for p in result.policies if p.policy == "actual"), None)
    stdout.write(
        f"replay of the {result.group} group, {_iso(count_from)} to {_iso(now_s)} "
        f"({days:g} days), job {result.job_pse:.3f} PSE, forecast x{result.forecast_scale:g}\n"
        f"accounts: {', '.join(result.accounts) or '(none)'}\n"
    )
    if result.fidelity_ok is None:
        stdout.write("fidelity: not checked (no `actual` run or no waste series)\n")
    else:
        verdict = "OK" if result.fidelity_ok else "FAILED: no comparison below counts"
        stdout.write(
            f"fidelity: actual {actual.observed_waste_pse:.3f} PSE observed waste vs "
            f"{result.recorded_waste_pse:.3f} recorded in waste.jsonl -> {verdict}\n"
        )
    stdout.write(
        f"\n{'policy':<24} {'metric':>7} {'waste':>7} {'tail':>7} {'forfeit':>8} "
        f"{'unmet':>7} {'served':>7} {'switch':>7} {'jobs':>6}\n"
    )
    for p in result.policies:
        stdout.write(
            f"{p.policy:<24} {p.metric_pse:>7.3f} {p.waste_pse:>7.3f} {p.tail_pse:>7.3f} "
            f"{p.forfeited_pse:>8.3f} {p.unmet_pse:>7.3f} {p.served_pse:>7.3f} "
            f"{p.switches:>7d} {p.jobs:>6d}\n"
        )
    stdout.write("\nmetric = waste at deadlines + neutral 7-day tail drain, in PSE\n")
    for note in result.notes:
        stdout.write(f"note: {note}\n")
    for warning in dict.fromkeys(warnings):
        stderr.write(f"! {warning}\n")
    return EXIT_OK


def _credit_pool_rows(
    config: Config, env: Mapping[str, str], now_s: float, *, record_expiries: bool = False
) -> tuple[list[dict[str, Any]], list[str]]:
    """Each configured credit pool's balance, from the ledger; empty without pools.

    ``record_expiries`` (``status`` only, the poller's command) writes one ``expired``
    line for each period that ended since the last run.
    """
    if not config.credit_pools:
        return [], []
    from . import credits as credits_mod

    lines, problems = credits_mod.read(env)
    plan_ends = config.plan_ends()
    usd_per_pse = config.engine.usd_per_pse or 0.0
    notes = list(problems)
    if record_expiries:
        for pool in config.credit_pools.values():
            for line in credits_mod.expiries_due(
                pool, lines, now_s=now_s, plan_end_s=plan_ends.get(pool.linked_account)
            ):
                if usd_per_pse:
                    line["remaining_pse"] = line["remaining_usd"] / usd_per_pse
                try:
                    credits_mod.append(env, line)
                except OSError as exc:
                    notes.append(f"cannot record {pool.id}'s expiry: {exc}")
                    continue
                lines.append({"v": credits_mod.LEDGER_VERSION, **line})
    rows = []
    for pool in config.credit_pools.values():
        bal = credits_mod.balance(
            pool, lines, now_s=now_s, plan_end_s=plan_ends.get(pool.linked_account)
        )
        expired_unused = sum(
            float(line.get("remaining_usd") or 0.0)
            for line in lines
            if line.get("pool") == pool.id and line.get("type") == "expired"
        )
        rows.append(
            {
                "id": pool.id,
                "provider": pool.provider,
                "linked_account": pool.linked_account,
                "enabled": pool.enabled,
                "remaining_usd": bal.remaining_usd,
                "grant_usd": bal.grant_usd,
                "remaining_pse": bal.remaining_usd / usd_per_pse if usd_per_pse else None,
                "held_usd": bal.held_usd,
                "expires": _iso(bal.expires_s),
                "expires_s": bal.expires_s,
                "source": bal.source,
                "confidence": bal.confidence,
                "exhausted": bal.exhausted,
                "expired": bal.expired,
                "available": bal.available and pool.enabled,
                "checkpoint_at": _iso(bal.since_s) if bal.since_s is not None else None,
                "checkpoint_s": bal.since_s,
                "expired_unused_usd": expired_unused,
                "note": bal.note,
            }
        )
    return rows, notes


def _credit_pool_table(
    rows: Sequence[Mapping[str, Any]], now_s: float, zone: tzinfo | None
) -> str:
    """The pools for a person: ``expires`` on the reader's clock (``_display_zone``)."""
    lines = [
        "API credit pools (reachable only through `quotapick run`):",
        f"{'pool':<20} {'linked':<10} {'left':>9} {'of':>7} {'PSE':>6}  "
        f"{'expires':<33} {'source':<9} note",
    ]
    for row in rows:
        days = (row["expires_s"] - now_s) / 86400.0
        pse = f"{row['remaining_pse']:.3f}" if row["remaining_pse"] is not None else "-"
        expires = f"{_wall_stamp(row['expires_s'], zone)} ({days:.1f}d)"
        lines.append(
            f"{row['id']:<20} {row['linked_account']:<10} "
            f"{'$' + format(row['remaining_usd'], '.2f'):>9} "
            f"{'$' + format(row['grant_usd'], '.0f'):>7} {pse:>6}  "
            f"{expires:<33} {row['source']:<9} {row['note']}"
        )
    return "\n".join(lines) + "\n"


def _cmd_credits(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    """Show credit pool balances, or record what the Console says.

    ``credits set POOL --remaining USD [--expires ISO]`` records a checkpoint;
    ``credits grant POOL --amount USD --expires ISO`` records a new grant.
    """
    from . import credits as credits_mod
    from .config import _as_instant

    config = load_config(env=env, cwd=cwd, explicit_path=getattr(args, "config", None))
    action = getattr(args, "credits_action", None)
    if action in ("set", "grant"):
        pool_id = args.pool
        if pool_id not in config.credit_pools:
            known = ", ".join(sorted(config.credit_pools)) or "none configured"
            stderr.write(f"{_PROG} credits: unknown pool {pool_id!r} (known: {known})\n")
            return EXIT_USAGE
        try:
            expires = (
                _as_instant(args.expires, "", "--expires") if args.expires is not None else None
            )
        except ConfigError as exc:
            stderr.write(f"{_PROG} credits: {exc}\n")
            return EXIT_USAGE
        amount = args.remaining if action == "set" else args.amount
        if amount is None or not amount >= 0:
            stderr.write(f"{_PROG} credits: the amount must be zero or more\n")
            return EXIT_USAGE
        if action == "grant" and expires is None:
            stderr.write(f"{_PROG} credits grant: --expires is required\n")
            return EXIT_USAGE
        line: dict[str, Any] = {"t": now_s, "pool": pool_id, "type":
                                "checkpoint" if action == "set" else "grant",
                                "source": "operator"}
        line["remaining_usd" if action == "set" else "usd"] = float(amount)
        if expires is not None:
            line["expires_s"] = expires
        credits_mod.append(env, line)

    rows, notes = _credit_pool_rows(config, env, now_s)
    if getattr(args, "json", False):
        _dump_json({"generated_at": _iso(now_s), "credit_pools": rows, "warnings": notes},
                   stdout)
        return EXIT_OK
    if not rows:
        stdout.write("no credit pools configured ([credit_pools.<id>] in the config)\n")
    else:
        zone, zone_note = _display_zone(env)
        stdout.write(_credit_pool_table(rows, now_s, zone))
        if zone_note:
            notes = [*notes, zone_note]
    for note in notes:
        stderr.write(f"! {note}\n")
    return EXIT_OK


def _cmd_run(
    args: argparse.Namespace,
    env: Mapping[str, str],
    now_s: float,
    deps: Deps,
    stdout: TextIO,
    stderr: TextIO,
    cwd: str | None,
) -> int:
    """Execute one tool-less call from a JSON request on stdin; print the response.

    Exit 0 whenever the request parsed, whatever the outcome (the response says it);
    exit 2 for a request or config the router refuses before spending anything.
    """
    from . import run as run_mod

    raw = (getattr(args, "stdin", None) or sys.stdin).read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _dump_json(run_mod.bad_request_payload(f"request is not JSON: {exc}"), stdout)
        return EXIT_USAGE
    try:
        request = run_mod.parse_request(data)
        payload = run_mod.execute(
            request, env=env, now_s=now_s, deps=deps, cwd=cwd,
            config_path=getattr(args, "config", None),
            only=_split_list(getattr(args, "only", None)),
            exclude=_split_list(getattr(args, "exclude", None)),
        )
    except run_mod.BadRequest as exc:
        request_id = data.get("request_id") if isinstance(data, Mapping) else None
        _dump_json(run_mod.bad_request_payload(str(exc), request_id), stdout)
        return EXIT_USAGE
    payload["generated_at"] = _iso(now_s)
    _dump_json(payload, stdout)
    return EXIT_OK


_COMMANDS: Final[Mapping[str, Callable[..., int]]] = {
    "pick": _cmd_pick,
    "exec": _cmd_exec,
    "status": _cmd_status,
    "explain": _cmd_explain,
    "calibrate": _cmd_calibrate,
    "waste": _cmd_waste,
    "forecast": _cmd_forecast,
    "replay": _cmd_replay,
    "credits": _cmd_credits,
    "run": _cmd_run,
    "forensics": _cmd_forensics,
    "launch-plan": _cmd_launch_plan,
    "doctor": _cmd_doctor,
    "capabilities": _cmd_capabilities,
    "report-failure": _cmd_report_failure,
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
    stdin: TextIO | None = None,
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
    # Only `run` reads stdin (its request); injected like stdout so a test never blocks.
    args.stdin = sys.stdin if stdin is None else stdin

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
