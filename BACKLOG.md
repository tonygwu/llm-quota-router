# Backlog

Open gaps, roughly by how much they matter. Each entry says what would close it, so
"done" is checkable rather than a judgment call.

Kept free of machine-specific paths and account identities: publishing this repo is a
live option, and a backlog is the file most likely to leak them.

---

## 1. The core claim is now measurable, and not yet measured

The tool exists to reduce **wasted quota** — budget that expires unspent at a window
reset. Every justification to date has been mechanical ("the scorer now compares like
units") rather than empirical.

**What now exists.** `quota_router/waste.py` writes one durable, never-pruned row per
window reset per account to `waste.jsonl` — remaining fraction, the same remainder in
PSE, the `k` and tier scale it was computed with, and how long before the reset the last
reading was taken. `quotapick status` is the writer, so the launchd poller produces it
with no new scheduling; `quotapick waste [--json]` reports it, and `--backfill` re-scans
retained history for rows the series is missing. Resets that happened while nothing was
watching are written as `observed: false` with no remainder, so any total reads as a
lower bound rather than as an average over a hole.

Rows carry `kind` since version 2: a provider's one-off early reset is reported as
*forfeited*, apart from the waste total, because routing cannot plan for a boundary
nobody published. See the README section "An early reset is not waste".

**What is still open, in the order it blocks publishing:**

1. **Data exists now; the figure needs a decision.** As of 2026-10-12, every account
   that publishes usage windows has rows covering eight weekly cycles: 44 scheduled
   resets with 54.68 PSE expired unused (a lower bound, because 6 were missed), and 12
   early resets in 8 events with 37.06 PSE forfeited. The Antigravity pools had no
   readable windows until the `/usage` adapter, so their series starts when that ships.
   Two codex_b "missed" rows come from synthetic test data that a working checkout wrote
   into the live history on 2026-09-15. They carry no PSE.
2. **`--backfill` cannot reach backwards very far.** It reads `history.jsonl`, whose own
   rotation caps retention at 30 days, and history is currently only hours deep after the
   pruning incident. So the series effectively starts now.
3. **The counterfactual is not built, on purpose.** The publishing bar asks for router
   *versus the previous dial*, and only the absolute half exists. Building the other half
   needs three things this item does not have: the old dial's selection rule preserved as
   a callable (it was replaced, not kept); a replay that re-runs it over the recorded
   snapshot series to produce the account it *would* have picked; and — the hard part —
   a model of how a different pick would have changed subsequent *consumption*, since
   history records what was actually burned on the account that was actually chosen, not
   what would have burned elsewhere. Without that third piece a replay compares one real
   trajectory against an imagined one and the difference is unfalsifiable. Worth
   designing before building.
4. **Antigravity rows are logged at a placeholder size.** The first four rows, from
   2026-10-13 to 2026-10-16, use capacity 1.0. For the Claude/GPT pools that is about 11
   times the measured size: their two scheduled rows log 2.99 PSE, and the measurement
   gives 0.26. The two Gemini rows are early resets, so they stay out of the waste total,
   but they inflate the forfeited figure by a factor nobody has measured. #9 and #10 fix
   the sizes; fix them before the figure is stated.

**Closes when:** at least one full weekly cycle of resets is recorded for every account,
and the absolute wasted-PSE figure is stated. If the number is unimpressive, that is the
finding — publish it or shelve the project, but do not keep shipping scoring changes
justified by argument alone. The counterfactual comparison becomes its own item at that
point, not a precondition: a fleet that wastes almost nothing shelves the project whatever
the counterfactual says.

## 2. Small, but each one costs trust

- **`EligibilityConfig.min_remaining_configured`** distinguishes an explicitly
  configured floor from the identical built-in default. The semantics are coherent and
  the exclusion reason now names the floor's provenance, so the divergence is stated
  where it is acted on rather than discovered later. Still unreviewed as a *design*
  choice — surfaced here for the operator, not as an open defect.
- **The `home=` injection seam is only half honoured.** `claude_configs_from_policy`
  expands a config-supplied `config_dir` with `os.path.expanduser`, which reads the
  *process* `HOME`, so an injected `home=` is ignored for exactly the accounts that
  come from configuration. Only `_default_config_dir` respects it. This is why four
  tests passed for three weeks by reading the author's real `~/.claude*` directories,
  and CI caught it the first time it ran on a machine with none. The tests are now
  hermetic; the seam itself is not fixed. Closing it means expanding `~` against the
  injected base everywhere, and it is a behaviour change in a published library, so
  it wants a deliberate decision rather than a drive-by.

- **`fable_fraction = 0.5`** is documented by the vendor and *consistent* with local data
  (a static bound gives <= 0.551) but not pinned by it. Recorded honestly in `pse.py`;
  noted here so it is not mistaken for a measurement.

- **Reported waste double-counts the operator's working time across accounts.**
  `pse.wasted_pse` credits every account with the whole working rate over its own
  horizon, so N accounts claim N times the hours that exist. Two accounts can each read
  "0.000 PSE nothing wasted" while one shared rate cannot absorb both pools, and
  `status` and `explain` under-report waste more as a fleet grows. **This is a reporting
  bug, not a routing bug.** Under one shared rate, earliest-deadline-first already
  minimises total waste, so the ranking the router produces today is the right order.
  A fleet-aware *score* that ranks the account showing unavoidable waste first spends in
  the wrong order: it lets the earlier pool expire too. A first attempt did exactly that
  and broke `test_ranking_across_realistic_accounts_prefers_the_soonest_deadline_with_quota_at_risk`.
  Closes when the *reported* waste figure is computed over the whole field with one shared
  rate, the ranking still orders by earliest deadline, and a test pins both.

- **The capability refresher does not check which Google account answered.** Every
  Antigravity `/usage` read now compares agy's sign-in log with `identity_email`, and a
  mismatch benches the pool. `capability_refresh` still runs `agy models` through the
  same launch recipe with no such check, so after a silent login switch it would record
  the other account's model list under this pool. The stakes are lower than for quota
  numbers, because a model list rarely differs between two accounts of one tier.
  Closes when the refresher passes `--log-file /dev/stderr`, refuses a listing whose
  log names another address, and a test pins it.

- **A Claude plan that lapses without `plan_ends` keeps getting routed to.** Nothing the
  router reads names the plan: the usage endpoint carries percentages and deadlines, and
  the tier comes from a file Claude Code rewrites only when it runs. A lapsed account
  whose usage read still returns low percentages looks like a fresh pool, and a session
  that fails without spending keeps it looking fresh. `plan_ends` covers an announced
  end; this covers the unannounced one, such as a trial that ends. Evidence from
  2026-10-14: `GET /api/oauth/profile` with the existing access token (read-only, no
  `anthropic-beta` header needed) returns `account.has_claude_max`,
  `account.has_claude_pro`, `organization.organization_type`, `rate_limit_tier` and
  `subscription_status`. Two working Max accounts read `active` and `trialing`. A
  cancellation changed none of these fields before the paid period ended, and claude.ai
  refuses to authorize Claude Code for a free account, so no free-plan response can be
  captured in advance. Proposed rule: read the profile at most once per 15 minutes per
  account, failures included; exclude the account, keeping its windows and naming the
  observed values, only when a well-formed profile shows no paid flag or a status other
  than `active` or `trialing`; when the read fails or the shape is unfamiliar, keep
  routing on usage and say so in the note; offline mode applies the cached verdict.
  Closes when that rule ships with tests, and a real lapsed account's profile and usage
  responses are captured as fixtures.

- **The switch test compares pace, not quota at stake.** Stickiness keeps the incumbent
  unless a challenger's tightest-window slack clears `incumbent x 1.15 + 0.02`. On
  2026-10-14 an account with about 5 PSE at risk of expiring (its plan was ending) lost
  that test for about 1.5 hours to an incumbent with about 1 PSE at risk, because its
  five-hour window had just opened and a fresh window's slack is near zero. Pace bounds
  thrashing, so this may be the right trade; it is surfaced here as a design choice,
  not filed as a defect. Related: confidence shrinkage pulls stale accounts toward the
  field mean score, so one large score raises every stale account's score with it.
  A dry run on 2026-10-13 showed how large that can be. Changing only the Gemini pools'
  k from 6.25 to 15 moved `claude` from 0.256 to 0.889 and `codex` from 0.064 to 0.222 in
  a standard pick. Both carried stale data at the time: the Codex reading was 58 minutes
  old, and `claude`'s five-hour window was restored from another source. Shrinkage is the
  likely cause, but nobody has confirmed it.
  Closes when the operator decides whether a large difference in quota at stake should
  be able to override the pace margin, and a test pins whichever rule is chosen.

- **Antigravity read failures leave no durable cause.** `--log-file /dev/stderr` replaces
  agy's own per-run log file, which held the only record of why a `/usage` read failed
  (sign-in stall, eligibility stall, eligibility 503). The cause now reaches only the
  `status` warnings, and the poller's log rotates within about 30 minutes because of the
  window-transition notes. The snapshot note, which history keeps for days, says only
  that the latest read failed. Closes when the note carries the failure's cause, so an
  attempted / succeeded / failed-by-cause count can be read back out of history. #11 is
  why the poller's log is that short.

- **`[providers]` weights do nothing for an account with a weekly window.**
  `account_score` computes the weight (`scoring.py:487`) but returns the PSE waste score
  without it (`scoring.py:520-525`). Only the no-weekly-window fallback multiplies by it
  (`scoring.py:530`). The tests that pin the weight, such as
  `test_provider_weight_scales_the_score` (`tests/test_scoring.py:452`), build a
  `seven_day` window. That is not the `7d` key, so they exercise only the fallback. On
  2026-10-18 one snapshot scored 0.8000 at weight 1.0 and 0.4000 at weight 0.5 with a
  `seven_day` window, and 4.6650 at both weights with a `7d` window. Nothing warns, as
  `tier_override_warnings` does for `[tiers]`. The defaults are not neutral:
  `config.py:1228` ships claude 1.0, codex 0.7, cursor 0.5 and antigravity 0.3, and the
  live `engine_cfg` carries exactly those. They date from `7386ddc` (2026-08-14), and the
  PSE path has ignored them since `de80483` (2026-08-15). So every live pick since then
  has scored Codex at weight 1.0, and every pick since `8151c71` gave the Antigravity
  pools windows (2026-10-12) has scored them at 1.0 too. Applying the weights is
  therefore a large live change, not a cleanup: every Antigravity score would drop to
  30% and every Codex score to 70%. Closes when the operator chooses between applying
  the weights on the PSE path, which changes what a PSE means, and removing them with a
  loud loader warning, and a test with a `7d` window pins the choice. Settle this
  together with #9: a weight and a capacity both scale what Antigravity scores, and they
  must not be tuned to cancel each other.

- **agy's Gemini 4 Argon is classified as neither flash nor pro.** On 2026-10-18 both
  Gemini pools list `Gemini 4 Argon (Low)`, `(Medium)` and `(High)` beside Gemini 3.8
  Flash. Gemini 3.1 Pro left both lists between 21:30Z and 22:30Z on 2026-10-12. The
  family map (`capability.py:82`, `PROVIDER_ANTIGRAVITY: ("flash", "pro")`) matches
  neither name for Argon, so premium has no Antigravity model: `agy lists no gemini pro
  model at high effort`. Closes when the operator decides which capability Argon serves,
  the map and a test say so, and one call's telemetry shows the model that actually
  served it.

- **Two digital-twin evals ran on `claude_c` with no router pick on 2026-10-12.** The
  ledger of the Antigravity measurement (see #9) records unrouted `digital-twin/repo-3`
  evals on that account at 09:31Z. It also records five busy `digital-twin/repo-8` replay
  sessions at 21:27Z, with no router pick of `claude_c`. Both checkouts set
  `OWNER_QUOTA_ROUTER=on` in their `.env`. The 21:27Z case is not proven: the account was
  disabled from 20:50Z to 23:50Z for the measurement, so no pick could choose it, and a
  session started before 20:50Z would look the same. Closes when the consumer's launch
  path is traced to a pick or to a fixed account. A fix in that repo goes in that repo's
  backlog.

---

## 3. The joint objective stalls more than today's when the fleet is tight

`[engine] objective = "joint"` ranks by projected fleet waste avoided and never scores
unmet demand (operator decision 2026-10-14). `quotapick replay` (21 days to 2026-10-14,
Claude group, job 0.01 PSE) is its gate, and the gate fails:

| fleet | policy | metric PSE | waste | unmet | switches |
|---|---|---|---|---|---|
| as it was | own_waste | 34.633 | 26.321 | 0.000 | 852 |
| as it was | joint | 34.633 | 25.141 | 0.000 | 131 |
| one 20x account removed, its demand kept | own_waste | 11.634 | 11.634 | 1.217 | 702 |
| one 20x account removed, its demand kept | joint | 12.155 | 9.966 | 2.520 | 126 |

With the fleet as it was, every policy scores the same metric: nothing went unmet and no
cap bound, so total waste is stock minus demand whatever the routing. With one account
removed, every unmet PSE comes from the five-hour cap during bursts (each pool with
weekly room had spent its five-hour window), under any routing rule; spreading work, as
`own_waste` does, leaves less unmet than draining the earliest deadline first.

**What would close it:** a stall term in the joint objective (lambda x projected unmet,
priced in PSE), or a five-hour-aware tie-break, that passes the replay gate on both rows
at job sizes 0.01 and 0.05 and forecast x0.5 and x2. Until then the default stays
`own_waste`; `run` may still use the joint projection, because credit pools are invisible
to `own_waste` and carry no five-hour cap.

---

## 4. The joint projection cannot move work across providers

The projection and the replay keep provider groups apart: Claude work stays on Claude
accounts. That makes #3's stall figure an upper bound for work that could have run on
another provider, and it leaves two cheap sources of quota undervalued. Measured
2026-10-14:

- Codex: both accounts at about 10% of their week with 128h to go. Since the waste
  series began labelling resets, the two accounts logged 14 early resets against 4
  scheduled. Whatever is left at an early reset is forfeited, so Codex quota is more
  perishable than its published deadline says.
- Antigravity: every pool at 0-8% of its week.

`pick --capability` already ranks all three providers together and skips a Claude
account whose five-hour window is spent, so capability-routed clients can spill today.
Interactive `cl` and work pinned to a Claude model cannot.

**What would close it:** (a) a measured exchange rate between providers' PSE units
(Codex and Antigravity use the default k and capacity 1.0, which nothing calibrates);
(b) a capability stream in the projection that any provider group can serve at that
rate; (c) an early-reset hazard per provider, measured from `waste.jsonl`, that brings
the effective deadline forward. Then re-run the replay gate with cross-provider routing.

*Antigravity part of (a), as of 2026-10-18:* the Claude/GPT pools were measured on
2026-10-12. Their k = 2.0 is now in the operator config, and their capacity of about 0.088
has no config key yet (#9). At capacity 1.0 the router overvalues those pools about 11
times; it does not undervalue them. The Gemini pools have a measured k = 15 that is not
applied and a size nobody has measured (#10).

## 5. A `run` client cannot tell from an attempt whether it was billed

A client may route a failed `quotapick run` call some other way only if nothing was
spent. Today it has to infer that from each attempt's `outcome` and `cost_usd`. Two cases
make the inference fragile. First, `auth` means either a refused login (nothing ran) or a
finished call whose `apiKeySource` was not the expected one (the call ran and was billed);
only a non-zero `cost_usd` tells them apart. Second, a timed-out credit-pool call has
`cost_usd: null`, while the ledger books an estimated spend for it. The first client
(2026-10-15) falls back only when every attempt ended `exhausted`, `no_candidate` or
`auth` with no positive cost, and treats an unknown cost as unknown. That is correct
today because Claude Code reports `total_cost_usd` on every finished call, even a $0.000125
subscription call. It stops being correct if that field is ever missing.

**What would close it:** a per-attempt `spent: true | false | null` in the `run`
response, set from what the router observed: the child produced a result event, or the
ledger booked a spend. Clients would then fall back on `spent: false` alone.

## 6. A credit-pool `run` records a pick on a subscription that never ran

`run` asks `_prepare` for the best subscription first, with `write_state=True`, and only
then compares that choice against the credit pools. When a pool wins, the subscription
pick is already written. A probe on 2026-10-16 (the credit test fixture, pool served)
showed all three effects: a history line with `chosen` set to the subscription, a pileup
reservation on it in `state.json`, and a sticky `selection` entry for it. The reservation
makes the next decisions in the pileup window (60 s by default) see load that never ran.
The sticky entry favors an account that served nothing. The history line adds a pick
with no burn behind it, and `quotapick calibrate` divides recorded picks by observed burn.
Every pool-served `run` repeats this, and five pools are live.

Partly closed: when `--only`/`--exclude` allow no subscription, `run` no longer records
the pick (`test_only_a_pool_never_falls_back_to_a_subscription`).

**What would close it:** split the recording tail out of `_prepare` (`record_pick`, the
selection writeback, and `chosen` on the history line), let `run` call `_prepare` with
`write_state=False`, and record only after the target is known, for a subscription
target alone. The risk is in `_prepare`, which every `pick` runs; the pick goldens must
stay byte-identical.

---

## 9. Every Antigravity pool is sized as if it were a Max-20x account  `[difficulty: high]`  `[filed: 2026-10-18]`

**Background.** Capacity, the size of one five-hour window in PSE, comes only from the
plan tier. `AccountSnapshot.capacity` returns `capacity_for_tier(self.tier)`
(`types.py:843-845`). Antigravity pools have the tier `unknown`, which maps to
`UNKNOWN_TIER_CAPACITY = 1.0` (`types.py:301`). `[tiers]` overrides do not reach scoring
(`tier_override_warnings`, `config.py:1081`), and no per-account key exists. A status run
on 2026-10-13 printed `antigravity_claude {'tier': 'unknown', 'capacity': 1.0}`.

**An idle Antigravity pool's weekly reset behaves two ways, and nobody knows why.**

- *Sliding.* Every poller record from 2026-10-18T01:24:21Z to 01:40:09Z shows all four
  pools at `used 0.000`, with the weekly reset exactly 168.00 hours after the reading.
  The reset moves forward with the clock. The Gemini pools slid like this straight
  after their early resets on 2026-10-16.
- *Fixed.* After `antigravity_claude`'s scheduled reset at 2026-10-13T16:01:20Z, the next
  reset stayed fixed at 2026-10-20T16:01:20Z with `used 0.0` until about
  2026-10-14T09:00Z. Then, still with no use, it jumped to 168 hours ahead
  (`2026-10-21T09:01:36Z` at 09:01:29Z) and slid from then on. `antigravity_claude_b`
  did the same for about 17 hours after its reset on 2026-10-15.

So a sliding pool cannot reach its reset, and it loses nothing while it slides. What
switches a pool between the two behaviors, and whether the first use fixes the boundary,
is unknown. Find out before relying on either. Both scheduled Antigravity rows in
`waste.jsonl` come from pools that the measurement below used part-way and then left.

A measurement on 2026-10-12 sized the Antigravity Claude/GPT pools. The data, harness and
analysis are in `measurements/agy-capacity-2026-10-12/` in the checkout container, beside
`repo-prod`. The main file is `analysis2-final.json`, and `LEDGER.txt` lists every stop
and exclusion. The directory is not in git, so it is the only copy of this evidence.

- **k = 2.0** on both Google accounts: 6 reps on `antigravity_claude` (sd 0.000005) and 2
  on `antigravity_claude_b`. The operator config carries `weekly_to_session = 2.0` on both.
- **A full weekly pool holds $50.07 of Opus 5.5 work at list price.** Antigravity input
  was priced as cache writes at $5/M, and an input-heavy rep confirmed that rate. Every
  call served the model it asked for.
- **A Max-20x five-hour window holds about $283.** This came from `claude_c`, a Max-5x
  account, at $0.708 per point of its five-hour bar over 3 clean reps ($0.761, $0.628 and
  $0.751). One more rep was excluded as contaminated. A clean pilot rep at $0.624 was not
  pooled; with it the mean is $0.691 and the session estimate below becomes about 0.091
  PSE. The meter reads whole percents, so each rep carries about ±18%. The division by
  the 0.25 tier scale is an assumption: `claude_c` reads k = 9.8 where 20x accounts read
  6.25, so a 5x plan may not be exactly a quarter of a 20x plan.
- **So one Antigravity Claude/GPT five-hour window is about 0.088 PSE**, with a range of
  0.082 to 0.100 across reps. Its weekly pool is about 0.18 PSE, roughly 3% of one Max-20x
  week. Capacity 1.0 overstates the pool about 11 times.

**Impact.**

1. *Routing.* A premium dry run (`pick --dry-run --no-sticky --capability premium`, one
   snapshot at 2026-10-13T03:03:47Z) ranked `antigravity_claude` third, at 0.702 PSE at
   risk. With capacity patched to 0.088 in a scratch wrapper, it scored 0.000 and ranked
   last. At the true size, a full pool in a started week shows PSE at risk only in the
   last 5 to 10 hours of that week, depending on where its five-hour window stands. The
   table uses `pse.wasted_pse` at the default rate of 0.04 PSE/h, with a fresh five-hour
   window that resets in 5 hours:

   | Hours to weekly reset | At risk, capacity 1.0 | At risk, capacity 0.088 |
   |---|---|---|
   | 24 | 1.040 | 0.000 |
   | 8 | 1.680 | 0.035 |
   | 1 | 1.960 | 0.136 |

2. *The waste figure.* `waste.jsonl` records each expired window at the router's size
   (`tier_scale=reading.tier_scale`, `waste.py:603`), and nothing skips Antigravity pools.
   The first four rows all carry `tier_scale: 1.0`:

   | Account | Reset | Kind | Remaining | Logged PSE | At 0.088 |
   |---|---|---|---|---|---|
   | `antigravity_claude` | 2026-10-13T16:01:20Z | scheduled | 0.576 | 1.151 | 0.101 |
   | `antigravity_claude_b` | 2026-10-15T02:33:12Z | scheduled | 0.921 | 1.843 | 0.162 |
   | `antigravity_gemini_b` | 2026-10-16T22:06:28Z | early | 0.994 | 6.216 | unknown (#10) |
   | `antigravity_gemini` | 2026-10-16T22:15:24Z | early | 0.996 | 6.225 | unknown (#10) |

   #1 plans to publish this figure.

**The operator has not decided yet.** On 2026-10-12 the operator asked for Antigravity
quota to be used more aggressively, because its app always shows the pools near full. The
measuring agent first recommended keeping the overstatement for that reason. On
2026-10-13 it withdrew that recommendation. The idle-reset finding above came later, on
2026-10-18. It means a full meter in the app is not always losing quota, but the data do
not yet say when it is. The options:

- *Encode 0.088. This is the recommendation.* Ranking and waste rows become honest. The
  Antigravity Claude pools then rank last almost always, so they mostly stay idle. While
  Claude quota is also expiring, a call sent to Antigravity
  saves Claude quota that then expires, so total work does not change. In the 2026-10-13
  dry run `claude_b` carried 2.803 PSE at risk, and #1 counts 54.68 PSE expired unused
  over eight weekly cycles. When the Claude accounts run short, they have nothing at risk
  or do not fit, and the router picks Antigravity at its true size anyway. That case is
  now more likely: `claude_d` (2.715 PSE at risk in that dry run) has been disabled since
  its plan ended, per the operator config on 2026-10-18. #3 shows that a fleet with one
  20x account fewer leaves demand unmet in bursts. A pool kept idle is full at that
  moment.
- *Keep 1.0.* Antigravity is picked early and its meter moves, which is what the
  operator asked for. A pool used part-way and then left can expire with quota in it, as
  both scheduled rows above did. It gets no extra work done while Claude quota expires,
  it can leave the pools spent when the Claude accounts run short, and every Antigravity
  row in the waste figure stays about 11 times too large.
- *Prefer Antigravity regardless.* If the operator wants that, the honest tool is an
  explicit preference, not a false size. None works today: `deprioritize` does the
  opposite, and the `[providers]` weights are ignored on this path (see the bullet in
  #2).

**What would close it:** the operator's choice, then the steps for that choice. *Keep
1.0* closes when the operator config says so in a comment and `quotapick waste` marks
Antigravity rows as placeholder-sized, so #1 can state its figure without them. *Prefer
Antigravity* closes with steps 1 to 6 below, plus a preference setting filed as its own
item. *Encode 0.088* closes with steps 1 to 6:

1. Add an optional per-account `capacity` to `AccountConfig`, next to `weekly_to_session`
   (`config.py:378`). Refuse it unless it is finite and greater than 0, as loudly as
   `plan_for` refuses a bad k (`scoring.py`).
2. Make the configured value the snapshot's capacity wherever a snapshot is built for that
   account. Scoring (`scoring.py:486`, `:520`), the joint projection (`joint.py:162-178`),
   status output and the waste row's `tier_scale` must all read the same number. Keep
   `tier` as reported, and change only capacity.
3. Write the tests first and watch them fail. One test checks that a configured capacity
   changes a real pick, built like `test_a_configured_k_reaches_a_real_pick`
   (`tests/test_k_calibration.py:569`). Another checks that a waste row written for that
   account carries the capacity. Use the window keys `5h` and `7d`. The old `five_hour`
   and `seven_day` keys take the no-weekly-window fallback, so a test built on them tests
   nothing.
4. Ship from repo-prod and verify as `CLAUDE.md` says.
5. When the operator says yes, back up the operator config and set `capacity = 0.088` on
   both Antigravity Claude pools. Also rewrite its Antigravity comments, which still call
   the pools a "last resort for capability picks only" that "serves when no measured
   account fits". They have been stale since `8151c71` made the pools measured.
6. Decide how to treat the rows already in `waste.jsonl`. They carry `k` and `tier_scale`,
   so a report can rescale them. Rewriting a file that is never pruned needs its own
   decision.

*Risks and edge cases:* every capability pick that can land on Antigravity changes rank.
Hysteresis compares unscaled slack (`select.py`), so check that the switch test does not
start to favor the small pools. The replay gate in #3 reads capacity through the joint
projection, so run it again after the change.

*Why high, not medium:* the change crosses config, snapshot, scoring, the joint
projection and the waste writer, and it waits on an operator decision.

---

## 10. The Gemini pools' measured k = 15 waits on a size nobody has measured  `[difficulty: high]`  `[filed: 2026-10-18]`

**Background.** The measurement in #9 read k = 15.0 for both Antigravity Gemini pools.
The reads were consecutive and came from one `/usage` replica: 13 of 16 changes on
`antigravity_gemini` (mean 15.0006) and 12 of 16 on `antigravity_gemini_b` (mean
15.0005). Whole-rep k does not reproduce (6.5 to 31.9), because the Gemini five-hour bar
moves in exact steps of +0.0100. The operator config leaves both Gemini pools on the
default 6.25. Their size in PSE was never measured, so their capacity stays at the
placeholder 1.0 from #9.

The effect of applying k = 15 alone, in a standard dry run on the same snapshot as #9:

| Config | Standard winner | Gemini PSE at risk | `claude_b` |
|---|---|---|---|
| Live, k = 6.25 | `claude_b`, `claude-sonnet-5-5` | 0.428 / 0.383 | 2.803 |
| k = 15 | `antigravity_gemini_b`, `Gemini 3.8 Flash (High)` | 9.130 / 9.099 | 2.803 |

Premium picks did not change, because no Antigravity Gemini model counts as pro (see the
Gemini 4 Argon bullet in #2).

**Impact.** k = 15 is right and 6.25 is wrong, but k multiplies the placeholder capacity.
Applied alone, it sizes each Gemini week at 15 PSE, which is 2.4 Max-20x weeks. Every
standard pick then moves from Claude Sonnet to Gemini Flash on a size nobody measured.
Even a pool whose reset slides 168 hours ahead, so that it never reaches the reset (#9),
would read 8.28 PSE at risk: `pse.wasted_pse` gives 15 minus 0.04 PSE/h over 168 hours,
where k = 6.25 gives 0.000. A Gemini week that expires would log 14.94 PSE instead of
6.23. Nobody has checked Flash's
quality on the standard tier's real work.

**The operator has not decided yet.** On 2026-10-13 the operator questioned holding back
a measured k. The measuring agent agreed that k = 15 is correct and that
6.25 only hides part of the capacity error. Its proposal is the third option below:

- *Apply k = 15 alone now.* The config matches the measurement at once. Standard picks
  move to Flash on a placeholder size, sliding Gemini pools read 8.28 PSE at risk, and
  waste rows grow 2.4 times.
- *Keep 6.25.* Routing stays as it is today, on a k known to be wrong.
- *Apply k = 15 and a measured capacity together, after #9 ships. This is the
  recommendation.* Both numbers are measured when routing changes. It costs a size
  measurement and waits on #9.

**Hazards for whoever measures it:**

- **Two backend replicas answer `/usage`.** A 120 s poll lands on the other replica about
  49% of the time. One replica, called X in the analysis, was trustworthy. The other read
  offsets of up to 0.8% of the weekly pool, and the offsets were not constant. Never
  difference a reading against one from the other replica.
- **An idle pool's weekly reset either slides with the clock or stays fixed** (#9), and
  nobody knows what decides it. Record the reset time on every reading during the run,
  so the analysis can tell which behavior applied.
- **Both Gemini pools reset early on 2026-10-16**, at 22:06Z and 22:15Z, with 99.4% and
  99.6% left (`kind: early`). Do not count on time before a scheduled reset to measure.
- **Gemini 3.1 Pro left both accounts' model lists on 2026-10-12**, between 21:30Z and
  22:30Z. Fourteen eval calls failed with `gemini-3.1-pro-high not recognized`, so the reps
  used Gemini 3.8 Flash.
- **The two Google accounts ran different agy versions** during the eval: 1.3.2 on `_b`
  and 1.3.3 on the primary.

**What would close it:** the operator's choice. *Keep 6.25* closes when the config says
why in a comment. *Apply alone* closes with step 3 using `capacity` left unset.
*Together* closes with steps 1 to 3:

1. Agree with the operator how Flash work compares with Claude work. The proposal is
   list-price dollars, the same basis as #9, where one PSE is about $283 of Opus 5.5 work.
   Show the method before spending quota.
2. After #9 ships, measure the size of both Gemini pools, reading only one replica. Start
   from `harness2.py` and `analyze2.py` in the measurement directory.
3. Set `weekly_to_session = 15.0` and the measured `capacity` on both Gemini pools in one
   config change, with a backup. Record the standard dry run before and after.

*Risks and edge cases:* priced at list dollars, a cheap model may make the pool tiny, so
Gemini rarely wins a pick. That is the honest answer, for the same reason as in #9.

*Why high, not medium:* the method needs a judgment, and the run spends quota against a
noisy meter.

---

## 11. Every status record repeats every reset-scan warning in retained history  `[difficulty: medium]`  `[filed: 2026-10-18]`

**Background.** `_cmd_status` calls `_record_waste` (`cli.py:2381`; body at
`cli.py:2308-2320`). It reads all retained history, rotated files included, and runs
`detect_resets` (`waste.py:730`) over every pair of readings. The pair warnings
(`waste.py:781-797`) have no cursor and no dedup, so each call emits again every warning
the retained history can produce. The default `status` view already prints only a count
of these history notes (`cli.py:2372-2380`), so the harm is in `--json`, `--verbose` and
the poller's log. The last record in the rotated poller log at 2026-10-18T01:21:53Z held
12,203 warnings and was 3.04 MB as compact JSON:

- 7,347 came from `codex_b/base_model_inference`, such as `reset time moved forward 0.00
  of a window without rolling over (2026-09-25T00:02:05Z -> 2026-09-25T00:03:52Z)`. That
  window's boundary slides about two minutes per poll, and the pairs run from 2026-09-25
  to 2026-10-17.
- 4,442 came from the four Antigravity `7d` windows. Only 10 of them are `incoherent pair`
  warnings, from the replica flips in #10. The rest are `reset time moved forward`. At
  that time every pool's weekly reset sat 168 hours ahead and slid with the clock (#9).

**Impact.** Each poller log file holds about 7 records: the rotated file ran from
01:04:32Z to 01:21:53Z, and the live file from 01:24:21Z to 01:37:47Z. The two together
keep 17 to 35 minutes, depending on how full the live file is. The durable-cause bullet
in #2 depends on that log. A new warning is lost among thousands of old ones, and the
count grows with history.

**Fix:** emit a pair warning only for pairs newer than the previous scan, or fold the
warnings into one line per account and window, with a count and the newest example.
Write a test that builds a history with many drift pairs and checks two things: a status
run emits at most one line per window, and a drift pair that arrives after the first run
still appears. Separately, decide whether a window whose boundary always slides should
count as a stock while it slides. Two kinds exist: `codex_b/base_model_inference`, and
an Antigravity pool while its reset slides.

**Touch points:** `cli.py:2308-2320`, `cli.py:2381`, `waste.py:730-800`, `waste.py:1060`
(`update_from_history`), and the waste tests.

---

## Closed

- **Pileup reservations double-counted once the endpoint caught up.** A claim existed
  only because measured usage lagged, but nothing retired it when the usage appeared --
  it decayed on a timer regardless, so the same call was subtracted twice. Each claim
  now records what the bar read when it was written and is netted against whatever has
  become visible since, which needed no completion callback and so no consumer API
  change. Netting happens in fraction space with an explicit `calls_per_window`
  conversion: `cost` counts weighted picks and a bar moves in window fractions, and
  subtracting one from the other is the dimensional error this project already made
  once in the scorer.

- **`calibrate` emitted a paste-ready `calls_per_window` stanza.** The estimate is
  structurally confounded — only router picks in the numerator, all consumption in the
  denominator — and biased low, which makes every pileup reservation larger, the
  direction that already self-throttled the router once. The number is still reported,
  with the per-pick reservation it implies quantified against the default; the
  instruction to adopt it is gone.

- **`calibrate --days` filtered on write time but measured on observation time.** A
  republished stale reading is written now while describing hours ago, so a window
  selected on the record clock was not the window measured on the series clock --
  `--days 0.5` could report a 28-hour span. The same cutoff is now applied again where
  the series is built.

- **The k adoption gate could not fire on poller-only data.** `ADOPTION_MAX_GAP_S`
  equalled the usage poller's own `StartInterval`, so the bar demanded data denser than
  the process producing it. It also judged the wrong statistic: the one-way sampling
  loss it cited is already folded into the interval, and pairs spanning a large gap are
  dropped before they can bias anything, so a single overnight stall cost *data*, not
  accuracy — and less data already shows up as a wider interval. Now gates on the
  series' typical step. Second unreachable gate in this file's history; both read as
  "not enough evidence yet" rather than as bugs, which is what let them survive.

- **An idle account became unreadable, and unreadable meant unroutable** (`fe189f3`).
  Access tokens are renewed only by using an account, and this tool will not redeem a
  refresh token, so the idlest account — which is by definition the one holding the
  most quota — went dark and stayed dark. The poller now spawns the vendor CLI so that
  *it* renews, rate-limited to one attempt per account per 30 minutes. Kept here
  because the shape recurs: a correct safety rule produced a starvation loop, and the
  symptom looked like a ranking bug.
- **An unreadable account was reported as a readable one with no limits** (`62caa4b`).
  A failed read was published as a live snapshot with no windows, so the account
  vanished from `ranked`, `excluded` and `degraded` alike, and the only warning blamed
  a missing model-scoped window. It is now reported with `remaining: null` and the real
  cause. This was accept-and-guess in its exact classic form.

---

## First-run experience for an operator with a mixed fleet

Found by walking a fresh clone as somebody holding several Claude accounts, more than
one Codex account, Antigravity and Cursor. Adding an account beyond the builtin set now
*warns* rather than failing in silence, and Cursor is a real provider. The gaps below
are what that audit left open.

**There is no `setup`, `init` or `doctor` command.** The subcommands are `pick`, `exec`,
`status`, `explain`, `calibrate`, `forensics` and `waste`. Nothing walks a new operator
from clone to first routed call, and there is no example config in the repo. Closing it:
a `quotapick doctor` that names every account it looked for, where it looked, what it
found, and the single next action for each one that failed.

**A machine with no accounts gets an unhelpful first answer.** `status` on a clean home
says `no candidate can serve this call right now`. It never says it looked for Claude
accounts and found none, so the output reads as a broken tool rather than an
unconfigured one. Closing it: report the accounts that were looked for and missed, not
only the ones that answered. The warning added for *configured* accounts does not cover
builtin ones, which are still dropped quietly when their directory is absent. Partly
narrowed: the two Antigravity pools that used to be the whole of that first answer are
no longer builtin, so a clean home now shows an empty fleet rather than a fake one.

**Codex usage from `--ephemeral` runs was invisible.** Closed. Every declared Codex home
is read (`codex_accounts_from_policy`), and each is now read live through `codex
app-server` (`account/rateLimits/read`), with the session transcripts as the fallback.
Before this, a caller running `codex exec --ephemeral` wrote no transcript, and one
account's reading was fifteen hours old at 0% while it had spent 1%. Still open: the
transcript adapter keeps warning "no session transcripts" for a home the live read has
already measured, and the live read spawns one process per account on every pick,
which the poller also pays every 120s.

**Antigravity's two entries are pools, not accounts.** The pools now come from the
operator config through `account_ids_for_provider`, and the adapter runs only when the
config names them, so nothing is invented and the pair is no longer fixed. The design
question underneath is still open and is not this router's to answer: `agy` selects a
pool with `AGY_MODEL` behind one binary and offers no login or config-directory seam at
all, so a second Antigravity *subscription* remains indistinguishable from the first --
to this router and to `agy` itself. Closing it needs that seam to exist upstream.

**Cursor's tier is not read.** `cursor-agent about --format json` reports
`subscriptionTier`, and reading it costs a subprocess plus a network round trip (~0.9s
measured) on the critical path of opening a terminal. The tier is therefore declared by
the operator. Closing it: cache the value on disk with a long TTL and refresh it out of
band, the way the statusline cache already works for Claude.
