# Gemini weekly pool, re-analysis of the 2026-10-12 raw run

The October 12 fine-movement estimates are **16.22 and 18.31 PSE per week**. The
same method on October 18 gives **19.42 and 19.56**. The ratios are 0.835 and
0.936, rather than the 3.75 ratio expected if the five-hour pool stayed fixed
while k fell from 15 to 4. This evidence is closer to an unchanged weekly pool.
It does not establish that belief: the primary pool differs by 16.5%, neither
fine estimate is settled, and complete independent activity evidence is missing.

On October 20 the operator chose the **unchanged weekly pool** belief, and it was
appended to the live ledger after a scratch check. The measurement remains a
separate record; it does not establish that choice by itself.
All times below are UTC. No provider requests were made for this re-analysis.

## Results

| Account | October 12 fine weekly PSE | October 18 fine weekly PSE | October 12 / October 18 | October 12 per-chain range |
|---|---:|---:|---:|---:|
| `antigravity_gemini` | 16.216900 | 19.423512 | 0.834911 | 13.675701–18.900009 |
| `antigravity_gemini_b` | 18.309661 | 19.562576 | 0.935954 | 15.224948–23.401056 |

The ranges describe disagreement between chains, not confidence intervals.
Every pooled estimate is **total fine dollars / total fine weekly movement /
283.20**, not an average of the chains' ratios. The October 18 comparison was
recomputed from its original `analyze5.py`; it reproduces the figures in
[the October 18 measurement](2026-10-18-gemini-size.md#the-meter-settles).

| Account | Actual Flash calls | Actual Flash dollars | Fine dollars across chains | Fine weekly movement across chains | Fine k |
|---|---:|---:|---:|---:|---:|
| `antigravity_gemini` | 80 | $8.508647325 | $11.075321400 | 0.002411544323 | 15.001236 |
| `antigravity_gemini_b` | 80 | $8.533679250 | $13.904957250 | 0.002681612968 | 14.999978 |

Calls can belong to one interval on each chain. The fine-dollar column therefore
contains repeated attribution of the same work to independent meter traces; it
is not additional spend. It covers 104 and 129 call-chain attributions after
exclusions. There are 160 actual Flash calls in all.

## Method and exclusions

`ops/reanalyze-gemini-oct12.py` reads all seven Gemini reps, including the partial
`k-g2`, and validates each saved usage point against its raw `/usage` JSON. It
asserts the requested model, response status and available served-model evidence
before pricing successful Flash calls.

The method preserves the October 18 `analyze5.py` choices:

- Track two chains by the minimum total L1 distance in weekly and five-hour
  remaining fractions. Ties follow sorted pair order and chain order, as in the
  original. A point with more than two distinct pairs would report the number
  omitted. No such omission occurred in this run.
- Attribute each call's dollars to the interval in which it ended.
- Estimate each chain's normal movement per dollar by the median over paid,
  eligible intervals. Reject an interval if the absolute residual exceeds
  **0.0005 weekly** or **0.002 five-hour**. These are inherited judgment calls,
  not estimated measurement errors.
- Exclude the **whole interval**, dollars and movement, if any Gemini 3.1 Pro
  attempt overlaps it. Even a failed Pro attempt makes the interval ineligible.
  Also exclude intervals starting before the first Flash rep's observations.
- Pool all remaining intervals over both chains, including intervals without
  calls. No retained no-call interval had nonzero meter movement.

| Account / chain | Intervals attempted | Retained | Excluded | Pro overlap | Before Flash span | Jump residual |
|---|---:|---:|---:|---:|---:|---:|
| `antigravity_gemini` / 0 | 36 | 19 | 17 | 4 | 8 | 5 |
| `antigravity_gemini` / 1 | 25 | 20 | 5 | 1 | 0 | 4 |
| `antigravity_gemini_b` / 0 | 42 | 27 | 15 | 9 | 3 | 3 |
| `antigravity_gemini_b` / 1 | 45 | 31 | 14 | 9 | 3 | 2 |
| **Total** | **148** | **97** | **51** | **23** | **14** | **14** |

The JSON includes every interval, its residuals, and its exclusion reason.
There is no sampling or silent cap.

The price basis is the **2026 schedule captured by the October 18 measurement**:
$0.75/million input tokens, $0.075/million cache-read tokens, and $3.75/million
output and thinking tokens. This re-analysis does not substitute today's pricing
for that recorded schedule. The PSE reference is the October 12 Claude estimate
of $283.20 per Max-20x five-hour window; its tier-scale and meter-quantum
uncertainty also carries through.

## Attempts, identity and other usage

| Rep | Requested model | Calls attempted | Succeeded | Failed | Usage reads attempted | Succeeded | Failed |
|---|---|---:|---:|---:|---:|---:|---:|
| `k-g1` | 3.1 Pro | 3 | 0 | 3 | 29 | 28 | 1 |
| `k-g2` (partial) | 3.1 Pro | 3 | 0 | 3 | 22 | 22 | 0 |
| `k-g3` | 3.8 Flash | 40 | 40 | 0 | 56 | 56 | 0 |
| `k-g4` | 3.8 Flash | 40 | 40 | 0 | 60 | 60 | 0 |
| `k-gb1` | 3.1 Pro | 40 | 32 | 8 | 48 | 48 | 0 |
| `k-gb2` | 3.8 Flash | 40 | 40 | 0 | 83 | 80 | 3 |
| `k-gb3` | 3.8 Flash | 40 | 40 | 0 | 60 | 60 | 0 |
| **Total** | | **206** | **192** | **14** | **358** | **354** | **4** |

All 14 failed calls report `model_not_recognized`. Four raw usage responses
report errors and never enter a point. Two successful raw reads in the interrupted
`k-g2` have no completed point; they are reported as
`unassigned_partial_point_successes`, and are not used to construct an interval.
The other 352 successful reads form the points used by the analysis.

For the primary account, all 80 Flash calls carry served enum **1318**. Of these,
78 carry Google-style response ids; two have empty ids. The stored evidence
names the requested Flash model but does not independently map enum 1318 to a
model name. On the second account, all 80 calls have the CLI served label
**Gemini 3.8 Flash (High)** and Google response ids. Its independent conversation
store was unreadable during the run. These are limits on identity verification,
not silently assumed name matches. The original run used agy 1.3.2 and 1.3.3.

All six completed reps' saved router-history excerpts contain **zero picks of
either Gemini pool**. Other picks target the separate Claude/GPT pool or have no
chosen account. The partial `k-g2` has no completed history excerpt. There is no
complete surviving audit of direct CLI or web activity, so zero routed picks
cannot prove that no other work used the pools. The measurement records retain
this caveat; they do not claim isolation was established.

## Late Pro carryover

The last successful Pro call on the second account ended at
**21:39:30.964785Z**. Its first Flash call started at **21:55:23.806274Z**.
The tracked chains show no weekly or five-hour movement during the available
pre-Flash observations from approximately 21:42 to 21:55. This is evidence of a
quiet period, not proof that every Pro charge had settled: the October 18 run
shows that settlement can take hours.

As a sensitivity check, exclude **all of `k-gb2`**, including any interval
crossing from it into `k-gb3`. The second-account weekly estimate becomes
**18.595867 PSE**, versus 18.309661 with both Flash reps. This does not reveal a
large early-Pro contamination effect, but delayed Pro charges could still appear
in both reps. The later rep starts at 22:47:21.694139Z, about 68 minutes after the
last Pro call; that elapsed time is not asserted to be a settlement boundary.

For the primary account, the second Flash rep alone gives **20.910184 PSE**,
versus 16.216900 pooled. Its Pro attempts all failed. The variation between Flash
reps (13.291601 and 20.910184) and between chains remains visible rather than
being removed by another filter.

## Candidate beliefs for the two October 16 rows

The two historical rows keep their original readings. Their logged rounded
remainders are 0.996062 and 0.994486. The early-reset instants are
22:15:24.578925Z on `antigravity_gemini` and 22:06:28.660731Z on
`antigravity_gemini_b`, both on October 16.

| Candidate belief | Weekly pools, primary / second | Primary row | Second row | Both rows |
|---|---|---:|---:|---:|
| Weekly pool stayed the same: k 4 × windows 2.43 / 2.45 | 9.72 / 9.80 PSE | 9.68172264 PSE | 9.74596280 PSE | 19.42768544 PSE |
| Five-hour pool stayed the same: k 15 × windows 2.43 / 2.45 | 36.45 / 36.75 PSE | 36.30645990 PSE | 36.54736050 PSE | 72.85382040 PSE |

These candidates use the settled October 18 capacities already encoded by the
operator. They are not comparisons of settled October 18 values with fine
October 12 values. The like-for-like fine comparison is the first table.
Neither candidate is selected by this script. The initial reconstruction retains
both candidates as unresolved. The later operator decision is a separate append.

## Operator decision and live beliefs, October 20

The operator chose weekly pools of **9.72 and 9.80 PSE** for the window ending
October 16. The recorded rationale compares the October 12 fine estimates with
the October 18 fine estimates, which are closer to unchanged weekly capacity
than to the other candidate's 3.75-fold difference. The operator accepted that
interpretation with the settlement and other-activity caveats above; it is also
the smaller candidate. Neither the measurements nor the old belief were removed.

The installed CLI appended these records at `2026-10-20T05:42:21.199671Z`:

- `belief-antigravity_gemini-weekly-unchanged-20261020`
- `belief-antigravity_gemini_b-weekly-unchanged-20261020`

Each is a resolved `weekly_pool` belief with `valid_from: null` and
`valid_to: 2026-10-18T23:02:36Z`, matching the predecessor's historical interval.
The later October 18 beliefs remain unchanged. Values k=4 and window sizes
2.43 / 2.45 encode the chosen weekly totals; they are not a new measurement of
the historical five-hour size. Reasons and evidence retain the October 12 and
October 18 k runs, settled capacities and fine re-analysis records.
`evidence_at: 2026-10-20T05:15:20Z` is the UTC recording time of decision commit
`523772a1c1d95e7957a9cb8c1ead40c6c6d19e5b`, not an invented exact time for the
human's decision.

The first v3 poll recovered unrounded readings. These produce slightly different
forfeits from the rounded v2 inputs in the candidate table above:

| Account | Current forfeit PSE | Unchanged logged PSE |
|---|---:|---:|
| `antigravity_gemini` | 9.68172129392624 | 6.225387 |
| `antigravity_gemini_b` | 9.745963716506958 | 6.215538 |

The scratch check independently multiplied the raw remaining fraction by the
chosen weekly size, compared current and logged results, and reproduced the
unresolved predecessor with a cutoff before the append. The installed commands
`quotapick waste --json` and
`quotapick waste --beliefs-as-of 2026-10-20T05:42:21.199671Z --json` reproduced
those results after adoption. The latter uses records written strictly before
the cutoff. See the [adoption record](2026-10-19-calibration-reconstruction.md#approved-live-adoption-october-20)
for backup, installation and scheduled-poller validation.

## Reproduction and proposed ledger measurements

Run from a checkout; replace the input directory with the private measurement
folder and the stamp with the actual UTC writing time. This reads files and
prints JSON only:

```sh
./.venv/bin/python ops/reanalyze-gemini-oct12.py \
  /path/to/measurements/agy-capacity-2026-10-12 \
  --recorded-at 2026-10-19T23:26:23Z > /tmp/gemini-reanalysis.json
```

The output contains two `ledger_measurements`, with ids
`measurement-antigravity_gemini-oct12-fine-weekly` and
`measurement-antigravity_gemini_b-oct12-fine-weekly`. Each records its actual
Flash-call time span, the raw evidence's own date, the supplied writing time,
`reconstructed: true`, the method, evidence and caveats. The `weekly_pool` value
stores k = 15 and an implied window size equal to this fine weekly estimate / 15.
This representation does **not** assert a settled five-hour capacity. Descriptive
per-chain weekly-PSE ranges remain in the caveats; `spread` is null because no
combined statistical error bound is supported.

These are new measurements, not corrections of the October 18 experiment and
not provider-change beliefs. The reconstruction tool can ingest them without
choosing either candidate. The ledger stays in operator state, outside git.
Its approved reconstruction was backed up and adopted on October 20 before
installing the v3 writer; the two decided beliefs were appended afterwards.
Any future reconstruction still requires operator approval and backup.
