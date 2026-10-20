# Reconstructing the calibration ledger

The reconstruction tool produces a proposal for the operator's private state, not
an amendment to `waste.jsonl`. The operator-approved proposal was adopted on
October 20, 2026, as recorded below. `ops/reconstruct-calibrations.py` reads the supplied config,
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
  change. In the initial proposal, the second Claude/GPT pool's configured 0.088
  stays an encoding, without a measured size belief. A later operator belief applies
  the primary pool's size to it, as recorded below. Codex and other default inheritors
  gain no measured belief.
- **Initial Gemini beliefs:** both pools have an unresolved historical weekly-pool belief,
  including the October 16 resets, followed by the operator's settled-value belief
  from the October 18 experiment onward. That boundary says where the later
  measurement applies; it does not assert when a provider change occurred. Neither
  the new analysis nor an encoding change chooses the unresolved belief. The
  October 20 operator decision was appended separately, retaining these records.

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

## The historical Gemini rows

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
These proposal figures use the rounded v2 remainders. Both rows were excluded
from current-belief totals until the October 20 decision was appended. The v3
writer subsequently recovered unrounded readings from retained history; the
current figures below use those readings.

## Approved live adoption, October 20

The approved deployment used revision `7f8788769547cb10fe5b0eca3087e520f9a64137`.
All times in this section are UTC. The full backup, exact commands, raw responses,
validation and review reports remain in the private adoption record.

1. The state backup completed at `05:32:35Z`: one attempt, one success, zero
   failures. Full source-before, source-after and backup manifests agreed.
2. At `05:32:42.935565Z`, the CLI appended 137 reconstruction records:
   16 measurements, 109 encodings and 12 beliefs. Validation passed and the
   reconstruction-complete marker was valid. `waste.jsonl` was byte-identical
   to the backup at this point. The CLI stamped the actual append time rather
   than copying the proposal's earlier `recorded_at`.
3. Installation from clean `repo-prod` completed at `05:34:10.637910Z`, using
   `uv tool install --force --reinstall --no-cache`. The receipt named
   `repo-prod`; all 44 installed Python source files matched the pinned revision,
   with zero mismatches and no extra source files. The installed copy exposed
   `WASTE_VERSION = 3` and `detect_config_changes`.
4. The ordinary scheduled poll ran the new writer. The before and after evidence
   was:

   | Check | Before installation | After installation |
   |---|---|---|
   | Status `generated_at` | `05:33:11Z` | `05:35:25Z` |
   | Accounts | 11 | 11 |
   | Raw reset rows | 172: 86 v1 and 86 v2 | 240: original 172 plus 68 v3 |
   | Ledger records | 137 | 137 |

   The completed job's exit status was 0. All 68 appended rows had validated raw
   and basis blocks, including the weekly companion on Fable rows, and preserved
   the earliest recorded basis. The entire original 95,287-byte reset-log prefix
   remained unchanged. Calibration detection emitted zero warnings and added no
   unexplained encoding records. The scheduled capability refresh at `05:37:54Z`
   reported 10 attempted, 10 succeeded, 0 failed, with no failure kinds.
5. After repeating the current/logged/as-of checks against copied post-poller
   state, the installed CLI appended the operator's two Gemini beliefs at
   `05:42:21.199671Z`. The ledger then held 139 records: 16 measurements,
   109 encodings and 14 beliefs. Its marker remains valid and all earlier records
   remain intact. This belief append left `waste.jsonl` unchanged.

The launchd plist and its 120-second interval stayed unchanged; it still has no
window-ping invocation. The installation did not include the later waiting-capacity
writer or ping changes. Those deployments remain separate.

The installed `quotapick calibrations --json`, `quotapick waste --json`, and
`quotapick waste --beliefs-as-of 2026-10-20T05:42:21.199671Z --json` checks all
succeeded: 3 attempted, 3 succeeded, 0 failed. The cutoff selects records written
strictly before the append and reproduces both unresolved predecessor beliefs.
Current reports resolve them as follows:

| Account | Chosen weekly PSE | Current forfeit PSE | Unchanged logged PSE |
|---|---:|---:|---:|
| `antigravity_gemini` | 9.72 | 9.68172129392624 | 6.225387 |
| `antigravity_gemini_b` | 9.80 | 9.745963716506958 | 6.215538 |

The chosen basis and its evidence are detailed in the
[Gemini decision record](2026-10-19-gemini-reanalysis.md#operator-decision-and-live-beliefs-october-20).
The pre-deployment test gate, with both XDG homes in scratch storage, was
`./.venv/bin/python -m pytest -q`: `1563 passed, 3 deselected in 17.63s`.
A fresh reviewer returned `ship` before the first live write. A later scoped
review also returned `ship` before the belief append, after its verification
calculation was updated for recovered raw precision. No live operation failed.
Installation waited for an active poll to finish before invoking uv; no job was
signalled and no rollback was needed.

## Additional second-pool belief, October 20

The operator subsequently decided that the second Claude/GPT pool has the same
0.088 PSE five-hour size as the measured primary pool, for all time. The reason is
their shared plan and model group, supported by the primary pool's measurement.
The October 18 config already encoded both as 0.088. This is a belief about the
second pool, not a measurement of it.

After a scratch check and a fresh pre-write `ship` verdict, the installed CLI
appended `belief-antigravity_claude_b-capacity-same-plan-20261020` at
`2026-10-20T06:01:36.625472Z`. It has `key: capacity`, `value: 0.088`, and null
`valid_from` and `valid_to`. Its measurement reference is
`measurement-agy-claude-capacity`. The evidence timestamp
`2026-10-20T05:49:10Z` is the recording time of operator-decision commit
`18da47d43c3a32108e04f00f0f0b6edc9d3a9860`; the reason distinguishes that from
the unknown exact time of the human decision.

The ledger now contains 140 records: 16 measurements, 109 encodings and 15 beliefs.
A supplemental copy of the previous ledger, marker and reset log was saved
privately before the append. All 139 earlier records, the marker and the reset
log remained unchanged. A cutoff at the new append time reproduces the full prior
report. The only changed reset row is the second Claude/GPT pool's October 15
weekly reset: current waste changes from 1.8425697088241577 to
0.16214613437652586 PSE, while logged waste remains 1.84257 PSE.
The raw check is `(1 - 0.07871514558792114) * 2 * 0.088`.
Gemini rows remain unchanged.

The [saved waiting-capacity proposal](2026-10-20-forgone-capacity.md) was also
recalculated in scratch with all approved beliefs: 19 records attempted,
19 validated, 0 failed. The second Claude/GPT pool contributes
0.0883416402116402 PSE, and the total becomes 4.265023712993716 PSE.
Its raw-history adoption followed on October 20 under #32. The writer was installed
the same day under #29.
This additional belief append did not install code or change the schedule.

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
any v3 writer is installed**, and installation must use `repo-prod`. These gates
were fulfilled on October 20. Installing the initial ledger alone did not resolve
the October 16 Gemini choice; the separately approved belief append did.
