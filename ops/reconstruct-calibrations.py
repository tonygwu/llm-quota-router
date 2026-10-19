#!/usr/bin/env python3
"""Propose #24's historical ledger, reading evidence without touching live state.

The default is a JSON proposal on stdout. --output-state-home can only name a
temporary directory and creates a new scratch ledger. Review that proposal and
obtain the operator's approval before a separate, backed-up live migration.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import tempfile
import tomllib

DOC12 = "docs/measurements/2026-10-12-antigravity-capacity.md"
DOC18 = "docs/measurements/2026-10-18-gemini-size.md"
REANALYSIS = "docs/measurements/2026-10-19-gemini-reanalysis.md"
KEYS = ("weekly_to_session", "capacity", "tier")
# The experiment's first recorded window opening; not a claimed provider-change time.
GEMINI_MEASURED_FROM = "2026-10-18T23:02:36Z"


def utc(text: str) -> datetime:
    if not text.endswith("Z"):
        raise ValueError(f"UTC timestamp must end in Z: {text!r}")
    return datetime.fromisoformat(text[:-1] + "+00:00")


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def common(kind: str, ident: str, account: str | None, key: str,
           recorded_at: str, evidence_at: str, scope: str = "account") -> dict:
    utc(recorded_at)
    utc(evidence_at)
    return dict(version=1, id=ident, kind=kind, recorded_at=recorded_at,
                reconstructed=True, evidence_at=evidence_at, account=account,
                scope=scope, key=key)


def measurement(ident: str, account: str | None, key: str, value: float,
                recorded_at: str, evidence_at: str, evidence: list[str], method: str,
                *, scope: str = "account", spread=None, caveats=(), corrects=(),
                disputes=(), started_at=None, ended_at=None) -> dict:
    row = common("measurement", ident, account, key, recorded_at, evidence_at, scope)
    row.update(value=value, spread=spread, started_at=started_at,
               ended_at=ended_at, method=method, evidence=evidence,
               caveats=list(caveats), corrects=list(corrects), disputes=list(disputes))
    return row


def encoding(ident: str, account: str | None, key: str, value,
             recorded_at: str, evidence_at: str, evidence: list[str], reason: str,
             *, lower=None, upper=None, source="config", scope="account") -> dict:
    row = common("encoding", ident, account, key, recorded_at, evidence_at, scope)
    row.update(value=value, source=source, effective_from=lower, effective_to=upper,
               status="explained", evidence=evidence, reason=reason, annotates=None)
    return row


def historical_measurements(recorded_at: str) -> list[dict]:
    rows = [
        measurement("measurement-max20-k-11.9", None, "weekly_to_session", 11.9,
            recorded_at, "2026-08-16T06:12:46Z", ["git:de80483935b03d826459e1d02e1611978cdab6a4:src/quota_router/pse.py"],
            "Four logged intervals: summed session movement 95 points / weekly 8 points.",
            scope="default", spread={"low": 9.9, "high": 13.9},
            caveats=["Original +/-2 interval was later judged overconfident; source oscillation and 1-point weekly quantization biased it.",
                     "Accounts and exact run times were not recorded; evidence_at is the commit timestamp, not a recovered experiment clock."]),
        measurement("measurement-max20-k-6.25", None, "weekly_to_session", 6.25,
            recorded_at, "2026-08-16T09:16:04Z", ["git:f3564abc1a708ad9362e116ca51907bfcc080606:src/quota_router/pse.py",
                "git:758ca3ba080c8af3ece170cb52f302113c4c06fa:src/quota_router/pse.py"],
            "One clean full window: 100 session points / 16 weekly points; corroborated on two Max-20x accounts.",
            scope="tier:max_20x", spread={"low": 6.0, "high": 6.5}, disputes=["measurement-max20-k-11.9"],
            caveats=["Range describes corroborating estimates, not a confidence interval.",
                     "Original accounts are unnamed. This covers the Claude Max-20x tier, not other providers or tiers.",
                     "Exact experiment timestamps are unavailable; timestamp is the evidence commit."]),
        measurement("measurement-claude-c-k-9.8", "claude_c", "weekly_to_session", 9.8,
            recorded_at, "2026-08-16T10:00:38Z", ["git:758ca3ba080c8af3ece170cb52f302113c4c06fa:src/quota_router/pse.py", DOC12 + "#the-max-20x-side-28320-per-five-hour-window"],
            "Two dense calibration windows on the single Max-5x account, corroborated at about 9.8.",
            caveats=["One account does not establish a Max-5x tier law; exact spread and run timestamps were not preserved in the commit."]),
        measurement("measurement-agy-claude-capacity", "antigravity_claude", "capacity", 0.088,
            recorded_at, "2026-10-18T02:28:21Z", [DOC12 + "#the-ratio", "measurement-folder:agy-capacity-2026-10-12", "git:1498169"],
            "Opus dollar weekly pool / k / Claude Max-20x PSE reference; pooled measurement rounds 0.0884 to 0.088.",
            spread={"low": 0.082, "high": 0.100},
            caveats=["Max-5x scale 0.25, cache-write input pricing and replica X are assumptions; the range is not a complete error bound.",
                     "Only the primary Claude/GPT pool was sized. The second pool was not measured.",
                     "The experiment ran October 12. Exact start/end times are not extracted here; evidence_at is the documentation commit's author timestamp."]),
    ]
    for account in ("antigravity_claude", "antigravity_claude_b"):
        rows.append(measurement("measurement-" + account + "-k-2", account, "weekly_to_session", 2.0,
            recorded_at, "2026-10-18T02:28:21Z", [DOC12 + "#the-antigravity-side-5007-per-week-k--20", "measurement-folder:agy-capacity-2026-10-12", "git:1498169"],
            "Consecutive same-replica session movement / weekly movement.",
            spread=0.000005 if account == "antigravity_claude" else None,
            caveats=["Second account has two k reps but no served-model verification; no second-account dollar size was measured.",
                     "The experiment ran October 12; exact start/end clocks are not extracted here. evidence_at is the documentation commit."]))
    for account, k, provisional, settled in (("antigravity_gemini", 15.0006, 1.96, 2.43),
                                            ("antigravity_gemini_b", 15.0005, 2.24, 2.45)):
        rows.append(measurement("measurement-" + account + "-k-15", account, "weekly_to_session", k,
            recorded_at, "2026-10-18T02:28:21Z", [DOC12 + "#hazards-for-the-next-measurement", "git:1498169"],
            "Mean of fine consecutive same-replica movements: 13/16 and 12/16 accepted changes respectively.",
            caveats=["Five-hour jumps excluded; this experiment did not originally size the five-hour window.",
                     "The experiment ran October 12; exact start/end clocks are not extracted here. evidence_at is the documentation commit."]))
        rows.append(measurement("measurement-" + account + "-k-4", account, "weekly_to_session", 4.0,
            recorded_at, "2026-10-19T02:29:25Z", [DOC18 + "#k--40"],
            "Fine consecutive movements on two chains, corroborated by settled movement.",
            spread={"low": 3.992, "high": 4.007}, disputes=["measurement-" + account + "-k-15"],
            started_at=GEMINI_MEASURED_FROM, ended_at="2026-10-19T02:29:25Z",
            caveats=["Different experiment disagrees; no assertion that the provider changed exactly at the October 16 early reset."]))
        base = "measurement-" + account + "-capacity-"
        for phase, value, evidence_at, correction in (("end", provisional, "2026-10-19T01:34:19Z", []),
                                                     ("settled", settled, "2026-10-19T13:38:38Z", [base + "end"])):
            rows.append(measurement(base + phase, account, "capacity", value, recorded_at, evidence_at,
                [DOC18 + "#the-meter-settles", "measurement-folder:gemini-size-2026-10-18"],
                "Gemini 3.8 Flash list-price dollars / weekly movement / 283.20 / k=4.",
                corrects=correction, started_at=GEMINI_MEASURED_FROM, ended_at="2026-10-19T01:34:19Z",
                caveats=["The settled result corrects the same experiment; this is not a provider-change assertion.",
                         "Primary served identity is model code 1318 only; secondary includes a pre-run smoke-test charge without its dollars.",
                         "PSE is price-based and the Max-5x reference scale remains an assumption."]))
    return rows


def historical_code_encodings(recorded_at: str) -> list[dict]:
    rows = []
    for value, when, commit in ((12.0, "2026-08-16T06:12:46Z", "de80483"),
                                (6.25, "2026-08-16T09:16:04Z", "f3564ab")):
        rows.append(encoding(f"encoding-default-k-{value}", None, "weekly_to_session", value,
            recorded_at, when, ["git:" + commit + ":src/quota_router/pse.py"],
            "Code default committed at this instant; live installation time is not established by git.",
            lower=when, upper=when, source="code_default", scope="default"))
    rows.append(encoding("encoding-default-fable-0.5", None, "fable_fraction", 0.5,
        recorded_at, "2026-08-16T06:12:46Z", ["git:de80483:src/quota_router/pse.py"],
        "Code used the documented Fable half-week allowance. Consistency checks did not independently measure it.",
        lower="2026-08-16T06:12:46Z", upper="2026-08-16T06:12:46Z", source="code_default", scope="default"))
    for tier, scale in (("max_20x", 1.0), ("max_5x", 0.25), ("pro", 1.0), ("unknown", 1.0)):
        rows.append(encoding("encoding-tier-" + tier, None, "tier_scale", scale,
            recorded_at, "2026-08-14T20:47:55Z", ["git:7386ddc:src/quota_router/types.py"],
            "Original code tier scale; Max-20x defines the unit, Max-5x 0.25 is an assumption, and pro/unknown 1.0 are neutral defaults.",
            lower="2026-08-14T20:47:55Z", upper="2026-08-14T20:47:55Z", source="code_default", scope="tier:" + tier))
    return rows


def backup_encodings(config_dir: Path, recorded_at: str) -> tuple[list[dict], list[str]]:
    """Only explicit account values are recovered; filenames carry backup stamps.

    A backup contains the configuration BEFORE an edit. It constrains an edit to
    an interval; it does not establish the edit happened at the filename's clock.
    Undated backups are read for validity and named as exclusions, never dated by mtime.
    """
    backups, warnings = [], []
    for path in sorted(config_dir.glob("config.toml.bak-*")):
        data = tomllib.loads(path.read_text())
        accounts = data.get("accounts", {})
        if not isinstance(accounts, dict):
            raise ValueError(f"{path.name}: accounts must be a table")
        suffix = path.name.removeprefix("config.toml.bak-")
        if re.fullmatch(r"\d{8}T\d{6}Z", suffix):
            when = datetime.strptime(suffix, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            earliest = latest = stamp(when)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", suffix):
            when = datetime.strptime(suffix, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            earliest, latest = stamp(when), stamp(when + timedelta(days=1))
            warnings.append(path.name + ": date-only stamp retains a whole UTC-day bound, not an invented instant")
        else:
            warnings.append(path.name + ": no parseable evidence timestamp; excluded from chronology (never dated by mtime)")
            continue
        backups.append((earliest, latest, path.name, accounts))
    rows, previous = [], {}
    for earliest, latest, filename, accounts in sorted(backups):
        for account, values in sorted(accounts.items()):
            if not isinstance(values, dict):
                raise ValueError(f"{filename}: account {account} must be a table")
            for key in KEYS:
                value = values.get(key)
                pair = (account, key)
                old = previous.get(pair)
                if old is None and value is None and key in {"weekly_to_session", "capacity"}:
                    rows.append(encoding(f"encoding-backup-{len(rows):04d}-{account}-{key}", account, key,
                        6.25 if key == "weekly_to_session" else None, recorded_at, latest, [filename],
                        "No explicit override in the first surviving backup; onset unknown. Inherited 6.25 is the code default after August 16, "
                        "not a measurement of this account; older code-default 12 is recorded separately. Unset capacity falls back to the observed tier.",
                        source="code_default" if key == "weekly_to_session" else "tier_fallback"))
                if value is not None and (old is None or old[0] != value):
                    rows.append(encoding(f"encoding-backup-{len(rows):04d}-{account}-{key}", account, key,
                        value, recorded_at, latest, [filename] + ([old[2]] if old else []),
                        "Backup holds config before an edit: change is bounded by observations, not assigned to the backup instant."
                        if old else "First surviving explicit setting; earlier onset is unknown.",
                        lower=old[1] if old else None, upper=latest))
                elif value is None and old is not None and old[0] is not None:
                    # Removing an override is itself an encoding change.
                    replacement = {"weekly_to_session": 6.25, "capacity": None, "tier": "unknown"}[key]
                    rows.append(encoding(f"encoding-backup-{len(rows):04d}-{account}-{key}", account, key,
                        replacement, recorded_at, latest, [old[2], filename],
                        "Explicit override removed between backups; the router falls back to its code default or observed tier.",
                        lower=old[1], upper=latest, source="code_default"))
                previous[pair] = (value, earliest, filename)
    return rows, warnings


def documented_gemini_encodings(recorded_at: str) -> list[dict]:
    rows = []
    for account, provisional, settled in (("antigravity_gemini", 1.96, 2.43), ("antigravity_gemini_b", 2.24, 2.45)):
        for key, value, minute in (("weekly_to_session", 4.0, "13:42"), ("capacity", provisional, "13:42"),
                                   ("capacity", settled, "20:33")):
            lower = "2026-10-19T" + minute + ":00Z"
            upper = stamp(utc(lower) + timedelta(minutes=1))
            rows.append(encoding(f"encoding-doc-{account}-{key}-{minute.replace(':', '')}", account, key,
                value, recorded_at, "2026-10-19T20:54:02Z", [DOC18 + "#results", "git:699de25"],
                "The measurement document records this operator encoding to minute precision. Bounds retain that precision; "
                "the later settled value corrects the same experiment, not a provider change.", lower=lower, upper=upper))
    return rows


def belief(ident: str, account: str, key: str, value, recorded_at: str, reason: str,
           measurements: list[str], *, start=None, end=None, candidates=(), disputed=()) -> dict:
    row = common("belief", ident, account, key, recorded_at, "2026-10-19T00:00:00Z")
    row.update(valid_from=start, valid_to=end, status="unresolved" if candidates else "resolved",
               value=value, candidates=list(candidates), reason=reason,
               measurement_ids=measurements, disputed_measurement_ids=list(disputed),
               decided_by="operator reconstruction instruction, 2026-10-19")
    return row


def historical_beliefs(recorded_at: str, observed_tiers: dict[str, str]) -> list[dict]:
    rows = []
    for account, tier in sorted(observed_tiers.items()):
        if (account == "claude" or account.startswith("claude_")) and tier == "max_20x":
            rows.append(belief("belief-" + account + "-k", account, "weekly_to_session", 6.25,
                recorded_at, "The measurement covers the Claude Max-20x tier, whose original two accounts were unnamed. "
                "Apply that tier evidence to this observed Max-20x account, including later-created accounts. "
                "No evidence of a change: covers all time; it does not claim this account was in the original experiment.",
                ["measurement-max20-k-6.25"], disputed=["measurement-max20-k-11.9"]))
    rows.append(belief("belief-claude-c-k", "claude_c", "weekly_to_session", 9.8, recorded_at,
        "Account-specific Max-5x evidence; not a tier law. No evidence of a change: covers all time.",
        ["measurement-claude-c-k-9.8"]))
    for account in ("antigravity_claude", "antigravity_claude_b"):
        rows.append(belief("belief-" + account + "-k", account, "weekly_to_session", 2.0, recorded_at,
            "Both accounts had k measured. No evidence of a change: covers all time.",
            ["measurement-" + account + "-k-2"]))
    rows.append(belief("belief-antigravity_claude-capacity", "antigravity_claude", "capacity", 0.088,
        recorded_at, "Primary Claude/GPT pool measured directly. No evidence of a change: covers all time. "
        "The second pool's configured 0.088 is not promoted to a measured belief because its size was never measured.",
        ["measurement-agy-claude-capacity"]))
    for account, size in (("antigravity_gemini", 2.43), ("antigravity_gemini_b", 2.45)):
        rows.append(belief("belief-" + account + "-historical-unresolved", account, "weekly_pool", None,
            recorded_at, "Operator decision pending. The October 16 reset is a plausible marker, not proof of a provider change. "
            "Before the October 18 experiment the weekly pool versus five-hour-window interpretation is unresolved; "
            "no provider-change instant is invented. The re-analysis is additional measurement evidence, not an operator decision.",
            ["measurement-" + account + "-k-15", "measurement-" + account + "-k-4",
             "measurement-" + account + "-capacity-settled"], end=GEMINI_MEASURED_FROM,
            candidates=[{"label": "weekly pool stayed the same", "value": {"weekly_to_session": 4.0, "window_size": size}},
                        {"label": "five-hour window stayed the same", "value": {"weekly_to_session": 15.0, "window_size": size}}]))
        rows.append(belief("belief-" + account + "-measured-weekly", account, "weekly_pool",
            {"weekly_to_session": 4.0, "window_size": size}, recorded_at,
            "Use the operator's October 19 settled-value decision for the measured October 18 window and later. "
            "Starting at the experiment establishes applicability, not the unknown time of a provider change; "
            "earlier windows remain unresolved.", ["measurement-" + account + "-k-4",
            "measurement-" + account + "-capacity-settled"], start=GEMINI_MEASURED_FROM))
    return rows


def check_scratch_destination(path: Path) -> Path:
    destination = path.resolve()
    roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
    if not any(destination != root and destination.is_relative_to(root) for root in roots):
        raise ValueError("Output must be a dedicated directory beneath a temporary directory; this tool cannot write live state")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Scratch destination already exists; choose a fresh output directory")
    return destination


def build_proposal(config_dir: Path, status_file: Path, recorded_at: str, analysis_file: Path | None = None,
                   waste_file: Path | None = None) -> dict:
    from quota_router import calibrations
    from quota_router.config import load_config

    status = json.loads(status_file.read_text())
    observed_at = status["generated_at"]
    utc(observed_at)
    if utc(observed_at) > utc(recorded_at):
        raise ValueError("The reconstruction cannot predate its status evidence")
    observed_tiers = {account["id"]: account["tier"] for account in status["accounts"]}
    historical_tiers = dict(observed_tiers)
    if waste_file is not None:
        for line_number, line in enumerate(waste_file.read_text().splitlines(), 1):
            row = json.loads(line)
            tier = row["basis"]["tier"] if row["v"] == 3 else row["tier"]
            # v1 did not store provider; canonical Claude account ids identify the
            # tier evidence without inventing a provider field in the old row.
            if (row["account"] == "claude" or row["account"].startswith("claude_")) and tier != "unknown":
                previous_tier = historical_tiers.get(row["account"])
                if previous_tier not in {None, "unknown", tier}:
                    raise ValueError(f"Waste row {line_number}: {row['account']} has changing tiers; explicit dated reconstruction is required")
                historical_tiers[row["account"]] = tier
    # Explicit input file only; neither the checkout nor the caller's XDG config layers can leak in.
    config = load_config(explicit_path=config_dir / "config.toml", cwd="/",
                         env={"XDG_CONFIG_HOME": str(config_dir / ".no-implicit-config")})
    records = historical_measurements(recorded_at) + historical_code_encodings(recorded_at)
    historical, warnings = backup_encodings(config_dir, recorded_at)
    records.extend(historical)
    records.extend(documented_gemini_encodings(recorded_at))
    for index, value in enumerate(calibrations.loaded_values(config, observed_tiers=observed_tiers)):
        basis_at = observed_at if value["source"] == "snapshot" else recorded_at
        records.append(encoding(f"encoding-reconstruction-baseline-{index:03d}", value["account"],
            value["key"], value["value"], recorded_at, basis_at,
            ["config.toml (read for this reconstruction)", "status generated_at=" + observed_at],
            "Current loaded encoding, not a claim of measurement or an exact historical change time. "
            "Unset capacity inherits the tier scale; code defaults remain unmeasured for accounts not covered by evidence.",
            lower=basis_at, upper=basis_at, source=value["source"], scope=value["scope"]))
    if analysis_file is not None:
        analysis = json.loads(analysis_file.read_text())
        # The re-analysis owns its exact measurement rows, so its interval exclusions and caveats survive unchanged.
        proposed = analysis["ledger_measurements"]
        if not isinstance(proposed, list) or not proposed:
            raise ValueError("Gemini analysis must supply non-empty ledger_measurements")
        if len(proposed) != 2 or {r["account"] for r in proposed} != {"antigravity_gemini", "antigravity_gemini_b"}:
            raise ValueError("Gemini analysis must supply exactly one measurement for both Gemini pools")
        for row in proposed:
            if row["kind"] != "measurement" or row["key"] != "weekly_pool":
                raise ValueError("Re-analysis may supply only Gemini weekly_pool measurement records")
            row = dict(row, recorded_at=recorded_at, reconstructed=True)
            records.append(row)
    else:
        warnings.append("October 12 Gemini dollar re-analysis has not been supplied; this proposal is incomplete and cannot initialize detection")
    beliefs = historical_beliefs(recorded_at, historical_tiers)
    if analysis_file is not None:
        for row in beliefs:
            if row["status"] == "unresolved":
                row["measurement_ids"].extend(r["id"] for r in proposed if r["account"] == row["account"])
    if waste_file is None:
        warnings.append("Historical waste tiers were not supplied; disabled accounts may lack a tier-supported belief")
    records.extend(beliefs)
    for index, row in enumerate(records):
        calibrations.validate_record(row, records[:index])
    return {"recorded_at": recorded_at, "complete": analysis_file is not None and waste_file is not None,
            "operator_gates": ["Approve a backup and live reconstruction before installing any v3 writer",
                               "Choose the Gemini belief for the window ending October 16; it remains unresolved"],
            "counts": dict(Counter(row["kind"] for row in records)), "warnings": warnings, "records": records}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--recorded-at", required=True, help="Explicit UTC write timestamp; never derived from file mtime")
    parser.add_argument("--gemini-analysis", type=Path)
    parser.add_argument("--waste-file", type=Path, help="Read historical account tiers from the immutable raw reset log")
    parser.add_argument("--output-state-home", type=Path, help="Optional fresh scratch directory under the OS temporary directory")
    args = parser.parse_args()
    destination = check_scratch_destination(args.output_state_home) if args.output_state_home else None
    proposal = build_proposal(args.config_dir, args.status_file, args.recorded_at, args.gemini_analysis, args.waste_file)
    if destination is not None:
        if not proposal["complete"]:
            raise ValueError("Cannot initialize an incomplete reconstruction; supply --gemini-analysis and --waste-file")
        from quota_router import calibrations
        destination.mkdir(parents=True, exist_ok=False)
        env = {"XDG_STATE_HOME": str(destination)}
        calibrations.append_records(proposal["records"], env=env)
        calibrations.mark_reconstruction_complete(env=env, now_s=utc(args.recorded_at).timestamp(),
            reason="Reviewed historical reconstruction in scratch; live adoption still requires approval and backup")
        proposal["scratch_state_home"] = str(destination)
    print(json.dumps(proposal, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
