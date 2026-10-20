"""Disposable reports derived from the durable reset log and assumption ledger."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from itertools import product
from typing import Any

from . import calibrations, waste


def _at(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _uncertain_change(entries, at_s):
    return any(entry["effective_from"] is not None and entry["effective_to"] is not None
               and _at(entry["effective_from"]) <= at_s < _at(entry["effective_to"])
               for entry in entries)


def _effective(row, basis, beliefs, ledger, replacements=None):
    """Compose beliefs by component, retaining explicit capacity over a tier scale.

    A newer whole-pool belief covers k and effective size together. Later component
    beliefs can revise those components; an irrelevant tier scale cannot override an
    explicit pool size. Only unresolved beliefs that still supply a used component
    prevent a figure.
    """
    replacements = replacements or {}
    slots = {key: (basis[key], None) for key in ("k", "window_size", "fable_fraction", "tier")}
    mode = basis["window_size_source"]
    scale = (basis["window_size"], None) if mode == "tier" else (None, None)
    for belief in beliefs:
        selected = replacements.get(belief["id"])
        value = selected["value"] if selected is not None else belief["value"]
        key = belief["key"]
        if key == "weekly_pool":
            slots["k"] = (None if value is None else value["weekly_to_session"], belief)
            slots["window_size"] = (None if value is None else value["window_size"], belief)
            mode = "pool"
            if value is not None and "fable_fraction" in value:
                slots["fable_fraction"] = (value["fable_fraction"], belief)
            elif value is None and any("fable_fraction" in candidate["value"]
                                       for candidate in belief["candidates"]):
                # Validation requires uniform optional fields across candidates,
                # so the whole belief either owns this uncertainty or retains
                # the prior fraction (including any prior unresolved choices).
                slots["fable_fraction"] = (None, belief)
        elif key == "weekly_to_session":
            slots["k"] = (value, belief)
        elif key == "capacity":
            mode = "tier" if value is None and belief["status"] == "resolved" else "capacity"
            slots["window_size"] = (value, belief)
        elif key == "tier_scale":
            scale = (value, belief)
        else:
            slots[key] = (value, belief)
    missing = None
    extra_ids = []
    if mode == "tier":
        if scale[1] is not None or slots["tier"][0] == basis["tier"]:
            slots["window_size"] = scale
        else:
            # A corrected tier does not authorize today's mapping. Use an encoding
            # known to exist by the reset, or leave the old figure visibly logged.
            events = [entry for entry in ledger if entry["kind"] == "encoding"
                      and entry["scope"] == "tier:" + str(slots["tier"][0])
                      and entry["key"] == "tier_scale" and entry["annotates"] is None]
            candidates = [entry for entry in events if entry["effective_to"] is not None
                          and _at(entry["effective_to"]) <= row.reset_at_s]
            if _uncertain_change(events, row.reset_at_s):
                missing = "corrected tier scale is uncertain at this reset within encoding change bounds"
            elif candidates:
                chosen = max(enumerate(candidates), key=lambda pair: (_at(pair[1]["effective_to"]), pair[0]))[1]
                slots["window_size"] = (chosen["value"], None)
                extra_ids.append(chosen["id"])
            else:
                missing = "corrected tier has no historical tier scale evidence"
    elif mode is None and (scale[1] is not None or slots["tier"][1] is not None):
        missing = "original writer did not record whether window size came from capacity or tier"
    used = [slots["k"], slots["window_size"]]
    if mode == "tier":
        used.append(slots["tier"])
    if row.scoped:
        used.append(slots["fable_fraction"])
    active_ids = {entry["id"] for _, entry in used if entry is not None}
    active = [entry for entry in beliefs if entry["id"] in active_ids]
    return {key: value for key, (value, _) in slots.items()}, active, missing, extra_ids


def _value(row: waste.ResetRecord, values: Mapping[str, Any]) -> tuple[float | None, str | None]:
    if not row.observed:
        return None, "reset was not observed"
    if not row.scoped and row.window != waste.WINDOW_KEY_7D:
        return None, f"no conversion exists for unscoped window {row.window!r}"
    k, size = values["k"], values["window_size"]
    if k is None or size is None:
        return None, "conversion is missing k or window size"
    raw = row.raw
    if raw is not None and raw["before"] is not None:
        remaining = max(0.0, 1.0 - raw["before"]["used_fraction"])
    else:
        remaining = row.remaining_fraction
    if remaining is None:
        return None, "no remaining fraction was recorded"
    if not row.scoped:
        return remaining * k * size, None
    if (raw is not None and raw["before"] is not None and
            "fable" not in raw["before"]["applies_to"]):
        return None, "no conversion exists for this model-scoped window"
    fraction = values["fable_fraction"]
    if fraction is None:
        return None, "original writer did not record fable_fraction and no belief supplies it"
    if raw is None or raw["weekly_before"] is None:
        return None, "original writer did not retain the weekly reading needed for the coupled minimum"
    weekly = max(0.0, 1.0 - raw["weekly_before"]["used_fraction"])
    return min(remaining * fraction, weekly) * k * size, None


def _row_report(row: waste.ResetRecord, ledger: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    basis = row.logged_basis()
    keys = ("weekly_pool", "weekly_to_session", "capacity", "tier", "tier_scale", "fable_fraction")
    beliefs = [belief for key in keys if (belief := calibrations.resolve_belief(
        ledger, account=row.account, key=key, at_s=row.reset_at_s
    )) is not None]
    order = {entry["id"]: index for index, entry in enumerate(ledger)}
    beliefs.sort(key=lambda entry: (_at(entry["recorded_at"]), order[entry["id"]]))
    values, active, missing, encoding_ids = _effective(row, basis, beliefs, ledger)
    ids = [entry["id"] for entry in active] + encoding_ids
    unresolved = [entry for entry in active if entry["status"] == "unresolved"]
    candidates = []
    if not row.observed:
        value, status, reason = None, "unobserved", "reset was not observed"
    elif unresolved:
        for chosen in product(*(entry["candidates"] for entry in unresolved)):
            replacements = {entry["id"]: candidate for entry, candidate in zip(unresolved, chosen)}
            candidate_values, _, reason, _ = _effective(row, basis, beliefs, ledger, replacements)
            figure, arithmetic_reason = _value(row, candidate_values)
            if reason is not None:
                figure = None
            candidates.append({"label": "; ".join(candidate["label"] for candidate in chosen),
                               "values": candidate_values, "wasted_pse": figure,
                               "reason": reason or arithmetic_reason})
        value, status, reason = None, "unresolved", "operator belief remains unresolved"
    elif active or missing is not None:
        value, arithmetic_reason = _value(row, values)
        reason = missing or arithmetic_reason
        status = "belief" if reason is None else "logged_unrecomputable"
        if reason is not None:
            value = basis["wasted_pse"]
    else:
        value, status, reason = basis["wasted_pse"], "logged", "no belief covers this reset"

    # Count observed rows with an actual reported figure. Unresolved candidates and
    # missed resets do not enter this count. If recomputation failed, provenance
    # describes the logged figure we actually return, not hypothetical new values.
    countable = row.observed and value is not None
    counted_values = values if status == "belief" else basis
    counted_beliefs = active if status == "belief" else []
    by_id = {entry["id"]: entry for entry in ledger}
    writer_ids = basis["ledger_ids"]
    writer_encodings = writer_ids.get("encodings", {}) if isinstance(writer_ids, Mapping) else {}

    def historical_encoding(key):
        events = [entry for entry in ledger if entry["kind"] == "encoding"
                  and entry["scope"] == "account" and entry["account"] == row.account
                  and entry["key"] == key and entry["annotates"] is None]
        # A reset inside a change's bounds cannot be assigned either encoding.
        if _uncertain_change(events, row.reset_at_s):
            return None
        applicable = [(float("-inf") if entry["effective_to"] is None else _at(entry["effective_to"]), index, entry)
                      for index, entry in enumerate(events)
                      if (entry["effective_from"] is None and entry["effective_to"] is None)
                      or (entry["effective_to"] is not None and _at(entry["effective_to"]) <= row.reset_at_s)]
        return max(applicable, key=lambda item: item[:2])[2] if applicable else None

    def source_for(key, expected, explicit):
        entry = by_id.get(writer_encodings.get(key))
        if entry is not None and entry["kind"] == "encoding" and entry["value"] == expected:
            return entry["source"]
        if explicit is not None:
            return explicit
        entry = historical_encoding(key)
        return entry["source"] if entry is not None and entry["value"] == expected else None

    suppliers = {}
    size_source = basis["window_size_source"]
    for entry in counted_beliefs:
        key = entry["key"]
        if key == "weekly_pool":
            suppliers.update(k=entry, window_size=entry)
            size_source = "belief"
            if "fable_fraction" in entry["value"]:
                suppliers["fable_fraction"] = entry
        elif key == "weekly_to_session":
            suppliers["k"] = entry
        elif key == "capacity":
            size_source = "tier" if entry["value"] is None else "belief"
            if entry["value"] is not None:
                suppliers["window_size"] = entry
        elif key == "tier_scale":
            suppliers["window_size"] = entry
            size_source = "tier"  # active scale means _effective used the fallback
        elif key == "fable_fraction":
            suppliers["fable_fraction"] = entry

    sources = {"k": source_for("weekly_to_session", counted_values["k"], basis.get("k_source")),
               "window_size": size_source}
    if "k" in suppliers and counted_values["k"] != basis["k"]:
        sources["k"] = "belief"
    if sources["window_size"] is None:
        capacity = historical_encoding("capacity")
        if capacity is not None:
            if capacity["value"] is None:
                sources["window_size"] = "tier"
            elif capacity["value"] == counted_values["window_size"]:
                sources["window_size"] = "capacity"
    if row.scoped:
        sources["fable_fraction"] = source_for("fable_fraction", counted_values["fable_fraction"],
                                               basis.get("fable_fraction_source"))
        if "fable_fraction" in suppliers and counted_values["fable_fraction"] != basis["fable_fraction"]:
            sources["fable_fraction"] = "belief"

    component_keys = {"k": {"weekly_to_session", "weekly_pool"},
                      "window_size": {"capacity", "tier_scale", "weekly_pool"},
                      "fable_fraction": {"fable_fraction", "weekly_pool"}}
    provenance = {}
    for component, component_source in sources.items():
        supplier = suppliers.get(component)
        covered = []
        for measurement_id in supplier["measurement_ids"] if supplier is not None else []:
            measurement = by_id.get(measurement_id)
            if measurement is None or measurement["kind"] != "measurement":
                continue
            scope_covers = ((measurement["scope"] == "account" and measurement["account"] == row.account)
                            or measurement["scope"] == "tier:" + str(counted_values["tier"]))
            key_covers = measurement["key"] in component_keys[component]
            if component == "fable_fraction" and measurement["key"] == "weekly_pool":
                key_covers = "fable_fraction" in measurement["value"]
            if scope_covers and key_covers:
                covered.append(measurement_id)
        is_default = component_source in {"code_default", "tier", "tier_fallback"}
        provenance[component] = {"source": component_source, "measurement_ids": covered,
                                 "unmeasured_default": countable and is_default and not covered,
                                 "unknown_source": countable and component_source is None and not covered}
    default_components = [key for key, detail in provenance.items() if detail["unmeasured_default"]]
    unknown_components = [key for key, detail in provenance.items() if detail["unknown_source"]]
    default_unmeasured, unknown_source = bool(default_components), bool(unknown_components)
    return {
        "account": row.account, "window": row.window, "reset_at": waste._iso(row.reset_at_s),
        "reset_at_s": row.reset_at_s, "scheduled_reset_at_s": row.boundary_s,
        "kind": row.kind, "observed": row.observed, "scoped": row.scoped,
        "raw": row.raw, "remaining_fraction": row.remaining_fraction,
        "logged": {"wasted_pse": basis["wasted_pse"], "basis": basis},
        "current": {"status": status, "wasted_pse": value, "values": values,
                    "ledger_ids": ids, "reason": reason, "candidates": candidates},
        "unmeasured_default": default_unmeasured, "unknown_basis_source": unknown_source,
        "unmeasured_default_components": default_components,
        "unknown_basis_source_components": unknown_components,
        "assumption_provenance": provenance,
    }


def _aggregate(rows: Sequence[waste.ResetRecord]) -> dict[str, Any]:
    accounts: dict[str, Any] = {}
    totals, scoped = waste._blank_bucket(), waste._blank_bucket()
    early, early_scoped = waste._blank_early_bucket(), waste._blank_early_bucket()
    for row in rows:
        bucket = accounts.setdefault(row.account, {}).setdefault(
            row.window, {**waste._blank_bucket(), "early": 0, "forfeited_pse": 0.0, "scoped": row.scoped})
        if row.kind == waste.KIND_EARLY:
            bucket["early"] += 1
            if row.wasted_pse is not None:
                bucket["forfeited_pse"] += row.wasted_pse
            waste._absorb_early(early_scoped if row.scoped else early, row)
        else:
            waste._absorb(bucket, row)
            waste._absorb(scoped if row.scoped else totals, row)
    return {"accounts": accounts, "totals": totals, "scoped_totals": scoped,
            "early_totals": early, "early_scoped_totals": early_scoped,
            "early_events": waste._early_events(rows)}


def summarize(records: Iterable[waste.ResetRecord], *, ledger: Sequence[Mapping[str, Any]] = (),
              beliefs_as_of: str | None = None, forgone_records=(), ping_events=()) -> dict[str, Any]:
    current = waste.current_records(records)
    selected = list(ledger)
    if beliefs_as_of is not None:
        cutoff = calibrations.parse_time(beliefs_as_of)
        selected = [entry for entry in selected if _at(entry["recorded_at"]) < cutoff]
    rows = [_row_report(row, selected) for row in current]
    derived = [replace(row, wasted_pse=report["current"]["wasted_pse"])
               for row, report in zip(current, rows)]
    logged = [replace(row, wasted_pse=row.logged_basis()["wasted_pse"]) for row in current]
    summary = _aggregate(derived)
    summary.update({
        "rows": rows, "logged_totals": _aggregate(logged),
        "beliefs_as_of": beliefs_as_of, "ledger_records_considered": len(selected),
        "basis_mode": "beliefs_and_logged" if selected else "logged_only_no_ledger_records",
        "unresolved_rows": sum(row["current"]["status"] == "unresolved" for row in rows),
        "unrecomputable_rows": sum(row["current"]["status"] == "logged_unrecomputable" for row in rows),
        "unmeasured_default_rows": sum(row["unmeasured_default"] for row in rows),
        "unknown_basis_source_rows": sum(row["unknown_basis_source"] for row in rows),
        "first_reset_at_s": min((row.reset_at_s for row in current), default=None),
        "last_reset_at_s": max((row.reset_at_s for row in current), default=None),
    })
    from . import forgone
    waiting = forgone.summarize(forgone_records, ledger=selected, ping_events=ping_events)
    summary.update(forgone_totals=waiting['totals'], forgone_accounts=waiting['accounts'],
                   forgone_rows=waiting['rows'], first_live_ping_at_s=waiting['first_live_ping_at_s'],
                   forgone_retention_note=waiting['retention_note'],
                   total_wasted_pse=summary['totals']['wasted_pse']+waiting['totals']['forgone_pse'])
    return summary
