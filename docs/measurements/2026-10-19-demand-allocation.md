# Sharing demand across accounts, measured 2026-10-19

The old objective materially understates predicted waste. With two measured Gemini
pools holding 19.3248 PSE and 144 hours left, it predicts **zero waste**, because each
pool receives the entire 0.07 PSE/hour demand credit. Sharing that credit in proportion
to remaining quota predicts **9.2448 PSE** left at reset.

This is a deterministic offline experiment for BACKLOG item 25. It establishes a
prediction error, not a reduction in total wasted quota: both routing rules waste
7.5648 PSE in the separate seven-day simulation below.

## Reproduce

From the repository root, using its existing virtual environment:

```sh
replay_scratch=$(mktemp -d /tmp/quota25-replay.XXXXXX)
XDG_CONFIG_HOME="$replay_scratch/config" \
XDG_STATE_HOME="$replay_scratch/state" \
PYTHONPATH=src .venv/bin/python ops/replay-demand-allocation.py > "$replay_scratch/result.json"
cmp "$replay_scratch/result.json" docs/measurements/2026-10-19-demand-allocation.json
rm -r "$replay_scratch"
```

The [script](../../ops/replay-demand-allocation.py) uses production `replay.simulate`,
`select`, and `account_score`. Scoped rank adapters preserve both allocation rules;
the original functions are restored afterward. Every proportional evaluation also
checks production `scoring.rank` for matching order, applied rates, and scores.
Selection hysteresis, five-hour caps, and the 2% eligibility floor remain active.
No network requests or operator-state reads occur.

The [raw results](2026-10-19-demand-allocation.json) reproduced byte for byte after the
production change. SHA256:
`6dca5a26809db387336f1292709d805f40a35aaf191ac0379f776fad607c35ac`.

## Gemini scenario

The measured configuration is `weekly_to_session = 4`, with session capacities
2.43 PSE for `antigravity_gemini` and 2.45 for `antigravity_gemini_b`; see the
[capacity measurement](2026-10-18-gemini-size.md). This gives weekly capacities of
9.72 and 9.80 PSE. Both synthetic starting bars are 1% used, leaving 9.6228 and
9.7020 PSE.

Both pools serve one Gemini demand group. Demand is a constant 0.07 PSE/hour, with
one prior synthetic week supplying the causal trailing forecast. The counted week
has hourly bins and 0.01 PSE jobs. Both weekly deadlines are fixed at its end.
The old rule credits each eligible account with the group rate. The proposed rule
divides that rate in proportion to eligible accounts' remaining weekly PSE. Both
rules cap the group rate at 0.2 PSE/hour before scoring or allocation.

### The 144-hour checkpoint

This checkpoint uses the starting stocks above with a 144-hour deadline. It is a
separate, same-state calculation, not day 2 of the week simulation: no preceding
day's work has been subtracted here.

| Pool | Remaining PSE | Old rate, PSE/hour | Split rate, PSE/hour | Old predicted waste, PSE | Split predicted waste, PSE |
|---|---:|---:|---:|---:|---:|
| `antigravity_gemini` | 9.6228 | 0.070000 | 0.034857 | 0.0000 | 4.6035 |
| `antigravity_gemini_b` | 9.7020 | 0.070000 | 0.035143 | 0.0000 | 4.6413 |
| Total | 19.3248 | 0.140000 | 0.070000 | 0.0000 | 9.2448 |

The shared work can consume 10.08 PSE over 144 hours. The old rule credits the two
pools with twice that demand. The proportional prediction conserves the shared
rate and leaves exactly `19.3248 - 10.08 = 9.2448` PSE.

### Seven replayed days

Each row evaluates both prediction rules on the **same state at that day's start
along the old rule's simulated trajectory**. The raw JSON also contains the paired
predictions along the proportional trajectory. Attempt counts are identical under
both routing rules. Counts describe simulated job chunks, not provider calls; a
failure would mean an unserved job-sized chunk. The script checks served and unmet
totals against production replay results rather than hiding failed demand.

| Day | Hours to reset | Attempted | Succeeded | Failed | Old predicted waste: Gemini / Gemini B, PSE | Split predicted waste: Gemini / Gemini B, PSE |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 168 | 168 | 168 | 0 | 0.0000 / 0.0000 | 3.7669 / 3.7979 |
| 2 | 144 | 168 | 168 | 0 | 0.0000 / 0.0000 | 3.4053 / 4.1595 |
| 3 | 120 | 168 | 168 | 0 | 0.0000 / 0.0620 | 3.5551 / 4.0097 |
| 4 | 96 | 168 | 168 | 0 | 0.4328 / 0.4120 | 3.7879 / 3.7769 |
| 5 | 72 | 168 | 168 | 0 | 1.1828 / 1.3420 | 3.7346 / 3.8302 |
| 6 | 48 | 168 | 168 | 0 | 2.1628 / 2.0420 | 3.8242 / 3.7406 |
| 7 | 24 | 168 | 168 | 0 | 2.8728 / 3.0120 | 3.7254 / 3.8394 |

Each rule attempts and serves 1,176 jobs, totaling 11.76 PSE, with zero unmet demand.
The proportional prediction sums to 7.5648 PSE at every daily checkpoint, matching
the aggregate remainder at reset. The old prediction starts at zero.

| Routing rule | Gemini reset waste, PSE | Gemini B reset waste, PSE | Total reset waste, PSE | Switches |
|---|---:|---:|---:|---:|
| Whole group credited to each account | 3.7728 | 3.7920 | 7.5648 | 25 |
| Proportional remaining quota | 3.8628 | 3.7020 | 7.5648 | 35 |

This scenario has enough capacity to serve all demand under either rule. With the
same starting stock and fixed deadlines, changing its allocation cannot change total
reset waste. It changes which pool holds that waste and how often routing switches.
The neutral tail is 7.76 PSE under both rules; replay's `waste + tail` metric is
15.3248 PSE under both.

## Synthetic Claude sensitivity

The full synthetic fleet has four Max-20x pools, each with weekly capacity 6.25 PSE,
20% initially used, and weekly resets 24, 72, 120, and 168 hours into the count.
The tighter fleet removes the first pool and retains all demand. Every day has
three hourly bursts of 1.1 PSE, followed by 21 idle hours: 23.1 PSE of demand per
week. A prior week supplies the same causal forecast. These bursts exercise the
five-hour limit.

Each cell below is **old → proportional**. Metric is `waste + tail`; unmet demand
is reported separately. Forecast scaling changes what the scorer predicts, not
the simulated demand. The 0.2 PSE/hour ceiling applies before sharing the rate.

| Fleet | Job PSE | Forecast multiplier | Metric PSE | Unmet PSE | Switches |
|---|---:|---:|---:|---:|---:|
| Full | 0.01 | 0.5 | 6.320 → 6.310 | 0.340 → 0.340 | 31 → 54 |
| Full | 0.01 | 2.0 | 6.310 → 6.310 | 0.340 → 0.340 | 27 → 31 |
| Full | 0.05 | 0.5 | 6.200 → 6.200 | 0.300 → 0.300 | 29 → 49 |
| Full | 0.05 | 2.0 | 6.200 → 6.200 | 0.300 → 0.300 | 27 → 31 |
| One 20x removed | 0.01 | 0.5 | 2.300 → 2.300 | 4.480 → 4.480 | 22 → 19 |
| One 20x removed | 0.01 | 2.0 | 2.300 → 2.300 | 4.480 → 4.480 | 17 → 21 |
| One 20x removed | 0.05 | 0.5 | 2.200 → 2.200 | 4.300 → 4.300 | 19 → 19 |
| One 20x removed | 0.05 | 2.0 | 2.200 → 2.200 | 4.300 → 4.300 | 17 → 21 |

Unmet demand is unchanged in every comparison. The metric improves by 0.01 PSE in
one case and is unchanged elsewhere; this small difference is not evidence of a
general waste reduction. Switching increases in six cases, decreases in one, and
is unchanged in one.

## Limits

Demand, starting usage, and deadlines are synthetic. No historical fidelity claim
is made. Fixed weekly deadlines omit Antigravity's sliding and early resets. The
Gemini experiment contains only its two compatible pools; it does not measure
model-group separation or cross-provider substitution.

The Claude cases exercise the same job-size and forecast-scale axes as BACKLOG
item 3, but **do not satisfy its historical replay gate** or justify changing the
default to the joint objective. Item 3 remains open. Item 4's cross-provider routing,
exchange rates, and early-reset hazards are also unresolved by this experiment.

## Implementation validation

The checkout gate, with both XDG roots pointed at scratch directories, reported:

```text
./.venv/bin/python -m pytest -q
1416 passed, 3 deselected in 16.67s
```

The allocation regression failed before the production change. Tests now pin rate
conservation, eligibility filtering, holds, calibrated sizes, separate model groups,
cache migration, replay group consistency, and the per-account explain output.
Existing calibration assertions now use proportional demand arithmetic. Reserve and
deprioritization fixtures retain a natural first-account winner so they still prove
that those policies change a pick. The earlier-deadline regression remains green;
no pick golden files were regenerated.

The first adversarial review required a stronger terminal replay assertion: comparing
two relabeled runs could hide the same broken group assignment in both. The test now
independently expects `5.625 - (0.625 / 168) * 161 = 5.0260416667` PSE at the deadline.
A process-local mutation dropping the stock's group assignment fails that assertion
with 5.625 PSE; the unmodified implementation passes. Three stale descriptions of
forecast groups were also corrected. No production repair was needed in that round.
