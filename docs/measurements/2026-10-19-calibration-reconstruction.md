# Reconstructing the calibration ledger

The reconstruction is a proposal for the operator's private state, not an amendment
to `waste.jsonl`. `ops/reconstruct-calibrations.py` reads the supplied config,
timestamped backups, a saved status response, historical reset tiers, and the
[Gemini re-analysis](2026-10-19-gemini-reanalysis.md). It emits every proposed ledger
record as JSON. By default it writes nothing. It can initialize a new **scratch**
ledger with `--output-state-home`, which must be beneath an OS temporary directory.
It cannot write the live ledger.

All records have an explicit reconstruction `recorded_at`, separate from the date of
their evidence and the interval they describe. A measurement whose exact run clock
was not preserved has null start/end times and a caveat. No timestamp is taken from
a file's mtime. A date-only backup retains its full UTC-day uncertainty; an undated
backup is listed as excluded from chronology. Nothing is silently sampled.

## What the proposal records

- **Experiments:** the old 11.9 estimate, the 6.25 Max-20x tier measurement that
  disputes it, the account-specific 9.8, the two Antigravity Claude k measurements,
  and the one measured Claude/GPT pool size. It also records both Gemini k runs,
  both end-of-run sizes and their settled corrections, and the two October 12
  fine-movement re-analysis results. That is 16 measurement records.
- **Encodings:** the code default changing from 12 to 6.25, the documented 0.5 Fable
  fraction, all four code tier scales, every recoverable explicit config change,
  initial inherited defaults, the Gemini adoption and correction, and a complete
  current baseline for `weekly_to_session`, `capacity`, `tier`, `tier_scale`, and
  `fable_fraction`. An encoding explains what the router used; it is not evidence
  that the account was measured. Code commit times do not establish live install times.
- **Beliefs:** 6.25 applies to each Claude account observed as Max-20x, including
  retired accounts found in the reset log. The original two measured accounts were
  unnamed: the reason is the tier evidence, not a claim that every current account
  participated. The account-specific 9.8, both measured Claude/GPT k values and the
  primary pool's measured size cover all time because there is no evidence of a
  change. The second Claude/GPT pool's configured 0.088 stays an encoding, without
  a measured size belief. Codex and other default inheritors gain no measured belief.
- **Gemini decisions:** both pools have an unresolved historical weekly-pool belief,
  including the October 16 resets, followed by the operator's settled-value belief
  from the October 18 experiment onward. That boundary says where the later
  measurement applies; it does not assert when a provider change occurred. Neither
  the new analysis nor an encoding change chooses the unresolved belief.

The measured 11.9 versus 6.25 disagreement comes from different experiments, so it
is a dispute. The end-of-run versus settled Gemini sizes come from the same
experiment, so the later measurement explicitly corrects the earlier one. Neither
operation erases an old record or implies that the provider changed its quota.

## Encoding bounds from the surviving backups

A backup contains the configuration **before** an edit. The following are bounds,
not precise edit clocks. The script also preserves earlier explicit values and
defaults, then adds the current loaded baseline.

| Setting | Lower bound, UTC | Upper bound, UTC |
|---|---|---|
| `claude_c` k 9.8 | unknown | date-only 2026-08-24 backup |
| `antigravity_claude` k 2.0 | 2026-10-12 20:50:17 | 2026-10-12 23:51:14 |
| `antigravity_claude_b` k 2.0 | 2026-10-12 23:51:14 | 2026-10-14 17:35:30 |
| both Claude/GPT capacities 0.088 | 2026-10-18 21:57:48 | 2026-10-18 23:00:00 |
| both Gemini k values 4.0 and provisional sizes | 2026-10-19 13:42:01 | 2026-10-19 20:33:00 |

The Gemini measurement document narrows the provisional encoding to the 13:42 UTC
minute, and records the settled correction in the 20:33 UTC minute. Separate
encoding records preserve that evidence with minute-wide bounds. The script does
not replace those bounds with an invented second.

## The unresolved rows

The immutable v2 rows retain their logged k 6.25 and size 1.0. The candidates use
the two interpretations specified by the operator; they are not rescalings chosen
by the script.

| Reset row | Logged PSE | Weekly pool unchanged | Five-hour window unchanged |
|---|---:|---:|---:|
| `antigravity_gemini`, October 16 22:15:24 UTC | 6.225387 | 9.681723 | 36.306460 |
| `antigravity_gemini_b`, October 16 22:06:28 UTC | 6.215538 | 9.745963 | 36.547361 |

The two candidates use weekly sizes 9.72 / 9.80 and 36.45 / 36.75 PSE respectively.
The [read-only re-analysis](2026-10-19-gemini-reanalysis.md) found fine weekly sizes
16.216900 / 18.309661 PSE, compared with the October 18 fine figures
19.423512 / 19.562576 PSE. These are measurements, not settled sizes or a decision.
Both rows remain excluded from current-belief totals until the operator decides.

## Reproduction and adoption

First run `ops/reanalyze-gemini-oct12.py` as documented in its measurement report.
Then supply the private inputs explicitly. These shell variables are placeholders
for paths selected by the operator; no machine-specific paths belong in the repo.

```sh
PYTHONPATH=src ./.venv/bin/python ops/reconstruct-calibrations.py \
  --config-dir "$CONFIG_EVIDENCE_DIR" \
  --status-file "$SAVED_STATUS_JSON" \
  --waste-file "$RAW_WASTE_JSONL" \
  --gemini-analysis "$GEMINI_REANALYSIS_JSON" \
  --recorded-at "$RECONSTRUCTION_UTC" > "$PROPOSAL_JSON"
```

Add `--output-state-home "$SCRATCH_STATE_HOME"` to create a fresh scratch ledger and
its reconstruction-complete marker after validating every record and reference.
The tool refuses an existing destination directory, a destination outside temporary storage, an
incomplete re-analysis, or contradictory historical tiers that need a dated belief.
The full proposal includes every ledger entry; keep it private with the config.

Completeness is checked against evidence content. The proposal lists attempted,
succeeded, failed and excluded backup reads and waste rows, with failure/exclusion
reasons, followed by the required coverage checks. It needs at least two dated
config snapshots with account evidence, the pre-August-25 `claude_c` override,
the before/after states supporting each October 12/18/19 transition in the table,
a nonempty status account list, both October 16 Gemini reset rows, and both Gemini
re-analysis measurements. Missing, empty or malformed required histories leave
`complete: false`; the tool refuses to create a reconstruction marker in that case.
An undated backup is a reported exclusion, not a fabricated chronological input.

Removing a builtin account table restores its inherited settings and is recorded
as a bounded encoding change. Removing a custom table makes that account inactive:
one `capacity: null` encoding with `source: account_absent` identifies that state,
with every removed setting in its reason and in the evidence manifest. It means
the entire account is absent, **not** that capacity falls back to a tier. Restoring
the custom table emits its settings again even when their values match those before
removal. These events retain both backup bounds.

Before live adoption, the operator must approve the concrete proposal and a backup
of existing state. The reconstructed ledger and its marker must be in place **before
any v3 writer is installed**, and installation must use `repo-prod`. The October 16
Gemini choice is a separate pending decision; installing the ledger does not resolve it.
