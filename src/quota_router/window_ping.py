"""Start idle subscription windows from the poller's recorded observations.

No usage fetch, credential access, schedule or recent-run heuristic lives here.
The append-only journal is the cooldown/verification source of truth. A lock spans
planning, write-ahead attempt, CLI call and result; a corrupt journal fails closed.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import time
import uuid
from collections import Counter
from pathlib import Path

from .config import Config
from .types import AccountSnapshot, NotRunningWindow, Window

WAIT_TOLERANCE_S = 240.0  # 120s poll plus the measured offsets (rarely 97s).
MAX_READING_AGE_S = 600.0  # Five poll intervals; never wake from an hours-old cache.
FAILURE_BACKOFF_S = 1800.0
HISTORY_LOOKBACK_S = 8 * 86400.0  # One full weekly predecessor plus a day.


def waiting_reason(window, previous):
    """One provider-independent test, measured against the observation's own clock."""
    if isinstance(window, NotRunningWindow):
        return "provider_says_not_running"
    if window.applies_to or window.used_fraction != 0:
        return None
    if abs(window.resets_at_s - window.observed_at_s - window.length_s) <= WAIT_TOLERANCE_S:
        return "reset_near_observation_plus_length"
    # Exact next boundary, not the tolerance: do not confuse a slowly sliding reset
    # with a scheduled reset. The prior observation must actually precede that reset.
    if any(w.key == window.key and not w.applies_to and w.length_s == window.length_s
           and w.observed_at_s < w.resets_at_s <= window.observed_at_s
           and w.resets_at_s + window.length_s == window.resets_at_s for w in previous):
        return "previous_reset_plus_length"
    return None


def _decode(raw):
    # No permissive history parser: a malformed reading must not start calls.
    for key in ("id", "provider", "source"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise ValueError(f"invalid history {key}")
    if type(raw["available"]) is not bool:
        raise ValueError("invalid history available")
    def number(value, key):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"invalid history {key}")
    number(raw["confidence"], "confidence")
    for collection in ("windows", "not_running"):
        entries = raw[collection] if collection == "windows" else raw.get(collection, [])
        if not isinstance(entries, list):
            raise ValueError(f"invalid history {collection}")
        for w in entries:
            if not isinstance(w["key"], str) or not w["key"].strip():
                raise ValueError("invalid history window key")
            fields = ("length_s", "observed_at_s")
            if collection == "windows":
                fields += ("used_fraction", "resets_at_s")
                scope = w.get("applies_to")
                if scope is not None and (not isinstance(scope, list) or
                        any(not isinstance(c, str) or not c.strip() for c in scope)):
                    raise ValueError("invalid history applies_to")
            for key in fields:
                number(w[key], key)
            for key in ("expected_used_fraction", "held_fraction", "reserved_fraction"):
                if key in w and w[key] is not None:
                    number(w[key], key)
    windows = tuple(Window(**{k: v for k, v in w.items() if k in Window.__dataclass_fields__})
                    for w in raw["windows"])
    idle = tuple(NotRunningWindow(**w) for w in raw.get("not_running", []))
    return AccountSnapshot(id=raw["id"], provider=raw["provider"], windows=windows,
        not_running=idle, available=raw["available"], confidence=raw["confidence"],
        source=raw["source"])


def _snapshots(records):
    """Keep the latest status attempt (even a failed read), and observed predecessors."""
    latest, previous = {}, {}
    for record in sorted(records, key=lambda r: r["t"]):
        if record.get("event") != "status":
            continue
        for raw in record["accounts"]:
            s = _decode(raw)
            latest[s.id] = s
            previous.setdefault(s.id, []).extend(w for w in s.windows if s.available)
    return latest, previous


def _journal(path):
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    result = []
    for line in text.splitlines():
        e = json.loads(line)
        if e["event"] not in {"attempt", "result", "verification", "resume"}:
            raise ValueError("unknown ping journal event")
        if not isinstance(e["pool"], str) or not math.isfinite(e["at_s"]):
            raise ValueError("invalid ping journal identity/time")
        if e["event"] in {"attempt", "result"}:
            if not isinstance(e["attempt_id"], str) or not isinstance(e["windows"], list):
                raise ValueError("invalid ping attempt")
        if e["event"] == "result" and e["outcome"] not in {"succeeded", "failed"}:
            raise ValueError("invalid ping result")
        if e["event"] == "result":
            if not math.isfinite(e["started_at_s"]) or not isinstance(e["model_asked"], str):
                raise ValueError("invalid ping result time/model")
            if e["outcome"] == "succeeded":
                served = e["model_served"]
                # Old successful entries have a served stamp but no evidence label.
                # A missing stamp is allowed only with the new explicit selection label.
                matching_stamp = (isinstance(served, str) and bool(served.strip()) and
                    served == e["model_asked"] and e.get("model_evidence") in {None, "served_model"})
                selected_only = served is None and e.get("model_evidence") == "selected_model_only"
                if not (matching_stamp or selected_only) or not isinstance(e["tokens"], dict):
                    raise ValueError("successful ping lacks identity/token evidence")
        if e["event"] in {"verification", "resume"} and not isinstance(e["key"], str):
            raise ValueError("invalid ping window key")
        if e["event"] == "verification" and e["outcome"] not in {"verified", "blocked"}:
            raise ValueError("invalid verification outcome")
        result.append(e)
    return result


def _verification(records, events, now_s):
    resolved = {(e["attempt_id"], e["key"]) for e in events if e["event"] == "verification"}
    pending = []
    for event in events:
        if event["event"] != "result" or event["outcome"] != "succeeded":
            continue
        triggers = [w for w in event["windows"] if (event["attempt_id"], w["key"]) not in resolved]
        if triggers:
            pending.append((event, triggers))
    # The journal is append-only: resolved successes must not each decode eight
    # days of history forever. Pending checks share one decode pass per pool.
    observations = {event["pool"]: [] for event, _ in pending}
    if not observations:
        return []
    for record in records:
        if record.get("event") == "status":
            for raw in record["accounts"]:
                if raw["id"] in observations:
                    observations[raw["id"]].append(_decode(raw))
    checks = []
    for event, triggers in pending:
        for trigger in triggers:
            readings = sorted((w for s in observations[event["pool"]] if s.available and s.confidence > 0 and s.source in {"live", "cache"}
                               for w in (*s.windows, *s.not_running)
                               if w.key == trigger["key"] and event["at_s"] < w.observed_at_s <= now_s),
                              key=lambda w: w.observed_at_s)
            expected = event["started_at_s"] + trigger["length_s"]
            for current in readings:
                matches = isinstance(current, Window) and abs(current.resets_at_s - expected) <= WAIT_TOLERANCE_S
                # A rounded zero at the first poll can still be sliding. Wait beyond
                # the tolerance to distinguish a fixed deadline from observation + length.
                if matches and current.used_fraction == 0 and current.observed_at_s <= event["at_s"] + WAIT_TOLERANCE_S:
                    continue
                checks.append({"event": "verification", "pool": event["pool"],
                    "attempt_id": event["attempt_id"], "key": trigger["key"], "at_s": now_s,
                    "outcome": "verified" if matches else "blocked",
                    "expected_reset_s": expected, "reading": current.to_dict(),
                    "boundary": ("unchanged" if isinstance(current, Window) and
                        current.resets_at_s == trigger.get("resets_at_s") else "moved_or_missing")})
                break  # The first conclusive fresh reading, not a later recovery.
    return checks


def plan_pings(config: Config, records, events, now_s):
    from .ping_cli import model_for
    latest, previous = _snapshots(records)
    plans = []
    for account in config.accounts.values():
        pool = account.id
        p = {"account": account.id, "pool": pool, "action": "skip", "reason": "no_waiting_window",
             "model": None, "waiting_windows": [], "eligible_windows": [], "windows": []}
        plans.append(p)
        if not account.enabled or (account.plan_ends_s is not None and account.plan_ends_s <= now_s):
            p["reason"] = "disabled_or_plan_ended"
            continue
        if account.provider not in {"claude", "codex", "antigravity"}:
            p["reason"] = "unsupported_subscription_provider"
            continue
        p["model"] = model_for(account)
        s = latest.get(account.id)
        if s is None or not s.available or s.confidence <= 0 or s.source not in {"live", "cache"}:
            p["reason"] = "no_readable_status"
            continue
        pool_events = [e for e in events if e["pool"] == pool]
        successes = [e["at_s"] for e in pool_events if e["event"] == "result" and e["outcome"] == "succeeded"]
        attempts = [e["at_s"] for e in pool_events if e["event"] == "attempt"]
        last_success = max(successes) if successes else None
        blocked, pending = set(), set()
        for e in pool_events:
            if e["event"] == "result" and e["outcome"] == "succeeded":
                pending.update(w["key"] for w in e["windows"])
            elif e["event"] == "verification":
                pending.discard(e["key"])
                if e["outcome"] == "blocked":
                    blocked.add(e["key"])
            elif e["event"] == "resume":
                blocked.discard(e["key"])
                pending.discard(e["key"])
        for w in (*s.windows, *s.not_running):
            reason = waiting_reason(w, previous[account.id])
            row = {**w.to_dict(), "waiting_reason": reason, "gate": "not_waiting"}
            p["windows"].append(row)
            if not reason:
                continue
            p["waiting_windows"].append(row)
            age = now_s - w.observed_at_s
            if not 0 <= age <= MAX_READING_AGE_S:
                row["gate"] = "stale_or_future_reading"
            elif w.key in blocked:
                row["gate"] = "verification_blocked"
            elif w.key in pending:
                row["gate"] = "verification_pending"
            elif last_success is not None and now_s - last_success < w.length_s:
                row["gate"] = "successful_ping_cooldown"
            elif attempts and now_s - max(attempts) < FAILURE_BACKOFF_S:
                row["gate"] = "attempt_backoff"
            else:
                row["gate"] = "eligible"
                p["eligible_windows"].append(row)
        if p["eligible_windows"]:
            # A ping must fit every account-wide constraint, even if another waits.
            # Otherwise vendor overage settings could turn a subscription wake-up into credit spend.
            exhausted = any(w.used_fraction >= 1 and not w.applies_to for w in s.windows)
            if exhausted:
                p["reason"] = "another_pool_window_exhausted"
            else:
                p.update(action="ping", reason="waiting_window_without_recent_success")
        elif p["waiting_windows"]:
            p["reason"] = ",".join(sorted({w["gate"] for w in p["waiting_windows"]}))
    return plans


def _append(path, event):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


def run_pings(config, records, path: Path, *, now_s, dry_run=True, env, send=None, clock=None, resume=None):
    from .ping_cli import send_ping
    send = send or send_ping
    clock = clock or time.time
    records = list(records)
    lock = None
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError("another window-ping process holds the journal lock")
    try:
        events = _journal(path)
        checks = _verification(records, events, now_s)
        for check in checks:
            if not dry_run:
                _append(path, check)
        events.extend(checks)
        if resume:
            if dry_run:
                raise ValueError("--resume-window requires --execute")
            account_id, key = resume.rsplit(":", 1)
            if account_id not in config.accounts or not any(e["event"] == "verification" and e["pool"] == account_id and e["key"] == key and e["outcome"] == "blocked" for e in events):
                raise ValueError("no blocked window with that account:key")
            e = {"event": "resume", "pool": account_id, "key": key, "at_s": now_s}
            _append(path, e)
            events.append(e)
        plans = plan_pings(config, records, events, now_s)
        results = []
        if not dry_run and not resume:
            for p in plans:
                if p["action"] != "ping":
                    continue
                started = clock()
                # Other calls may take minutes. Re-check freshness/cooldown at each spawn.
                current = next(x for x in plan_pings(config, records, events, started) if x["pool"] == p["pool"])
                if current["action"] != "ping":
                    p.update(current)
                    continue
                attempt = {"event": "attempt", "pool": p["pool"], "account": p["account"],
                    "attempt_id": str(uuid.uuid4()), "at_s": started, "model_asked": p["model"],
                    "windows": [w for w in current["waiting_windows"] if w["gate"] not in {"verification_blocked", "stale_or_future_reading"}]}
                _append(path, attempt)  # No durable attempt -> no call.
                events.append(attempt)
                try:
                    outcome = send(config.accounts[p["account"]], env)
                except Exception as exc:
                    # Preserve a failed outcome even for an unexpected runner fault;
                    # never let an exception erase that a spend was attempted.
                    outcome = {"outcome": "failed", "kind": "runner_exception",
                        "detail": f"{type(exc).__name__}: {exc}", "tokens": None,
                        "model_served": None}
                result = {**attempt, **outcome, "event": "result", "at_s": clock(), "started_at_s": started}
                _append(path, result)
                events.append(result)
                results.append(result)
        failed = [r for r in results if r["outcome"] == "failed"]
        blocked = set()
        for e in events:
            if e["event"] == "verification" and e["outcome"] == "blocked":
                blocked.add((e["pool"], e["key"]))
            elif e["event"] == "resume":
                blocked.discard((e["pool"], e["key"]))
        return {"dry_run": dry_run, "at_s": now_s, "would_ping": sum(p["action"] == "ping" for p in plans),
            "attempted": len(results), "succeeded": len(results)-len(failed), "failed": len(failed),
            "failed_by_kind": dict(Counter(r["kind"] for r in failed)), "plans": plans,
            "results": results, "verification": checks,
            "warnings": [f"{a}:{k}: verification failed; inspect journal, then --resume-window {a}:{k}" for a, k in sorted(blocked)]}
    finally:
        if lock:
            lock.close()
