#!/usr/bin/env python3
"""Offline, deterministic experiment for BACKLOG #25; no operator state is read.

Run: PYTHONPATH=src .venv/bin/python ops/replay-demand-allocation.py > result.json
The two frozen rank adapters use production account_score/select/simulate while
changing only how the supplied group rate is allocated. They retain old semantics
after production rank gains allocation. The proposed rule also checks production
rank's rates, scores and ordering on every eligible field. Every monkeypatch is
restored in finally.
"""

from __future__ import annotations

import importlib
import json
import math
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import replace

from quota_router import replay, scoring
from quota_router.pse import Plan

selection = importlib.import_module("quota_router.select")
HOUR = 3600.0
DAY = 24 * HOUR
WEEK = 7 * DAY
START = 1_792_368_000.0  # Fixed synthetic epoch, not the machine's clock.
RULES = ("whole_group", "proportional_remaining")


def ranked(rule, snapshots, now_s, cfg=None, model_class=None, *, joint=None):
    """Frozen allocation rule; select supplies its eligible candidate set."""
    candidates = tuple(snapshots)
    regime = scoring.decide_regime(candidates, now_s, model_class)
    stocks = {
        s.id: scoring.stocks_from_snapshot(
            s, now_s, s.capacity, scoring.plan_for(cfg, s.id)
        )
        for s in candidates
    }
    allocatable = {
        s.id for s in candidates
        if s.available and scoring.fits(s, model_class)
        and stocks[s.id] is not None and stocks[s.id].weekly_remaining > 0.0
    }
    totals = defaultdict(float)
    for s in candidates:
        if s.id in allocatable:
            totals[s.provider] += stocks[s.id].weekly_remaining
    rows = []
    for s in candidates:
        rate = min(scoring.rate_for(cfg, s.provider), scoring.MAX_SUSTAINED_RATE_PSE_PER_HOUR)
        if rule == "proportional_remaining" and s.id in allocatable:
            total = totals[s.provider]
            rate *= stocks[s.id].weekly_remaining / total if total else 0.0
        # A scalar is deliberate: bypass any group allocation added to rank later.
        local_cfg = {**(cfg or {}), "rate_pse_per_hour": rate}
        rows.append(scoring.account_score(s, now_s, local_cfg, model_class, regime))
    if joint is not None:
        rows = list(joint.rescore(rows))
    rows.sort(key=scoring.ranking_sort_key)
    if rule == "proportional_remaining":
        production = scoring.rank(candidates, now_s, cfg, model_class, joint=joint)
        assert [row.account_id for row in production] == [row.account_id for row in rows], (
            "production and experimental allocation disagree on ranking"
        )
        for actual, expected in zip(production, rows):
            assert math.isclose(actual.score, expected.score, rel_tol=1e-12, abs_tol=1e-12), (
                f"production score disagrees for {actual.account_id}"
            )
            assert math.isclose(actual.rate_pse_per_hour, expected.rate_pse_per_hour,
                                rel_tol=1e-12, abs_tol=1e-12), (
                f"production applied rate disagrees for {actual.account_id}"
            )
    return rows


def prediction(snapshots, now_s, cfg):
    """Both rules evaluated on exactly the same pool state."""
    out = {}
    for rule in RULES:
        rows = {row.account_id: row for row in ranked(rule, snapshots, now_s, cfg)}
        out[rule] = {}
        for snap in snapshots:
            stock = scoring.stocks_from_snapshot(
                snap, now_s, snap.capacity, scoring.plan_for(cfg, snap.id)
            )
            assert stock is not None
            rate = rows[snap.id].rate_pse_per_hour
            out[rule][snap.id] = {
                "remaining_pse": stock.weekly_remaining,
                "hours_to_reset": (stock.weekly_reset_s - now_s) / HOUR,
                "rate_pse_per_hour": rate,
                "predicted_waste_pse": scoring.wasted_pse(
                    stock, now_s=now_s, rate_pse_per_hour=rate
                ),
            }
    return out


def pool(name, *, provider, capacity, k, reset_hours=168, used=0.01):
    return replay.PoolSpec(
        id=name, provider=provider, tier="max_20x", tier_scale=capacity,
        plan=Plan(weekly_to_session=k),
        events=(replay.ResetEvent(START + reset_hours * HOUR,
                                  START + reset_hours * HOUR, False, False),),
        plan_end_s=None, start_used=used, start_fable_used=None, has_fable=False,
    )


def trace_for(hourly_demand):
    # A complete prior week supplies a causal trailing forecast. This is generated
    # demand, not a claim about the operator's observed consumption.
    demand = tuple(hourly_demand(h % 168) for h in range(336))
    return replay.Trace(
        start_s=START - WEEK, bin_s=HOUR, group_pse=demand,
        group_fable_pse=(0.0,) * len(demand), by_account={}, by_account_fable={},
    )


@contextmanager
def instrument(rule, specs, trace, scale):
    old_rank, old_take, old_settle = selection.rank, replay._take, replay._settle
    served_by_bin = defaultdict(float)
    successes_by_bin = defaultdict(int)
    resets = []
    daily_predictions = {}
    day_snapshots = defaultdict(list)

    def rank(*args, **kwargs):
        return ranked(rule, *args, **kwargs)

    def take(p, amount, fable, t):
        index = int(round((t - START) / HOUR))
        served_by_bin[index] += amount
        successes_by_bin[index] += 1
        return old_take(p, amount, fable, t)

    def settle(p, t, count_from_s, sink):
        if p.roll_s <= t and p.roll_s >= START:
            resets.append({"account": p.spec.id, "hour": (p.roll_s - START) / HOUR,
                           "waste_pse": p.weekly_rem})
        old_settle(p, t, count_from_s, sink)
        hour = int(round((t - START) / HOUR))
        if 0 <= hour < 168 and hour % 24 == 0:
            snaps = day_snapshots[hour]
            snaps.append(replay._snapshot(p, t))
            if len(snaps) == len(specs):
                index = int(round((t - trace.start_s) / HOUR))
                rate, _ = replay._trailing_rate(trace, index, 168, scale)
                daily_predictions[hour // 24 + 1] = prediction(
                    snaps, t, replay._cfg(specs, rate)
                )

    selection.rank, replay._take, replay._settle = rank, take, settle
    try:
        yield served_by_bin, successes_by_bin, resets, daily_predictions
    finally:
        selection.rank, replay._take, replay._settle = old_rank, old_take, old_settle


def run_case(specs, trace, *, rule, group, job_pse=0.01, scale=1.0, include_predictions=True):
    with instrument(rule, specs, trace, scale) as observed:
        result = replay.simulate(
            specs, trace, policy="own_waste_measured_rate", count_from_s=START,
            end_s=START + WEEK, job_pse=job_pse, forecast_scale=scale, group=group,
        )
    served, succeeded, resets, predictions = observed
    days = []
    for day in range(7):
        hours = range(day * 24, (day + 1) * 24)
        unmet = {h: max(0.0, trace.group_pse[168 + h] - served[h]) for h in hours}
        # A failed request is a job-sized chunk of demand the simulator could not
        # serve. Partial successful takes remain successes; remainder becomes failure.
        failed = sum(math.ceil(max(0.0, amount - 1e-9) / job_pse) for amount in unmet.values())
        success = sum(succeeded[h] for h in hours)
        row = {"day": day + 1, "attempted": success + failed,
               "succeeded": success, "failed": failed,
               "served_pse": sum(served[h] for h in hours),
               "unmet_pse": sum(unmet.values())}
        if include_predictions:
            row["same_state_prediction"] = predictions[day + 1]
        days.append(row)
    assert sum(d["succeeded"] for d in days) == result.jobs
    assert math.isclose(sum(d["served_pse"] for d in days), result.served_pse, abs_tol=1e-8)
    assert math.isclose(sum(d["unmet_pse"] for d in days), result.unmet_pse, abs_tol=1e-8)
    assert math.isclose(sum(r["waste_pse"] for r in resets), result.waste_pse, abs_tol=1e-8)
    assert all(d["attempted"] == d["succeeded"] + d["failed"] for d in days)
    return {"rule": rule, "job_pse": job_pse, "forecast_scale": scale,
            "result": result.to_dict(), "daily": days, "reset_waste": resets}


def main():
    gemini = [pool("antigravity_gemini", provider="antigravity", capacity=2.43, k=4),
              pool("antigravity_gemini_b", provider="antigravity", capacity=2.45, k=4)]
    gemini_trace = trace_for(lambda _: 0.07)
    anchor = [replace(s, events=(replace(s.events[0], roll_s=START + 144 * HOUR,
                                        visible_s=START + 144 * HOUR),)) for s in gemini]
    anchor_snaps = [replay._snapshot(replay._start(s, START), START) for s in anchor]
    claude = [pool(f"claude_{i}", provider="claude", capacity=1.0, k=6.25,
                   reset_hours=24 + i * 48, used=0.20) for i in range(4)]
    # Daily three-hour bursts bind the five-hour cap on the tighter fleet. Removing
    # a pool leaves this identical trace, as required by the #3 comparison design.
    claude_trace = trace_for(lambda h: 1.1 if h % 24 < 3 else 0.0)
    result = {
        "schema": 1, "kind": "offline_synthetic_counterfactual",
        "scenario": {"duration_days": 7, "warmup_days": 7, "bin_hours": 1,
                     "gemini_rate_pse_per_hour": 0.07, "initial_used_fraction": 0.01,
                     "gemini_weekly_cap_pse": {s.id: s.weekly_cap for s in gemini}},
        "limitations": [
            "Synthetic demand and deadlines, not an observed historical replay or #3 gate.",
            "Fixed weekly resets; no claim about Antigravity's sliding/early resets.",
            "One Gemini demand group; model-specific grouping is outside this measurement.",
            "Failure counts are unserved job-sized demand chunks; no network requests occur.",
            "Production selection, hysteresis, five-hour caps, 2% floor, and replay are retained.",
            "Tail uses the production neutral projection; compare waste and unmet separately.",
        ],
        "anchor_at_144_hours": prediction(anchor_snaps, START, replay._cfg(anchor, 0.07)),
        "gemini": [run_case(gemini, gemini_trace, rule=r, group="antigravity") for r in RULES],
        "claude_synthetic_sensitivity": [
            {"fleet": name, **run_case(specs, claude_trace, rule=r, group="claude",
                                       job_pse=job, scale=scale, include_predictions=False)}
            for name, specs in (("full", claude), ("one_20x_removed", claude[1:]))
            for job in (0.01, 0.05) for scale in (0.5, 2.0) for r in RULES
        ],
    }
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
