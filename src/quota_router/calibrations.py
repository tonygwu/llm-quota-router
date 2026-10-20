"""Append-only evidence, encodings, and historical beliefs about quota denominators.

Measurements never silently become beliefs. A report applies only an explicit account
belief covering the reset, and a correction to an experiment leaves that belief intact.
Times are UTC instants from the caller or evidence, never filesystem timestamps.

The JSONL file is locked for the entire read/validate/append transaction. Invalid rows,
unknown fields, duplicate ids with different contents, and partial final lines raise;
none can be mistaken for an empty ledger. The reconstruction marker authenticates the
original prefix and survives subsequent appends. It is a separate control file, not a
fourth kind of assumption. Detection cannot initialize the ledger on its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Iterator
import uuid

from .pse import DEFAULT_FABLE_FRACTION, DEFAULT_WEEKLY_TO_SESSION
from .state import state_path
from .types import TIER_CAPACITY, TIER_UNKNOWN, capacity_for_tier


VERSION = 1
LOCK_TIMEOUT_S = 2.0
KEYS = frozenset({"weekly_to_session", "capacity", "tier", "tier_scale", "fable_fraction", "weekly_pool"})
_COMMON = {"version", "id", "kind", "recorded_at", "reconstructed", "evidence_at", "scope", "account", "key"}
_FIELDS = {
    "measurement": {"value", "spread", "started_at", "ended_at", "method", "evidence", "caveats", "corrects", "disputes"},
    "encoding": {"value", "source", "effective_from", "effective_to", "status", "evidence", "reason", "annotates"},
    "belief": {"value", "valid_from", "valid_to", "status", "candidates", "reason", "measurement_ids", "disputed_measurement_ids", "decided_by"},
}


class LedgerError(ValueError):
    """An assumption history cannot be interpreted or safely appended."""


def ledger_path(env: Mapping[str, str] | None = None) -> Path:
    return state_path(env).with_name("calibrations.jsonl")


def reconstruction_path(env: Mapping[str, str] | None = None) -> Path:
    return ledger_path(env).with_name("calibrations-reconstruction.json")


def parse_time(value: str) -> float:
    """Accept only explicit UTC ISO instants (Z or +00:00), including fractional seconds."""
    if not isinstance(value, str):
        raise LedgerError("time must be an ISO UTC string")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError(f"invalid UTC time: {value!r}") from exc
    if instant.tzinfo is None or instant.utcoffset().total_seconds() != 0:
        raise LedgerError(f"time must have an explicit UTC zone: {value!r}")
    return instant.timestamp()


def format_time(now_s: float) -> str:
    _number(now_s, "timestamp")
    return datetime.fromtimestamp(now_s, timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LedgerError(f"{label} must be a finite number")


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{label} must be a nonempty string")


def _strings(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise LedgerError(f"{label} must be a list")
    for item in value:
        _text(item, label)
    if len(set(value)) != len(value):
        raise LedgerError(f"{label} contains duplicate entries")


def _value(key: str, value: Any, *, nullable: bool = False) -> None:
    if key == "capacity" and nullable and value is None:
        return
    if key == "tier":
        _text(value, "tier")
    elif key == "weekly_pool":
        if not isinstance(value, dict) or not {"weekly_to_session", "window_size"} <= value.keys():
            raise LedgerError("weekly_pool needs weekly_to_session and window_size")
        if value.keys() - {"weekly_to_session", "window_size", "fable_fraction"}:
            raise LedgerError("unknown weekly_pool field")
        _value("weekly_to_session", value["weekly_to_session"])
        _value("capacity", value["window_size"])
        if "fable_fraction" in value:
            _value("fable_fraction", value["fable_fraction"])
    else:
        _number(value, key)
        if value <= 0 or (key == "weekly_to_session" and value < 1) or (key == "fable_fraction" and value > 1):
            raise LedgerError(f"invalid {key}: {value!r}")


def _interval(record: Mapping[str, Any], start: str, end: str, *, nullable: bool, equal: bool = False) -> None:
    a, b = record[start], record[end]
    if not nullable and (a is None or b is None):
        raise LedgerError(f"{start} and {end} are required")
    av = parse_time(a) if a is not None else None
    bv = parse_time(b) if b is not None else None
    if av is not None and bv is not None and (av > bv or (av == bv and not equal)):
        raise LedgerError(f"{start} must precede {end}")


def _identity(record: Mapping[str, Any]) -> tuple[str, str | None, str]:
    return record["scope"], record["account"], record["key"]


def validate_record(record: Mapping[str, Any], previous: Iterable[Mapping[str, Any]] = ()) -> None:
    """Validate a complete record and its references to earlier ledger entries."""
    if not isinstance(record, dict):
        raise LedgerError("record must be an object")
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise LedgerError(f"unknown record kind: {kind!r}")
    expected = _COMMON | _FIELDS[kind]
    if set(record) != expected:
        raise LedgerError(f"record fields: missing={sorted(expected-set(record))}, unknown={sorted(set(record)-expected)}")
    if type(record["version"]) is not int or record["version"] != VERSION:
        raise LedgerError("unsupported calibration ledger version")
    _text(record["id"], "id")
    recorded_at_s = parse_time(record["recorded_at"])
    if type(record["reconstructed"]) is not bool:
        raise LedgerError("reconstructed must be boolean")
    if record["evidence_at"] is not None:
        if parse_time(record["evidence_at"]) > recorded_at_s:
            raise LedgerError("evidence_at cannot be later than recorded_at")
    elif record["reconstructed"]:
        raise LedgerError("reconstructed records require evidence_at")
    scope = record["scope"]
    if scope == "account":
        _text(record["account"], "account")
    elif scope == "default" or (isinstance(scope, str) and scope.startswith("tier:") and len(scope) > 5):
        if record["account"] is not None:
            raise LedgerError("non-account scope requires account=null")
    else:
        raise LedgerError(f"invalid scope: {scope!r}")
    if not isinstance(record["key"], str) or record["key"] not in KEYS:
        raise LedgerError(f"unknown calibration key: {record['key']!r}")
    refs = {r["id"]: r for r in previous}
    def references(field: str, required_kind: str) -> list[Mapping[str, Any]]:
        _strings(record[field], field)
        result = []
        for id in record[field]:
            if id not in refs or refs[id]["kind"] != required_kind:
                raise LedgerError(f"{field} references missing {required_kind} id {id!r}")
            if parse_time(refs[id]["recorded_at"]) > parse_time(record["recorded_at"]):
                raise LedgerError(f"{field} references a record written later")
            result.append(refs[id])
        return result
    if kind == "measurement":
        _value(record["key"], record["value"])
        spread = record["spread"]
        if spread is not None:
            if isinstance(spread, dict) and set(spread) == {"low", "high"}:
                _number(spread["low"], "spread.low")
                _number(spread["high"], "spread.high")
                if spread["low"] > spread["high"]:
                    raise LedgerError("spread bounds are inverted")
            else:
                _number(spread, "spread")
                if spread < 0:
                    raise LedgerError("spread must be nonnegative")
        _interval(record, "started_at", "ended_at", nullable=True, equal=True)
        for field in ("started_at", "ended_at"):
            if record[field] is not None and parse_time(record[field]) > recorded_at_s:
                raise LedgerError(f"{field} cannot be later than recorded_at")
        _text(record["method"], "method")
        _strings(record["evidence"], "evidence")
        if not record["evidence"]:
            raise LedgerError("a measurement requires evidence")
        _strings(record["caveats"], "caveats")
        if (record["started_at"] is None or record["ended_at"] is None) and not record["caveats"]:
            raise LedgerError("unknown experiment times require an explicit caveat")
        for target in references("corrects", "measurement"):
            if _identity(target) != _identity(record) or any(target[k] != record[k] for k in ("started_at", "ended_at")):
                raise LedgerError("a correction must describe the same experiment, scope, account and key")
        for target in references("disputes", "measurement"):
            if target["key"] != record["key"]:
                raise LedgerError("a dispute must concern the same key")
        if set(record["corrects"]) & set(record["disputes"]):
            raise LedgerError("cannot both correct and dispute one measurement")
    elif kind == "encoding":
        _value(record["key"], record["value"], nullable=True)
        _text(record["source"], "source")
        _interval(record, "effective_from", "effective_to", nullable=True, equal=True)
        if record["effective_to"] is not None and parse_time(record["effective_to"]) > recorded_at_s:
            raise LedgerError("effective_to cannot be later than recorded_at")
        if not isinstance(record["status"], str) or record["status"] not in {"explained", "unexplained"}:
            raise LedgerError("encoding status must be explained or unexplained")
        _text(record["reason"], "reason")
        _strings(record["evidence"], "evidence")
        if record["status"] == "explained" and not record["evidence"]:
            raise LedgerError("an explained encoding requires evidence")
        target_id = record["annotates"]
        if target_id is not None:
            _text(target_id, "annotates")
            if target_id not in refs or refs[target_id]["kind"] != "encoding":
                raise LedgerError("annotates references a missing encoding id")
            target = refs[target_id]
            if target["annotates"] is not None:
                raise LedgerError("annotates must reference the original encoding event")
            if any(record[k] != target[k] for k in ("account", "scope", "key", "value", "source", "effective_from", "effective_to")):
                raise LedgerError("an annotation must preserve the encoding it explains")
            if record["status"] != "explained":
                raise LedgerError("an annotation must explain its encoding")
            if parse_time(target["recorded_at"]) > parse_time(record["recorded_at"]):
                raise LedgerError("annotation precedes the encoding it explains")
    else:
        _interval(record, "valid_from", "valid_to", nullable=True)
        if scope != "account":
            raise LedgerError("beliefs must explicitly name an account")
        _text(record["reason"], "reason")
        _text(record["decided_by"], "decided_by")
        references("measurement_ids", "measurement")
        references("disputed_measurement_ids", "measurement")
        if record["status"] == "resolved":
            _value(record["key"], record["value"])
            if record["candidates"] != []:
                raise LedgerError("resolved beliefs cannot carry candidates")
        elif record["status"] == "unresolved":
            if record["value"] is not None or not isinstance(record["candidates"], list) or len(record["candidates"]) < 2:
                raise LedgerError("unresolved beliefs need null value and at least two candidates")
            labels = []
            for candidate in record["candidates"]:
                if not isinstance(candidate, dict) or set(candidate) != {"label", "value"}:
                    raise LedgerError("a candidate requires label and value")
                _text(candidate["label"], "candidate label")
                _value(record["key"], candidate["value"])
                labels.append(candidate["label"])
            if record["key"] == "weekly_pool" and len({
                    frozenset(candidate["value"]) for candidate in record["candidates"]}) != 1:
                raise LedgerError("weekly_pool candidates must name the same conversion fields; "
                                  "supply fable_fraction in every candidate or omit it from every candidate")
            if len(set(labels)) != len(labels):
                raise LedgerError("candidate labels must be unique")
        else:
            raise LedgerError("belief status must be resolved or unresolved")


def parse_json(raw: str | bytes) -> Any:
    """Read one JSON document without silently losing duplicate fields or NaN values."""
    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise LedgerError(f"duplicate JSON field: {key}")
            obj[key] = value
        return obj
    def invalid_constant(value):
        raise LedgerError(f"nonfinite JSON value: {value}")
    try:
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LedgerError(f"invalid JSON: {exc}") from exc


def _loads(raw: bytes) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    ids: set[str] = set()
    for line_no, line in enumerate(raw.splitlines(keepends=True), 1):
        try:
            if not line.endswith(b"\n"):
                raise LedgerError("partial final line")
            record = parse_json(line)
            validate_record(record, records)
            if record["id"] in ids:
                raise LedgerError(f"duplicate id {record['id']!r}")
            ids.add(record["id"])
            records.append(record)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise LedgerError(f"calibration ledger line {line_no}: {exc}") from exc
    return records


@contextmanager
def _locked(path: Path, *, write: bool) -> Iterator[Any]:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b" if write else "rb") as stream:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(stream, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise LedgerError(f"calibration ledger lock timed out after {LOCK_TIMEOUT_S:g}s") from exc
                time.sleep(0.01)
        try:
            stream.seek(0)
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_records(*, env: Mapping[str, str] | None = None, beliefs_as_of: str | float | None = None) -> list[dict[str, Any]]:
    cutoff = parse_time(beliefs_as_of) if isinstance(beliefs_as_of, str) else beliefs_as_of
    if cutoff is not None:
        _number(cutoff, "beliefs_as_of")
    path = ledger_path(env)
    try:
        with _locked(path, write=False) as stream:
            records = _loads(stream.read())
    except FileNotFoundError:
        return []
    return [r for r in records if cutoff is None or parse_time(r["recorded_at"]) < cutoff]


def _append(stream: Any, previous: list[dict[str, Any]], records: Iterable[Mapping[str, Any]]) -> list[str]:
    import os
    by_id = {record["id"]: record for record in previous}
    added: list[dict[str, Any]] = []
    for record in records:
        # Canonical JSON serialization also rejects non-JSON caller values.
        try:
            record = json.loads(json.dumps(record, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise LedgerError(f"record is not valid finite JSON: {exc}") from exc
        validate_record(record, [*previous, *added])
        if record["id"] in by_id:
            if record != by_id[record["id"]]:
                raise LedgerError(f"id {record['id']!r} already has different contents")
            continue
        by_id[record["id"]] = record
        added.append(record)
    if added:
        payload = b"".join((json.dumps(r, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode() for r in added)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return [r["id"] for r in added]


def append_records(records: Iterable[Mapping[str, Any]], *, env: Mapping[str, str] | None = None) -> list[str]:
    with _locked(ledger_path(env), write=True) as stream:
        return _append(stream, _loads(stream.read()), records)


def resolve_belief(records: Iterable[Mapping[str, Any]], account: str, key: str, at_s: float) -> dict[str, Any] | None:
    """Newest recorded belief wins; ties use append order, intervals are [from,to)."""
    _number(at_s, "reset time")
    matches = [r for r in records if r["kind"] == "belief" and r["account"] == account and r["key"] == key
               and (r["valid_from"] is None or parse_time(r["valid_from"]) <= at_s)
               and (r["valid_to"] is None or at_s < parse_time(r["valid_to"]))]
    return dict(max(enumerate(matches), key=lambda pair: (parse_time(pair[1]["recorded_at"]), pair[0]))[1]) if matches else None


def _latest_encodings(records: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str | None, str], Mapping[str, Any]]:
    records = list(records)
    latest: dict[tuple[str, str | None, str], Mapping[str, Any]] = {}
    for record in records:
        if record["kind"] == "encoding" and record["annotates"] is None:
            key = _identity(record)
            if key not in latest or parse_time(record["recorded_at"]) >= parse_time(latest[key]["recorded_at"]):
                latest[key] = record
    # Annotation time says when evidence was added, not when this value was encoded.
    # Explaining an older event cannot make its superseded value current again.
    event_ids = {key: record["id"] for key, record in latest.items()}
    for record in records:
        if record["kind"] == "encoding" and record["annotates"] is not None:
            key = _identity(record)
            if event_ids.get(key) == record["annotates"] and parse_time(record["recorded_at"]) >= parse_time(latest[key]["recorded_at"]):
                latest[key] = record
    return latest


def encoding_ids(records: Iterable[Mapping[str, Any]], account: str) -> dict[str, str]:
    return {key: record["id"] for (scope, owner, key), record in _latest_encodings(records).items()
            if scope == "account" and owner == account}


def basis_ledger_ids(records: Iterable[Mapping[str, Any]], account: str, at_s: float) -> dict[str, Any]:
    records = list(records)
    return {"encodings": encoding_ids(records, account),
            "beliefs": {key: r["id"] for key in sorted(KEYS)
                        if (r := resolve_belief(records, account, key, at_s)) is not None}}


def _check_marker(raw: bytes, marker: Mapping[str, Any]) -> bool:
    expected = {"version", "record_count", "sha256", "recorded_at", "reason"}
    if not isinstance(marker, dict) or set(marker) != expected or type(marker["version"]) is not int or marker["version"] != 1:
        raise LedgerError("invalid reconstruction marker")
    if type(marker["record_count"]) is not int or marker["record_count"] < 1:
        raise LedgerError("invalid reconstruction record count")
    parse_time(marker["recorded_at"])
    _text(marker["reason"], "reconstruction reason")
    lines = raw.splitlines(keepends=True)
    if len(lines) < marker["record_count"] or hashlib.sha256(b"".join(lines[:marker["record_count"]])).hexdigest() != marker["sha256"]:
        raise LedgerError("reconstruction ledger prefix does not match its marker")
    return True


def reconstruction_ready(*, env: Mapping[str, str] | None = None) -> bool:
    try:
        with _locked(ledger_path(env), write=False) as stream:
            raw = stream.read()
            _loads(raw)
            marker = parse_json(reconstruction_path(env).read_bytes())
            return _check_marker(raw, marker)
    except FileNotFoundError:
        return False
    except json.JSONDecodeError as exc:
        raise LedgerError(f"invalid reconstruction marker JSON: {exc}") from exc


def mark_reconstruction_complete(*, env: Mapping[str, str] | None = None, now_s: float, reason: str) -> None:
    """Seal the initial reconstruction prefix; called only by an explicit reconstruction."""
    import os
    _text(reason, "reconstruction reason")
    with _locked(ledger_path(env), write=True) as stream:
        raw = stream.read()
        records = _loads(raw)
        if not records:
            raise LedgerError("cannot mark an empty reconstruction complete")
        path = reconstruction_path(env)
        if path.exists():
            _check_marker(raw, parse_json(path.read_bytes()))
            return
        marker = {"version": 1, "record_count": len(records), "sha256": hashlib.sha256(raw).hexdigest(),
                  "recorded_at": format_time(now_s), "reason": reason}
        with path.open("x", encoding="utf-8") as out:
            out.write(json.dumps(marker, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())


def loaded_values(config: Any, *, observed_tiers: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Values actually used, including source changes with identical numeric values.

    The writer reads tier from adapter snapshots. If neither a snapshot nor an explicit
    config tier supplies it, omit that account's tier/scale; a failed read is not a
    change to unknown tier. Tier-table overrides are not used by the waste writer.
    """
    result: list[dict[str, Any]] = []
    def add(scope, account, key, value, source):
        result.append(dict(scope=scope, account=account, key=key, value=value, source=source))
    add("default", None, "weekly_to_session", DEFAULT_WEEKLY_TO_SESSION, "code_default")
    add("default", None, "fable_fraction", DEFAULT_FABLE_FRACTION, "code_default")
    for tier, scale in sorted(TIER_CAPACITY.items()):
        add(f"tier:{tier}", None, "tier_scale", scale, "code_default")
    for account, cfg in sorted(config.accounts.items()):
        add("account", account, "weekly_to_session", cfg.weekly_to_session if cfg.weekly_to_session is not None else DEFAULT_WEEKLY_TO_SESSION,
            "config" if cfg.weekly_to_session is not None else "code_default")
        add("account", account, "capacity", cfg.capacity, "config" if cfg.capacity is not None else "tier_fallback")
        add("account", account, "fable_fraction", DEFAULT_FABLE_FRACTION, "code_default")
        observed = observed_tiers is not None and account in observed_tiers
        tier = observed_tiers[account] if observed else cfg.tier
        if observed or tier != TIER_UNKNOWN:
            add("account", account, "tier", tier, "snapshot" if observed else "config")
            add("account", account, "tier_scale", capacity_for_tier(tier), f"tier:{tier}")
    return result


def detect_config_changes(config: Any, *, now_s: float, env: Mapping[str, str] | None = None,
                          observed_tiers: Mapping[str, str] | None = None) -> list[str]:
    """Append each unexplained change once and keep warning until it is annotated."""
    if not reconstruction_ready(env=env):
        return ["calibration reconstruction is absent or incomplete; encoding detection is disabled"]
    at = format_time(now_s)
    with _locked(ledger_path(env), write=True) as stream:
        raw = stream.read()
        records = _loads(raw)
        _check_marker(raw, parse_json(reconstruction_path(env).read_bytes()))
        latest = _latest_encodings(records)
        pending = []
        for item in loaded_values(config, observed_tiers=observed_tiers):
            prior = latest.get(_identity(item))
            if prior is not None and (prior["value"], prior["source"]) == (item["value"], item["source"]):
                continue
            if prior is not None and parse_time(prior["recorded_at"]) > now_s:
                raise LedgerError("loaded config observation precedes the latest encoding")
            pending.append(dict(version=VERSION, id=f"encoding-{uuid.uuid4().hex}", kind="encoding", recorded_at=at,
                                reconstructed=False, evidence_at=None, **item,
                                effective_from=prior["effective_to"] if prior is not None else None,
                                effective_to=at, status="unexplained", evidence=[],
                                reason="Loaded value or source differs from the latest encoding; evidence required", annotates=None))
        _append(stream, records, pending)
        # Unexplained past changes remain warnings even after another change supersedes
        # them. An annotation must address each unexplained event explicitly.
        records.extend(pending)
        annotated = {r["annotates"] for r in records if r["kind"] == "encoding" and r["annotates"] is not None}
        return [f"unexplained calibration encoding {r['id']}: {r['account'] or r['scope']} {r['key']}={r['value']!r} ({r['source']}); record evidence"
                for r in records if r["kind"] == "encoding" and r["status"] == "unexplained" and r["id"] not in annotated]
