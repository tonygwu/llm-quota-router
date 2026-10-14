"""Demand forecast: how much work each provider group runs, in PSE per hour.

The joint objective (``[engine] objective = "joint"``) projects
the fleet's stocks forward and spreads forecast demand over them. This module measures
that demand from the history the router already keeps, and caches it beside
``history.jsonl`` so a pick never scans history.

What is measured, per account, between consecutive readings:

* the rise of the weekly window above its **high-water mark** for the current window,
  so a one-point quantization flicker (0.50 -> 0.49 -> 0.50) adds nothing;
* after a reset (decided by :func:`quota_router.waste.window_transition`, the same rule
  the waste series uses), only what was used since the reset -- a lower bound;
* converted to PSE with the account's own ``k`` (:func:`quota_router.scoring.plan_for`)
  and tier scale, and spread evenly over the time between the two readings.

Readings are keyed on observation time, so a reading the cache republishes adds nothing.

Two streams per group. ``run`` is work that went through ``quotapick run`` (history lines
with ``event == "run"``); ``other`` is everything else. Run work served by a subscription
is inside that subscription's measured rise and is subtracted from ``other``; run work
served by a credit pool never touched a subscription window. A configured manual reserve
already removes its quota from the stock, so it is subtracted from ``other`` too, or the
projection would count the operator's own use twice.

Groups never mix: a Codex window's rise is Codex demand, never Claude demand.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from . import waste
from .history import history_path
from .pse import Plan
from .types import MODEL_CLASS_FABLE, WINDOW_KEY_7D

FORECAST_VERSION: Final[int] = 1

#: Default look-back: one weekly cycle, so a weekday-heavy week is averaged in full.
DEFAULT_WINDOW_S: Final[float] = 7 * 86400.0

#: Provider -> demand group. Providers not listed carry no measurable demand (Cursor
#: publishes no windows).
GROUP_FOR_PROVIDER: Final[Mapping[str, str]] = {
    "claude": "claude",
    "codex": "codex",
    "antigravity": "antigravity",
}

_HOUR_S: Final[float] = 3600.0


# ======================================================================================
# Values
# ======================================================================================


@dataclass(frozen=True, slots=True)
class StreamRate:
    """One demand stream: PSE per hour, and the share of it that is Fable work."""

    pse_per_h: float
    fable_share: float

    def to_dict(self) -> dict[str, float]:
        return {"pse_per_h": self.pse_per_h, "fable_share": self.fable_share}


@dataclass(frozen=True, slots=True)
class GroupForecast:
    """Demand of one provider group.

    Args:
        other: Work that reaches subscriptions only (``pick``, ``cl``, ``cdx``, library).
        run: Work that went through ``quotapick run``.
        coverage: Mean, over the group's accounts, of the share of hours in the window
            that hold at least one reading. Low coverage means the rate is guessed from
            a few sightings.
        accounts: Accounts that had a reading in the window.
        measured_pse: Total rise measured over the window, before any subtraction.
        manual_pse_per_h: Configured manual reserves, already subtracted from ``other``.
    """

    other: StreamRate
    run: StreamRate
    coverage: float
    accounts: tuple[str, ...]
    measured_pse: float
    manual_pse_per_h: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "other": self.other.to_dict(),
            "run": self.run.to_dict(),
            "coverage": self.coverage,
            "accounts": list(self.accounts),
            "measured_pse": self.measured_pse,
            "manual_pse_per_h": self.manual_pse_per_h,
        }


@dataclass(frozen=True, slots=True)
class Forecast:
    computed_at_s: float
    window_s: float
    groups: Mapping[str, GroupForecast] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": FORECAST_VERSION,
            "computed_at_s": self.computed_at_s,
            "window_s": self.window_s,
            "groups": {name: group.to_dict() for name, group in sorted(self.groups.items())},
            "warnings": list(self.warnings),
        }


def _number(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} is not a number: {value!r}")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{key} is not finite: {value!r}")
    return out


def _stream_from(data: Any, name: str) -> StreamRate:
    if not isinstance(data, Mapping):
        raise ValueError(f"stream {name} is not a table")
    return StreamRate(pse_per_h=_number(data, "pse_per_h"), fable_share=_number(data, "fable_share"))


def forecast_from_dict(data: Any) -> Forecast:
    """Parse a cached forecast. Raises ``ValueError`` naming what is wrong."""
    if not isinstance(data, Mapping):
        raise ValueError("top level is not an object")
    if data.get("v") != FORECAST_VERSION:
        raise ValueError(f"version {data.get('v')!r}, expected {FORECAST_VERSION}")
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, Mapping):
        raise ValueError("groups is not an object")
    groups: dict[str, GroupForecast] = {}
    for name, raw in raw_groups.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"group {name} is not an object")
        accounts = raw.get("accounts")
        if not isinstance(accounts, list) or not all(isinstance(a, str) for a in accounts):
            raise ValueError(f"group {name} accounts is not a list of ids")
        groups[str(name)] = GroupForecast(
            other=_stream_from(raw.get("other"), "other"),
            run=_stream_from(raw.get("run"), "run"),
            coverage=_number(raw, "coverage"),
            accounts=tuple(accounts),
            measured_pse=_number(raw, "measured_pse"),
            manual_pse_per_h=_number(raw, "manual_pse_per_h"),
        )
    warnings = data.get("warnings", [])
    if not isinstance(warnings, list):
        raise ValueError("warnings is not a list")
    return Forecast(
        computed_at_s=_number(data, "computed_at_s"),
        window_s=_number(data, "window_s"),
        groups=groups,
        warnings=tuple(str(w) for w in warnings),
    )


# ======================================================================================
# Measuring
# ======================================================================================


@dataclass(slots=True)
class _Tally:
    measured_pse: float = 0.0
    fable_pse: float = 0.0
    coverage: list[float] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    manual_pse_per_h: float = 0.0
    run_total_pse: float = 0.0
    run_subscription_pse: float = 0.0
    run_fable_pse: float = 0.0


def _fable_window(reading: Any) -> Any:
    return next(
        (w for w in reading.windows.values() if MODEL_CLASS_FABLE in w.applies_to), None
    )


def _rise(
    high_water: float | None,
    window: Any,
    later: Any,
    before_s: float,
    after_s: float,
) -> tuple[float, float]:
    """Used fraction added between two readings of one window, and the new high-water."""
    transition = waste.window_transition(window, later, before_s, after_s)
    if transition == waste.TRANSITION_BACKWARDS:
        return 0.0, later.used
    if transition == waste.TRANSITION_ROLLED:
        return max(0.0, later.used), later.used
    mark = window.used if high_water is None else max(high_water, window.used)
    return max(0.0, later.used - mark), max(mark, later.used)


def _coverage(times: list[float], start_s: float, end_s: float) -> float:
    hours = max(1, int(math.ceil((end_s - start_s) / _HOUR_S - 1e-9)))
    seen = {int((t - start_s) // _HOUR_S) for t in times if start_s <= t < end_s}
    return len(seen) / hours


def measure(
    records: Iterable[Mapping[str, Any]],
    *,
    plans: Mapping[str, Plan],
    now_s: float,
    window_s: float = DEFAULT_WINDOW_S,
    manual_rates: Mapping[str, float],
) -> Forecast:
    """Measure demand per group over ``[now_s - window_s, now_s]``.

    Args:
        records: History records. Records outside the window are ignored, so callers may
            pass a superset.
        plans: Per-account ``k`` from :func:`quota_router.scoring.plan_for`; absent
            accounts use the built-in :class:`~quota_router.pse.Plan`.
        now_s: End of the window. The caller's clock, never read here.
        window_s: Look-back.
        manual_rates: ``manual_rate_per_day`` per account, a fraction of the weekly pool.
    """
    if window_s <= 0:
        raise ValueError(f"window_s must be positive, got {window_s}")
    start_s = now_s - window_s
    hours = window_s / _HOUR_S
    materialized = list(records)
    tallies: dict[str, _Tally] = {}
    warnings: list[str] = []

    for account_id, readings in sorted(waste.readings_by_account(materialized).items()):
        deduped = []
        for reading in readings:
            if deduped and reading.observed_at_s <= deduped[-1].observed_at_s:
                deduped[-1] = reading
            else:
                deduped.append(reading)
        provider = next((r.provider for r in reversed(deduped) if r.provider), None)
        group = GROUP_FOR_PROVIDER.get(provider or "")
        if group is None:
            continue
        inside = [r.observed_at_s for r in deduped if start_s <= r.observed_at_s <= now_s]
        if not inside:
            warnings.append(
                f"{account_id}: no reading in the forecast window ({hours:.0f}h); "
                f"its demand is missing from the {group} forecast"
            )
            continue
        plan = plans.get(account_id) or Plan()
        tally = tallies.setdefault(group, _Tally())
        tally.accounts.append(account_id)
        tally.coverage.append(_coverage(inside, start_s, now_s))
        latest_scale = deduped[-1].tier_scale
        rate = manual_rates.get(account_id)
        if rate:
            tally.manual_pse_per_h += rate * plan.weekly_to_session * latest_scale / 24.0

        weekly_mark: float | None = None
        fable_mark: float | None = None
        for before, after in zip(deduped, deduped[1:]):
            span = after.observed_at_s - before.observed_at_s
            overlap = min(after.observed_at_s, now_s) - max(before.observed_at_s, start_s)
            weekly, later = before.windows.get(WINDOW_KEY_7D), after.windows.get(WINDOW_KEY_7D)
            if weekly is None or later is None:
                weekly_mark = None
                continue
            used, weekly_mark = _rise(
                weekly_mark, weekly, later, before.observed_at_s, after.observed_at_s
            )
            fable_used = 0.0
            fb, fa = _fable_window(before), _fable_window(after)
            if fb is not None and fa is not None:
                fable_used, fable_mark = _rise(
                    fable_mark, fb, fa, before.observed_at_s, after.observed_at_s
                )
            if span <= 0 or overlap <= 0:
                continue
            share = overlap / span
            cap = plan.weekly_to_session * after.tier_scale
            tally.measured_pse += used * cap * share
            tally.fable_pse += fable_used * plan.fable_fraction * cap * share

    for record in materialized:
        if record.get("event") != "run":
            continue
        t = record.get("t")
        run = record.get("run")
        if not isinstance(t, (int, float)) or not isinstance(run, Mapping):
            continue
        if not (start_s <= float(t) <= now_s):
            continue
        cost = run.get("cost_pse")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            continue
        group = str(run.get("group") or "claude")
        tally = tallies.setdefault(group, _Tally())
        tally.run_total_pse += float(cost)
        if run.get("pool_kind") == "subscription":
            tally.run_subscription_pse += float(cost)
        if run.get("fable") is True:
            tally.run_fable_pse += float(cost)

    groups: dict[str, GroupForecast] = {}
    for group, tally in sorted(tallies.items()):
        other_rate = max(
            0.0, (tally.measured_pse - tally.run_subscription_pse) / hours - tally.manual_pse_per_h
        )
        groups[group] = GroupForecast(
            other=StreamRate(
                pse_per_h=other_rate,
                fable_share=(
                    min(1.0, max(0.0, tally.fable_pse / tally.measured_pse))
                    if tally.measured_pse > 0
                    else 0.0
                ),
            ),
            run=StreamRate(
                pse_per_h=tally.run_total_pse / hours,
                fable_share=(
                    tally.run_fable_pse / tally.run_total_pse if tally.run_total_pse > 0 else 0.0
                ),
            ),
            coverage=(sum(tally.coverage) / len(tally.coverage)) if tally.coverage else 0.0,
            accounts=tuple(tally.accounts),
            measured_pse=tally.measured_pse,
            manual_pse_per_h=tally.manual_pse_per_h,
        )
    return Forecast(
        computed_at_s=now_s, window_s=window_s, groups=groups, warnings=tuple(warnings)
    )


# ======================================================================================
# The cache
# ======================================================================================


def forecast_path(env: Mapping[str, str] | None = None) -> Path:
    """``$QUOTA_ROUTER_FORECAST`` (a directory means "in here"), else beside history."""
    environ = os.environ if env is None else env
    override = (environ.get("QUOTA_ROUTER_FORECAST") or "").strip()
    if override:
        path = Path(os.path.expanduser(override))
        return path / "forecast.json" if path.is_dir() else path
    return history_path(environ).parent / "forecast.json"


def write_cache(result: Forecast, *, env: Mapping[str, str] | None = None) -> Path:
    """Write atomically: a reader sees the old file or the new one, never a torn one."""
    path = forecast_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(result.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def read_cache(*, env: Mapping[str, str] | None = None) -> tuple[Forecast | None, str | None]:
    """The cached forecast, or ``(None, why not)``. Never a silent empty forecast."""
    path = forecast_path(env)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"no forecast at {path} yet (`quotapick status` writes it)"
    except OSError as exc:
        return None, f"forecast at {path} unreadable: {exc}"
    try:
        return forecast_from_dict(json.loads(text)), None
    except json.JSONDecodeError as exc:
        return None, f"forecast at {path} unreadable: {exc}"
    except ValueError as exc:
        return None, f"forecast at {path} rejected: {exc}"


@dataclass(frozen=True, slots=True)
class Refresh:
    refreshed: bool
    forecast: Forecast | None
    warnings: tuple[str, ...] = ()


def refresh_if_due(
    load_records: Callable[[], Iterable[Mapping[str, Any]]],
    *,
    now_s: float,
    plans: Mapping[str, Plan],
    manual_rates: Mapping[str, float],
    window_s: float,
    refresh_s: float,
    env: Mapping[str, str] | None = None,
) -> Refresh:
    """Recompute and rewrite the cache when it is missing, stale, or from the future.

    A cache stamped later than ``now_s`` came from another clock (a ``--now`` probe) and
    would otherwise never be refreshed until the real clock caught up, so it is replaced.
    """
    cached, _problem = read_cache(env=env)
    if cached is not None:
        age = now_s - cached.computed_at_s
        if 0 <= age < refresh_s:
            return Refresh(refreshed=False, forecast=cached)
    result = measure(
        load_records(), plans=plans, now_s=now_s, window_s=window_s, manual_rates=manual_rates
    )
    try:
        write_cache(result, env=env)
    except OSError as exc:
        return Refresh(
            refreshed=False,
            forecast=result,
            warnings=(f"cannot write the demand forecast: {exc}",),
        )
    return Refresh(refreshed=True, forecast=result, warnings=result.warnings)
