# Antigravity capacity, measured 2026-10-12

One Antigravity Claude/GPT pool holds about **8.8% of a Claude Code Max-20x account per
five-hour window**, and about **2.8% per week**. The two ratios differ because an
Antigravity week holds only 2 five-hour windows, and a Max-20x week holds 6.25.

All dates and times are UTC. A PSE is one Max-20x five-hour window, the unit the router
uses for every quota (see `src/quota_router/pse.py`).

## Results

Each Google account has its own Antigravity Claude/GPT pool. The figures are for one pool.

| | Antigravity Claude/GPT pool | Claude Code Max-20x | Ratio |
|---|---|---|---|
| One five-hour window | $25.04 = **0.088 PSE** | $283.20 = 1 PSE | 8.8% |
| One week | $50.07 = **0.177 PSE** | $1,770 = 6.25 PSE | 2.8% |
| Five-hour windows per week (k) | **2.0** | 6.25 | |

If the second Google account's pool is the same size, which was not measured, both pools
together hold about 0.354 PSE a week. That is about 5.7% of one Max-20x week.

Dollars are Opus 5.5 work at Anthropic list prices. Both sides ran Opus 5.5, so equal
dollars means equal work. On the Claude side, every call's telemetry named
`claude-opus-5-5`. On the Antigravity side, agy (the Antigravity command-line tool)
records a model code, not a name, and an earlier scan mapped that code to Opus 5.5.

**The Gemini pools.** k = 15.0 on both Google accounts. Their size in PSE was not
measured.

**What uses these numbers today:**
- `weekly_to_session = 2.0` is in the operator config for both Antigravity Claude/GPT
  pools.
- The router still sizes every Antigravity window at capacity 1.0, because no config key
  can hold 0.088. See BACKLOG item 9.
- Gemini k = 15 is not applied. See BACKLOG item 10.

## Method

### The Max-20x side: $283.20 per five-hour window

- **Account:** `claude_c`, a Max-5x plan.
- **Work:** a fixed workload in 3 reps, each of 9 calls. All 27 calls succeeded, and all
  were served by `claude-opus-5-5`.
- **Measure:** dollars of work divided by the points its five-hour bar moved.

| Rep | $ per point | Implied Max-20x window |
|---|---|---|
| rep-r1 | 0.7611 | $304.43 |
| rep-r2 | 0.6280 | $251.21 |
| rep-r3 | 0.7509 | $300.36 |

- **Pooled result:** total dollars divided by total points across the 3 reps gives
  $0.7080 per point. The per-rep mean is $0.7133 with sd $0.0741.
- **Scale-up:** $0.7080 / (0.01 × 0.25) = $283.20. The 0.25 is the router's tier scale
  for Max-5x.
- **Pricing check:** Claude Code's own `total_cost_usd` matched the price computed from
  token counts on every call.

**Excluded reps:**
- `pilot-c4` read $0.107 per point. Five sessions from another workload were busy on the
  account during the rep, so the bar moved for work the rep did not do.
- `pilot-p1` read $0.624 per point. It was a pilot in a different mode, and only fixed
  reps are pooled. With it, the pooled figure is about $0.691, and the Antigravity
  window becomes about 0.091 PSE.

### The Antigravity side: $50.07 per week, k = 2.0

- **Account and model:** `antigravity_claude`, running Opus 5.5 (High) through `agy`.
- **Weekly size:** each rep priced the tokens of its work and divided by how far the
  weekly bar moved.

| Rep | pilot-a1 | rep-a3 | rep-a4 | rep-a5 | rep-a6 |
|---|---|---|---|---|---|
| $ per full week | 50.04 | 50.19 | 50.04 | 50.04 | 50.04 |

- **The pilot is pooled here.** Unlike the Claude side, the Antigravity mean includes
  its pilot rep. Without it, the mean is $50.08 instead of $50.07.
- **Replica choice:** these figures come from replica X of agy's `/usage`. The other
  replica gave $50.04, $35.46, $48.65, $50.27 and $48.84 for the same reps. See the
  hazards below.

- **Input pricing:** agy input tokens were priced as five-minute cache writes, at $5 per
  million. At the base input rate, the same reps give $40.56 to $49.37 and disagree with
  each other. An input-heavy rep confirmed the cache-write rate.
- **k:** in every rep, the five-hour bar moved exactly twice as far as the weekly bar.
  That held on 6 reps on `antigravity_claude` (sd 0.000005) and on 2 reps on
  `antigravity_claude_b`.
- **Second account:** the dollar size of `antigravity_claude_b` was not measured. Only
  its k was. Its 2 k reps have no served-model check, because the provenance read failed
  with `unable to open database file`.

### The ratio

$50.07 / 2 = $25.04 per Antigravity five-hour window, and $25.04 / $283.20 = 0.0884 PSE.
Using each Claude rep's own window value gives a range of 0.082 to 0.100.

## Uncertainty and assumptions

- **The meter quantum.** The Claude five-hour bar reads whole percents, and the three Claude
  reps moved it 5, 6 and 5 points. So each rep carries about ±18% error from rounding
  alone. The three reps give 0.082 to 0.100.
- **The tier scale.** The Max-5x to Max-20x step uses 0.25, which is an assumption.
  `claude_c` reads k = 9.8 where Max-20x accounts read about 6.25, so a Max-5x plan may
  not be exactly a quarter of a Max-20x plan. The size of this error is unknown, and it
  may be larger than the rounding error.
- **The input pricing.** The Antigravity size depends on pricing its input at the
  cache-write rate.
- **The replica.** The Antigravity size depends on reading replica X.
- **One account sized.** Only one Google account's pool was sized in dollars.
- **Overall:** no single error bound is claimed, because the tier-scale error is not
  quantified. To close the gap between 0.088 and the 1.0 that the router uses today,
  the tier scale would have to be wrong by about 11 times.

## Hazards for the next measurement

- **Two backend replicas answer `agy /usage`.** A 120 s poll lands on the other replica
  about 49% of the time. All k figures here come from one replica. The other replica read
  offsets that were not constant. The largest gap was 1.4% of the weekly pool, on one
  pair read during a rep, so lag may explain part of it. Never difference a reading
  against one from the other replica.
- **The Gemini five-hour bar sometimes jumps in one read**, out of step with the weekly
  bar. Of seven flagged jumps, five read 0.0100 to 0.0113 and two read 0.0009 and
  0.0058. So k
  from whole reps does not reproduce: it ranged from 6.5 to 31.9. Only consecutive reads
  gave a stable k:
  - `antigravity_gemini`: 13 of 16 changes, mean 15.0006;
  - `antigravity_gemini_b`: 12 of 16 changes, mean 15.0005.
- **Gemini 3.1 Pro left both accounts' model lists** between 21:30Z and 22:30Z. Fourteen
  calls then failed with `gemini-3.1-pro-high not recognized`, so the Gemini reps used
  Gemini 3.8 Flash.
- **The two Google accounts ran different agy versions:** 1.3.2 and 1.3.3.
- **Other work shares the Claude accounts.** Work from other projects on a Claude account
  moves its bar. Check for foreign busy sessions before and during every rep.
- **An idle Antigravity pool's weekly reset slides 168 hours ahead of the clock** at some
  times and stays fixed at others. This was seen after the measurement, and its cause is
  unknown. See BACKLOG item 9.

## Raw data

The harness, raw responses and analysis are in `measurements/agy-capacity-2026-10-12/`,
in the checkout container beside `repo-prod`, on the operator's machine. They are not in
this repository, because hundreds of the files name the operator's Google accounts. The
main files are listed below.

| File | What it holds |
|---|---|
| `analysis2-final.json` | the Claude arm, k, and the PSE ratio |
| `analysis-agy-scaled.json` | the Antigravity weekly-dollar reps |
| `analyze2.py`, `harness2.py` | the analysis and the harness |
| `LEDGER.txt` | every stop, exclusion and contamination |
| `config-eval.toml` | the router config used during the runs |
