#!/usr/bin/env python3
"""Read-only reanalysis of the seven 2026-10-12 Gemini reps (BACKLOG #24).

The raw directory is deliberately an argument: it contains private identities and
is not committed. JSON output contains account ids, never raw responses or paths.
Uses the two-chain assignment and jump thresholds in 2026-10-18 analyze5.py.
No provider calls, operator config, state writes, or installation are performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import statistics

USD_PER_PSE = 283.20
PRICES = {"input_tokens": 0.75e-6, "cache_read_tokens": 0.075e-6,
          "output_tokens": 3.75e-6, "thinking_tokens": 3.75e-6}
JUMP_W, JUMP_H = 0.0005, 0.002
FLASH = "gemini-3.8-flash-high"
PRO = "gemini-3.1-pro-high"
POOLS = {"antigravity_gemini": ["k-g1", "k-g2", "k-g3", "k-g4"],
         "antigravity_gemini_b": ["k-gb1", "k-gb2", "k-gb3"]}
FLASH_REPS = {"antigravity_gemini": {"k-g3", "k-g4"},
              "antigravity_gemini_b": {"k-gb2", "k-gb3"}}


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def price(usage: dict) -> float:
    for key in PRICES:
        value = usage[key]
        if type(value) is not int or value < 0:
            raise ValueError(f"invalid token count: {key}={value!r}")
    return sum(usage[key] * rate for key, rate in PRICES.items())


def load_pool(root: Path, pool: str) -> tuple[list, list, list]:
    points, calls, reps = [], [], []
    for rep in POOLS[pool]:
        d = root / "reps" / "k" / pool / rep
        metas = [load(f) for f in sorted(d.glob("calls/*/meta.json"))]
        if not metas:
            raise ValueError(f"{pool}/{rep}: no call metadata")
        complete = (d / "rep.json").exists()
        if complete:
            record = load(d / "rep.json")
            if record["status"] != "complete":
                raise ValueError(f"{pool}/{rep}: incomplete rep")
            if set(record["calls"]) != {m["key"] for m in metas}:
                raise ValueError(f"{pool}/{rep}: call manifest mismatch")
        elif rep != "k-g2":
            raise ValueError(f"{pool}/{rep}: missing rep.json")
        failures = Counter()
        identity = Counter()
        attempts = []
        for meta in metas:
            if meta["pool"] != pool or meta["rep"] != rep:
                raise ValueError(f"{pool}/{rep}: metadata identity mismatch")
            model = meta["model"]
            if model not in {FLASH, PRO}:
                raise ValueError(f"unexpected requested model: {model}")
            for attempt in meta["attempts"]:
                attempts.append(attempt)
                response = load(d / "calls" / meta["key"] / f"response.attempt{attempt['attempt']}.json")
                good = attempt["status"] == "ok"
                if good != (response["status"] == "SUCCESS"):
                    raise ValueError(f"{pool}/{rep}: response/metadata status mismatch")
                usd = price(response["usage"]) if good and model == FLASH else 0.0
                if good and model == FLASH:
                    if pool == "antigravity_gemini":
                        prov = load(d / "calls" / meta["key"] / f"provenance.attempt{attempt['attempt']}.json")
                        if prov["conversation_id"] != response["conversation_id"] or FLASH not in prov["requested"]:
                            raise ValueError("requested model/conversation provenance mismatch")
                        if not prov["steps"] or any(s["model_enum"] != 1318 or s["response_id_kind"] not in {"google-style", None} for s in prov["steps"]):
                            raise ValueError("unexpected served model telemetry")
                        identity["requested_flash_served_enum_1318_google_backend" if all(s["response_id_kind"] == "google-style" for s in prov["steps"]) else "requested_flash_served_enum_1318_empty_response_id"] += 1
                    else:
                        served = attempt["served"]
                        if served["labels"] != ["Gemini 3.8 Flash (High)"] or not served["google_ids"] or served["vertex_ids"]:
                            raise ValueError("unexpected served model log telemetry")
                        identity["flash_label_google_backend_store_unavailable"] += 1
                if not good:
                    # The raw error text has the useful category; metadata only says status ERROR.
                    error = response.get("error", response.get("response", ""))
                    if "not recognized" not in str(error):
                        raise ValueError(f"unclassified call failure: {error!r}")
                    failures["model_not_recognized"] += 1
                calls.append({"rep": rep, "key": meta["key"], "model": model,
                              "start": attempt["start_s"], "end": attempt["end_s"],
                              "succeeded": good, "usd": usd})
        raw_files = set(d.glob("usage/*-raw.json"))
        referenced = set()
        for p in sorted(d.glob("usage/*-point-*.json")):
            pt = load(p)
            pairs = set()
            for sub in pt["subreadings"]:
                path = d / "usage" / sub["raw"]
                referenced.add(path)
                raw = load(path)
                if raw["status"] != "SUCCESS" or raw["command"]["name"] != "usage":
                    raise ValueError("unexpected usage response")
                groups = [g for g in raw["command"]["data"]["groups"] if g["name"] == "Gemini Models"]
                if len(groups) != 1:
                    raise ValueError("missing or repeated Gemini group")
                buckets = {b["window"]: b for b in groups[0]["buckets"]}
                saved = sub["groups"]["Gemini Models"]
                for window in ["weekly", "5h"]:
                    if buckets[window]["remaining_fraction"] != saved[f"{window}_remaining"] or buckets[window]["reset_time"] != saved[f"{window}_reset"]:
                        raise ValueError("raw usage disagrees with derived point")
                pairs.add((saved["weekly_remaining"], saved["5h_remaining"]))
            points.append({"rep": rep, "label": pt["label"], "t": pt["t_first"], "pairs": sorted(pairs)})
        unreferenced = sorted(raw_files - referenced)
        raw_failures = Counter()
        orphan_successes = 0
        for path in unreferenced:
            raw = load(path)
            if raw["status"] == "SUCCESS" and rep == "k-g2":
                orphan_successes += 1  # interrupted rep: no completed point owns these reads
            elif raw["status"] == "ERROR":
                raw_failures["usage_error"] += 1
            else:
                raise ValueError(f"unclassified unreferenced read: {path.name}")
        picks = load(d / "history.json")["picks"] if complete else None
        routed = [p for p in picks or [] if p["chosen"] in POOLS]
        if routed:
            raise ValueError(f"{rep}: another router pick used a Gemini pool")
        reps.append({"rep": rep, "complete": complete,
                     "start": iso(min(a["start_s"] for a in attempts)),
                     "end": iso(max(a["end_s"] for a in attempts)),
                     "attempted": len(attempts), "succeeded": sum(a["status"] == "ok" for a in attempts),
                     "failed": sum(failures.values()), "failed_by_kind": dict(failures),
                     "model_identity": dict(identity),
                     "usage_reads": {"attempted": len(raw_files), "succeeded": len(referenced) + orphan_successes,
                                     "failed": sum(raw_failures.values()), "failed_by_kind": dict(raw_failures),
                                     "unassigned_partial_point_successes": orphan_successes},
                     "saved_router_picks": None if picks is None else len(picks),
                     "saved_router_gemini_picks": None if picks is None else len(routed)})
    points.sort(key=lambda p: p["t"])
    return points, calls, reps


def track(points: list) -> tuple[list, list]:
    """Exactly analyze5's L1 assignment; report any unassigned third pair."""
    first = points[0]["pairs"]
    chains = [[(points[0], first[-1])], [(points[0], first[0])]]
    omitted = ([{"at": iso(points[0]["t"]), "rep": points[0]["rep"], "pairs_omitted": len(first) - 2}]
               if len(first) > 2 else [])
    for point in points[1:]:
        vals = point["pairs"]
        choices = []
        for perm in itertools.permutations(range(2), min(2, len(vals))):
            for chosen in itertools.combinations(vals, len(perm)):
                cost = sum(abs(chains[c][-1][1][0] - v[0]) + abs(chains[c][-1][1][1] - v[1]) for c, v in zip(perm, chosen))
                choices.append((cost, perm, chosen))
        _, perm, chosen = min(choices, key=lambda choice: choice[0])
        for c, value in zip(perm, chosen):
            chains[c].append((point, value))
        if len(vals) > 2:
            omitted.append({"at": iso(point["t"]), "rep": point["rep"], "pairs_omitted": len(vals) - 2})
    return chains, omitted


def intervals(chain: list, calls: list, first_flash: float) -> list:
    out = []
    for (p0, v0), (p1, v1) in zip(chain, chain[1:]):
        contained = [c for c in calls if p0["t"] < c["end"] <= p1["t"]]
        # An attempt overlapping any part of the interval can have moved its meter,
        # including a failed request. Exclude both dollars and movement together.
        pro = [c for c in calls if c["model"] == PRO and c["start"] < p1["t"] and c["end"] > p0["t"]]
        reason = "pro_overlap" if pro else "before_flash_span" if p0["t"] < first_flash else None
        out.append({"from": iso(p0["t"]), "to": iso(p1["t"]), "rep": p1["rep"],
                    "from_rep": p0["rep"], "dw": v0[0] - v1[0], "dh": v0[1] - v1[1],
                    "flash_calls": sum(c["model"] == FLASH and c["succeeded"] for c in contained),
                    "pro_attempts_overlapping": len(pro), "usd": sum(c["usd"] for c in contained),
                    "excluded": reason})
    return out


def summarize(intervals: list) -> dict:
    dw, dh, usd = (sum(x[key] for x in intervals) for key in ("dw", "dh", "usd"))
    return {"intervals": len(intervals), "dw": dw, "dh": dh, "usd": usd,
            "call_chain_attributions": sum(x["flash_calls"] for x in intervals),
            "weekly_pse": usd / dw / USD_PER_PSE if dw > 0 else None,
            "k": dh / dw if dw > 0 else None}


def analyze(root: Path, pool: str) -> dict:
    points, calls, reps = load_pool(root, pool)
    chains, omitted = track(points)
    first_flash = min(p["t"] for p in points if p["rep"] in FLASH_REPS[pool])
    out = {"pool": pool, "reps": reps, "chain_assignment_omissions": omitted, "chains": [],
           "flash_calls": sum(c["model"] == FLASH and c["succeeded"] for c in calls),
           "flash_usd": sum(c["usd"] for c in calls),
           "usage_span": {"start": iso(points[0]["t"]), "end": iso(points[-1]["t"])}}
    all_fine = []
    for ci, chain in enumerate(chains):
        ivs = intervals(chain, calls, first_flash)
        paid = [x for x in ivs if x["excluded"] is None and x["usd"] > 0]
        rw, rh = (statistics.median(x[key] / x["usd"] for x in paid) for key in ("dw", "dh"))
        for x in ivs:
            x["res_w"], x["res_h"] = x["dw"] - rw * x["usd"], x["dh"] - rh * x["usd"]
            if x["excluded"] is None and (abs(x["res_w"]) > JUMP_W or abs(x["res_h"]) > JUMP_H):
                x["excluded"] = "jump_residual"
        fine = [x for x in ivs if x["excluded"] is None]
        all_fine.extend(fine)
        excluded = Counter(x["excluded"] for x in ivs if x["excluded"] is not None)
        out["chains"].append({"chain": ci, "attempted_intervals": len(ivs),
                              "succeeded_intervals": len(fine), "excluded_intervals": dict(excluded),
                              "fine": summarize(fine), "intervals": ivs})
    out["pooled_fine"] = summarize(all_fine)
    out["per_rep_fine"] = {rep: summarize([x for x in all_fine if x["rep"] == rep]) for rep in sorted(FLASH_REPS[pool])}
    # No arbitrary waiting period is presented as a proven settlement cutoff.
    # Drop the whole first Flash rep, including its carryover into the next, as sensitivity.
    last = "k-g4" if pool == "antigravity_gemini" else "k-gb3"
    out["last_rep_only_sensitivity"] = summarize([x for x in all_fine if x["rep"] == last and x["from_rep"] == last])
    out["no_call_fine_movements"] = [x for x in all_fine if not x["flash_calls"] and (x["dw"] != 0 or x["dh"] != 0)]
    out["model_identity_limit"] = ("Served enum 1318; 78 Google response ids and 2 empty ids. Saved evidence does not independently map enum to model name."
                                    if pool == "antigravity_gemini" else
                                    "CLI served label and Google response ids; independent conversation store was unreadable.")
    return out


def ledger_measurements(pools: list, recorded_at: str) -> list:
    instant = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant.utcoffset().total_seconds() != 0:
        raise ValueError("recorded_at must be explicit UTC")
    rows = []
    for pool in pools:
        account = pool["pool"]
        flash_reps = [r for r in pool["reps"] if r["rep"] in FLASH_REPS[account]]
        spread = [c["fine"]["weekly_pse"] for c in pool["chains"]]
        rows.append({
            "version": 1, "id": f"measurement-{account}-oct12-fine-weekly",
            "kind": "measurement", "recorded_at": recorded_at, "reconstructed": True,
            "evidence_at": pool["usage_span"]["end"], "scope": "account", "account": account,
            "key": "weekly_pool", "value": {"weekly_to_session": 15.0,
                "window_size": pool["pooled_fine"]["weekly_pse"] / 15.0},
            "spread": None, "started_at": min(r["start"] for r in flash_reps),
            "ended_at": max(r["end"] for r in flash_reps),
            "method": "Total Flash dollars / total fine weekly movement over both chains, using the Oct18 analyze5 method. Entire Pro-overlapping intervals excluded. The window_size is this fine weekly estimate / k=15, not a settled five-hour measurement.",
            "evidence": ["docs/measurements/2026-10-19-gemini-reanalysis.md#results",
                         "measurement-folder:agy-capacity-2026-10-12",
                         "measurement-folder:gemini-size-2026-10-18/analyze5.py",
                         "ops/reanalyze-gemini-oct12.py"],
            "caveats": [pool["model_identity_limit"],
                        "Meter not settled; compare only with Oct18 fine figures, not its settled figures.",
                        f"Per-chain weekly PSE range {min(spread):.9f} to {max(spread):.9f}; this is descriptive spread, not a confidence interval.",
                        "No Gemini router picks in saved completed-rep histories. Direct CLI/web usage cannot be completely audited.",
                        "Late Pro charges cannot be ruled out by a quiet pre-Flash period. Excluding the first Flash rep is a sensitivity check, not a proven settlement cutoff.",
                        "Both candidate historical beliefs remain unresolved for the operator.",
                        "Prices and USD/PSE inherit the documented Oct18 2026 schedule and Oct12 Claude reference; their uncertainties remain."],
            "corrects": [], "disputes": []})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measurement_directory", type=Path)
    parser.add_argument("--recorded-at", required=True, help="Explicit UTC writing time for proposed ledger measurements; never inferred from raw file timestamps")
    args = parser.parse_args()
    result = {"evidence_date": "2026-10-12", "reference_method": "gemini-size-2026-10-18/analyze5.py",
              "inputs": {"usd_per_pse": USD_PER_PSE, "prices_per_token": PRICES, "jump_w": JUMP_W, "jump_h": JUMP_H},
              "pools": [analyze(args.measurement_directory, p) for p in POOLS],
              "other_usage_limit": "Saved per-rep router histories are checked for Gemini picks. They cannot exclude direct CLI or web use; no complete independent activity audit survives.",
              "belief": "unresolved; this measurement does not choose an operator belief"}
    result["ledger_measurements"] = ledger_measurements(result["pools"], args.recorded_at)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
