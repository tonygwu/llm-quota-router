# Antigravity Gemini pool size, measured 2026-10-18

One Antigravity Gemini pool holds about **2.4 PSE per five-hour window** and **9.8 PSE per
week**, priced at Gemini 3.8 Flash list prices for 2026. That is about 2.4 times a Claude
Code Max-20x window. **k is 4.0**, not the 15.0 that the 2026-10-12 measurement read.

All dates and times are UTC. A PSE is one Max-20x five-hour window, $283.20 of Opus 5.5
work at list prices (see `2026-10-12-antigravity-capacity.md`).

## Results

| Pool | Work at 2026 list | Settled weekly use | Weekly pool | Five-hour window at k = 4 |
|---|---|---|---|---|
| `antigravity_gemini` | $19.74, 181 calls | 0.0071598 | $2,757 = **9.74 PSE** | **2.43 PSE** |
| `antigravity_gemini_b` | $19.79, 181 calls | 0.0071317 | $2,775 = **9.80 PSE** | **2.45 PSE** |

- **Settled, not end-of-run.** The sizes come from the meter's settled values (see "The
  meter settles" below). The values read at the end of the run were provisional, and
  they gave 7.85 and 8.94 PSE a week.
- **2027 prices.** Google doubles the Gemini 3.8 Flash list price on 2027-01-01, so every
  PSE figure doubles then.
- **Before this measurement**, the router used the default k = 6.25 and the placeholder
  capacity 1.0, so it treated a Gemini week as 6.25 PSE.
- **What uses these numbers.** The operator config sets `weekly_to_session = 4.0` on both
  Gemini pools, and `capacity = 2.43` and `2.45`. **Operator decision 2026-10-19:** encode
  the settled values. The end-of-run values (1.96 and 2.24) were in the config from
  13:42Z to 20:33Z that day.

## Method

- **Work.** For each pool, a one-call pilot and then 3 reps of 60 calls, with both pools
  run in parallel. The model asked for was Gemini 3.8 Flash (High) through `agy`.
  Attempted 362, succeeded 362, failed 0.
- **Served model.** On `antigravity_gemini`, all 181 calls report agy model code 1318. 175
  of them have a response id of Google's form, and 6 returned an empty response id. No
  record maps code 1318 to a model name, so the served model is verified only as far as a
  Google backend and that code. On
  `antigravity_gemini_b`, all 181 report the label "Gemini 3.8 Flash (High)". The store
  cross-check could not open the other macOS user's database.
- **Routing.** Both Gemini pools were out of routing for the run, so no routed work
  shared them. An exit trap put them back.
- **Price.** The tokens of every call were priced at Google's list prices, read
  2026-10-18: input $0.75 per million, cached input $0.075, output and thinking $3.75.
  The 2027 schedule is double.
- **Reads.** Every usage point during the run is four `agy /usage` reads. The settled
  values come from the router's own poller history, one read every 2 to 3 minutes.

### Two replicas, tracked as chains

Two backend replicas answer `/usage`, and during the run both report fine-grained values
with a small offset between them. So the analysis tracks two chains per pool across all
reps. It assigns each point's distinct (weekly, five-hour) pairs to the chains by the
smallest total distance from each chain's last value. A step between two observations of
one chain is an interval, and the calls that ended inside it are its dollars.

The pilot made the first replica look as if it reported the five-hour bar in whole
percents (0.989997). That reading was a 1% jump, not a property of the replica.

### k = 4.0

Over 92 intervals with weekly movement above 0.00005, the five-hour bar moved between
3.992 and 4.007 times the weekly bar, on all four chains. The medians are 3.9994, 4.0000,
4.0000 and 3.9997. The settled values give 4.0001 and 4.0000.

The 2026-10-12 measurement read 15.0006 and 15.0005 the same way. Both results are round
to four digits, so the ratio is most likely configured, and it changed between the two
runs. Both Gemini pools reset early on 2026-10-16, at 22:06Z and 22:15Z. That reset is a
plausible marker of the change, but nothing proves it.

### The 1% jumps

During the run, each pool also shows jumps that the fine movement cannot explain:

- the five-hour bar drops by 0.0100 at once (0.009908 to 0.010021 in five clean cases;
  three more read 0.0040 to 0.0091, mixed with lag);
- the weekly bar drops by 0.0027 to 0.0037 (six cases; one more reads 0.0014, mixed with
  lag).

One replica takes a jump at a time. A five-hour jump and its weekly jump often land in
different intervals, and each replica takes them at a different time.

- On `antigravity_gemini`, one jump came with the first call of the five-hour window, and
  one came after about 125 of the 181 calls.
- On `antigravity_gemini_b`, one jump was already there before the run: a smoke test of
  another model opened that pool's five-hour window at 22:41Z. One chain took that jump
  only at 23:37Z, inside the run, although the smoke test's calls are not in the run's
  dollars. The second jump came after about 140 of the 181 calls.

The 2026-10-12 run saw the same thing: five of its seven flagged five-hour jumps read
0.0100 to 0.0113.

There was also a rise during the run. At 00:48:20Z, with no calls, one chain on
`antigravity_gemini` gained back 0.000646 of the weekly bar and 0.000129 of the five-hour
bar.

### The meter settles

About an hour after the run, each pool's replicas moved to one value, and the bars rose:

| Pool | Settled at | Weekly, five-hour remaining | Poller records at the settled weekly value |
|---|---|---|---|
| `antigravity_gemini` | 02:29:25Z | 0.984329820, 0.971360624 | 288 of 288, to 13:38:38Z |
| `antigravity_gemini_b` | 02:22:45Z | 0.986800015, 0.971473038 | 291 of 291, to 13:38:38Z |

Nothing used either pool in that time. The settled five-hour values held for 40 and 35
records, until the five-hour windows reset at 04:02:36Z and 03:41:43Z. The settled weekly use is measured from the last
read before 22:41Z: 0.991489589 on the primary and 0.993931711 on `_b`. So the `_b` figure
includes the smoke test's charge but not its dollars, which makes `_b` slightly smaller
than it is. Measured from the first read of the run instead (0.993901789), `_b` reads 9.84
PSE a week.

The settled charge is about twice the fine movement alone, and less than the end-of-run
values with their jumps:

| Pool | Fine movement only | Settled | End of run, per chain |
|---|---|---|---|
| `antigravity_gemini` | 19.42 PSE a week | **9.74** | 6.79 and 9.30 |
| `antigravity_gemini_b` | 19.56 PSE a week | **9.80** | 7.27 and 11.61 |

So the jumps are provisional: settlement takes back part of them. The settled values of the
two pools agree within 1%, where the end-of-run values differed by 14%. Read a Gemini
measurement only after the replicas agree and hold. BACKLOG item 21 carries what is still
unknown.

## Reset behavior seen

- **Five-hour window, unused, before the run.** On `antigravity_gemini`, 12 reads from
  23:00:17Z to 23:02:30Z gave the reset as the read time plus 5 hours plus about 3
  seconds, so it slid with the clock. The first call fixed it at 04:02:36Z, and it stayed
  fixed for the 224 reads that followed.
- **Five-hour window, after a scheduled reset with no use.** The next window starts fixed,
  then slides. Poller history, both pools, twice each:

  | Pool | Reset | Next reset, fixed until | Then |
  |---|---|---|---|
  | `antigravity_gemini` | 2026-10-18T19:52:27Z | 20:30:35Z | slides |
  | `antigravity_gemini` | 2026-10-19T04:02:36Z | 04:52:29Z | slides |
  | `antigravity_gemini_b` | 2026-10-18T19:52:32Z | 20:25:51Z | slides |
  | `antigravity_gemini_b` | 2026-10-19T03:41:43Z | 04:26:30Z | slides |

  That is 33 to 50 minutes fixed, the same fixed-then-slide pattern as the weekly
  question in BACKLOG item 22.
- **Weekly window.** Both pools had been used earlier in the week, and both weekly resets
  stayed fixed for the whole run. The run says nothing about an idle weekly window.

## What it changed in routing on the day: nothing

Dry runs for the fast, standard and premium capabilities picked the same account with
the same scores under the old values and two candidate sizes (k 3.9 with capacity 2.0 and
2.3; k 4.0 with capacity 4.9 and 5.0). That was at 2026-10-19T01:34:19Z, and again with
the live config at 13:42:28Z. The reason is the
fallback rule in `pick`. It credited a provider whose accounts are all
`unmetered = "fallback"` with the demand of the metered providers, capped at 0.2 PSE/h.
At 01:34Z that was about 29 PSE over the 6.1 days to the Gemini weekly reset, more than
any of the sizes, so `pick` projected no Gemini waste. A pool with no waste ranks by
deadline, and Gemini binds on its weekly window, so it ranked below every account whose
binding window reset sooner, and never first.

## Raw data

The harness, raw responses, analysis and dry-run outputs are in
`measurements/gemini-size-2026-10-18/`, in the checkout container beside `repo-prod`, on
the operator's machine. They are not in this repository, because the raw responses name
the operator's Google accounts. The settled values are in the router's history files for
2026-10-19 on the same machine.

| File | What it holds |
|---|---|
| `analyze5.py`, `analysis5.json` | the two-chain analysis and every interval |
| `README.md` | the findings in more detail |
| `reps/k/<pool>/<rep>/` | every call's request, response and served-model record, and every raw usage read |
| `LEDGER.txt` | every start, stop and routing change during the run |
| `dry/` | the pick dry runs |
| `analyze4.py` | superseded: its whole-percent replica rule held in the pilot only |
