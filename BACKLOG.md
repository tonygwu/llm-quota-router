# Backlog

Open gaps, roughly by how much they matter: file order is the priority order, except that
a dependency of an open item is picked before it (see Priority below). Each entry says
what would close it, so "done" is checkable rather than a judgment call.

Kept free of machine-specific paths and account identities: publishing this repo is a
live option, and a backlog is the file most likely to leak them.

The protocol is the `backlog-protocol` skill, which `/run-backlog`, `/improve-code` and
`/improve-data` read. This file holds the parameters specific to this project, and the
items.

## Protocol parameters

| Parameter | Value |
|---|---|
| Item files | this file, under `## Open items` |
| ID scheme | plain integers, `### 14.`; next ID = 1 + the highest in this file or its git history. IDs 1 to 8 were reassigned between items in August 2026, before this protocol, so `git log -p` shows several titles per number; 7 and 8 must not return. Never renumber an item. |
| Priority | file order, except that an item named as a dependency by another open item is picked before that item; a dependency of a blocked item takes the blocked item's place; among equals, file order |
| Difficulty ladder | `low`, `medium`, `high`, `xhigh` (the skill's model map); claim at or below your row |
| Agent handle | `#<n>` from a `repo-<n>` checkout, else `#<directory name>` |
| Validation gate | `./.venv/bin/python -m pytest -q` in the checkout; CI runs the same command. Never `uv tool install` from a working checkout: see `AGENTS.md`. |
| Pre-push review | `adversarial-self-review` on `origin/main..HEAD`, before every push |
| Blocked tag | `[BLOCKED: <what must happen first>]` on the heading means do not claim. Whoever deletes an item also removes its ID from every BLOCKED tag that names it, in the deletion commit. |
| Completion | delete the item; also when all it still lacks is carried by other open items, each saying so. Git history is the record. `## Closed` below is historical and gains no new entries. |
| Umbrella items | #2 and #14 hold several small gaps as bullets: claim the item, finish one bullet, delete that bullet, release the claim. The item goes when its list is empty. |
| Scan logs | `backlog/scan-logs/improve-code/<shard>.md`, tracked (none yet) |
| Ledgers | `backlog/scan-logs/improve-data/<domain>.md`, tracked (none yet) |
| Data read path | none named, so `improve-data` stops |

## Item format

```markdown
### <ID>. <one-line problem statement>  `[difficulty: <level>]`  `[filed: YYYY-MM-DD]`

**Background:** ... (at least one `file:line`)

**Impact:** ...

**Fix:** ...

**Touch points:** `file:line`, ...

---
```

Items 1 to 6 and 14 predate this format: they carry no Background, Impact, Fix or Touch
points structure, and some cite no `file:line`. Items 9 and 10 carry no Fix label and no
Touch points either. Their difficulty and filed tags were added on 2026-10-18, with `filed` set to the
UTC date of the commit that first filed each one. The implementer derives the touch
points from the text. The claim helper matches `### <ID>. ` exactly, so an item heading
at any other level cannot be claimed. A paragraph starting **Operator decision** records
a choice the operator made, with its date; *Operator decision pending* marks a bullet
nobody should start.

---

## Open items

### 1. The core claim is now measurable, and not yet measured  `[difficulty: xhigh]`  `[filed: 2026-08-16]`  `[BLOCKED: waits on #9, #10 and one full Antigravity weekly cycle in waste.jsonl]`

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

**Operator decision 2026-10-18:** proceed. State the figure as soon as #9 and #10 have
landed and the Antigravity series covers one full weekly cycle.

---

### 2. Small, but each one costs trust  `[difficulty: high]`  `[filed: 2026-08-16]`

Workable bullets come first. The last one waits on an operator decision and is marked
so; do not start it. One bullet was closed by the operator on 2026-10-18:
`min_remaining_configured` stays as designed. The `fable_fraction = 0.5` note was
dropped by the author because it asked for no work; `pse.py:59-64` carries it.

- **Nothing enforces a credit pool's `hard_cap_usd`.** The config refuses a pool with
  `card_on_file = true` unless it sets `hard_cap_usd`, and the docstring calls it the
  "Spend ceiling the router enforces when a card is on file" (`config.py:800`). No code
  outside `config.py` reads it: `git grep -n hard_cap -- src` finds only that file. A
  pool with a card on file could spend past its cap through `run`. No money is at risk
  while every configured pool has `card_on_file = false`. Closes when `_pool_targets`
  (`run.py:383-413`) admits a card-on-file pool only while its booked spend plus the
  call's cap stays under `hard_cap_usd`, with a test. #12 depends on this.

- **Reported waste double-counts the operator's working time across accounts.**
  `pse.wasted_pse` credits every account with the whole working rate over its own
  horizon, so N accounts claim N times the hours that exist. Two accounts can each read
  "0.000 PSE nothing wasted" while one shared rate cannot absorb both pools, and
  `status` and `explain` under-report waste more as a fleet grows. **This is a reporting
  bug, not a routing bug, as long as the rate is the measured one.** Under one shared
  rate, earliest-deadline-first already minimises total waste, so the ranking the router
  produces today is the right order. With a rate far below demand it is not: on the
  evening of 2026-10-17 PT the hardcoded 0.04 PSE/h left both `claude` (2.8 PSE, 41h to
  reset) and
  `claude_e` (1.5 PSE, 8h to reset) looking undrainable and ranked the bigger pile
  first, while the measured 0.16 PSE/h drains `claude` in time and loses 0.27 PSE of
  `claude_e`. `pick` now credits each provider with its forecast rate and reports every
  fall-through to the default (`rate` in the payload); the double-counted *figure* is
  still open.
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

- **Antigravity read failures leave no durable cause.** `--log-file /dev/stderr` replaces
  agy's own per-run log file, which held the only record of why a `/usage` read failed
  (sign-in stall, eligibility stall, eligibility 503). The cause now reaches only the
  `status` warnings, and the poller's log rotates within about 30 minutes because of the
  window-transition notes. The snapshot note, which history keeps for days, says only
  that the latest read failed. Closes when the note carries the failure's cause, so an
  attempted / succeeded / failed-by-cause count can be read back out of history. #11 is
  why the poller's log is that short.

- **The `home=` injection seam is only half honoured.** `claude_configs_from_policy`
  expands a config-supplied `config_dir` with `os.path.expanduser`, which reads the
  *process* `HOME`, so an injected `home=` is ignored for exactly the accounts that
  come from configuration. Only `_default_config_dir` respects it. This is why four
  tests passed for three weeks by reading the author's real `~/.claude*` directories,
  and CI caught it the first time it ran on a machine with none. The tests are now
  hermetic; the seam itself is not fixed. Closing it means expanding `~` against the
  injected base everywhere, and it is a behaviour change in a published library, so
  it wants a deliberate decision rather than a drive-by.
  **Operator decision 2026-10-18:** fix it. Expand `~` against the injected base
  everywhere, with a test that injects a `home=` and reads a config-supplied `config_dir`.

- **agy's Gemini 4 Argon is classified as neither flash nor pro.** On 2026-10-18 both
  Gemini pools list `Gemini 4 Argon (Low)`, `(Medium)` and `(High)` beside Gemini 3.8
  Flash. Gemini 3.1 Pro left both lists between 21:30Z and 22:30Z on 2026-10-12. The
  family map (`capability.py:82`, `PROVIDER_ANTIGRAVITY: ("flash", "pro")`) matches
  neither name for Argon, so premium has no Antigravity model: `agy lists no gemini pro
  model at high effort`. **Operator decision 2026-10-18:** Argon serves `frontier`. The operator asked for it
  as "can we", conditional on agy offering the model, and it does: the ids
  `gemini-4-argon-low`, `-medium` and `-high` were listed on all four pools at the
  refreshes of 2026-10-18T06:49:46Z and 06:52:14Z, and `choose_gemini` matches an `argon`
  line as it is. `premium` keeps no Antigravity model; the operator should say if that is
  not wanted. A zero-code path exists today: a `[capabilities.frontier] antigravity =
  "Gemini 4 Argon (High)"` pin, which `validate_table` accepts. Closes when `argon` is an
  Antigravity selector (`SELECTORS`, `capability.py:82`), the default table's `frontier`
  row names it for Antigravity (`capability.py:97`), a test pins both, and one call's
  telemetry shows the model that actually served it.

- **A consumer's evals ran on `claude_c` with no router pick on 2026-10-12.** The ledger
  of the Antigravity measurement (see #9) records unrouted eval runs on that account at
  09:31Z. They came from a checkout whose environment turns its router integration on.
  It also records five busy replay sessions at 21:27Z, from another checkout of the same
  consumer, with no router pick of `claude_c`. The 21:27Z case is not proven: the account
  was disabled from 20:50Z to 23:50Z for the measurement, so no pick could choose it, and
  a session started before 20:50Z would look the same. The consumer and its checkouts are
  named in that measurement's ledger, which is outside git. Closes when the consumer's
  launch path is traced to a pick or to a fixed account. A fix in that repo goes in that
  repo's backlog.

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
  **Operator decision 2026-10-18:** a large difference in quota at stake wins, and the
  operator does not value account stability much, so the margin stays small. Closes when
  a challenger wins the switch test once its PSE at risk exceeds the incumbent's by a
  threshold set just above the noise in that estimate and stated where it is set, the
  shrinkage effect above is confirmed or filed as its own item, and a test pins the rule.
  The one cost of a flip that remains is a cold prompt cache on the new account, which is
  why the margin is small rather than zero.

- *Operator decision pending.* **`[providers]` weights do nothing for an account with a weekly window.**
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
  loud loader warning, and a test with a `7d` window pins the choice. The question is
  separate from #9's capacity, which is decided: a weight and a capacity both scale what Antigravity scores, and they
  must not be tuned to cancel each other.

---

### 3. The joint objective stalls more than today's when the fleet is tight  `[difficulty: xhigh]`  `[filed: 2026-10-14]`

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

### 4. The joint projection cannot move work across providers  `[difficulty: xhigh]`  `[filed: 2026-10-14]`  `[BLOCKED: the Antigravity half of (a) waits on #9 and #10; (c) is #15]`

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

*Split 2026-10-18:* part (c), the early-reset hazard, is #15 and needs nothing from this
item. The Codex half of (a) needs a Codex size measurement that no item carries yet.
**Operator decision 2026-10-18:** proceed with the rest as soon as #9 and #10 land.

---

### 5. A `run` client cannot tell from an attempt whether it was billed  `[difficulty: high]`  `[filed: 2026-10-15]`

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

---

### 6. A credit-pool `run` records a pick on a subscription that never ran  `[difficulty: high]`  `[filed: 2026-10-16]`

`run` asks `_prepare` for the best subscription first, with `write_state=True`, and only
then compares that choice against the credit pools. When a pool wins, the subscription
pick is already written. A probe on 2026-10-16 (the credit test fixture, pool served)
showed all three effects: a history line with `chosen` set to the subscription, a pileup
reservation on it in `state.json`, and a sticky `selection` entry for it. The reservation
makes the next decisions in the pileup window (60 s by default) see load that never ran.
The sticky entry favors an account that served nothing. The history line adds a pick
with no burn behind it, and `quotapick calibrate` divides recorded picks by observed burn.
Every pool-served `run` repeats this, and five pools are live. Since 2026-10-18 a pool
also wins every waste tie with a subscription above zero, which makes pool-served runs,
and so these phantom picks, the common case for `run` while pools hold credit.

Partly closed: when `--only`/`--exclude` allow no subscription, `run` no longer records
the pick (`test_only_a_pool_never_falls_back_to_a_subscription`).

**What would close it:** split the recording tail out of `_prepare` (`record_pick`, the
selection writeback, and `chosen` on the history line), let `run` call `_prepare` with
`write_state=False`, and record only after the target is known, for a subscription
target alone. The risk is in `_prepare`, which every `pick` runs; the pick goldens must
stay byte-identical.

---

### 9. Every Antigravity pool is sized as if it were a Max-20x account  `[difficulty: high]`  `[filed: 2026-10-18]`  `[claimed by: #1]`

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

A measurement on 2026-10-12 sized the Antigravity Claude/GPT pools. Its results, method,
per-rep numbers and assumptions are recorded in
`docs/measurements/2026-10-12-antigravity-capacity.md`. The raw data, harness and analysis
are in `measurements/agy-capacity-2026-10-12/` in the checkout container, beside
`repo-prod`. The main file is `analysis2-final.json`, and `LEDGER.txt` lists every stop
and exclusion. That directory is not in git, because hundreds of its files name the
operator's Google accounts, so it is the only copy of the raw data.

- **k = 2.0** on both Google accounts: 6 reps on `antigravity_claude` (sd 0.000005) and 2
  on `antigravity_claude_b`. The operator config carries `weekly_to_session = 2.0` on both.
- **A full weekly pool holds $50.07 of Opus 5.5 work at list price.** Antigravity input
  was priced as cache writes at $5/M, and an input-heavy rep confirmed that rate. The
  agy tool records a model code, not a name, and an earlier scan mapped that code to Opus 5.5.
- **A Max-20x five-hour window holds about $283.** This came from `claude_c`, a Max-5x
  account, at $0.708 per point of its five-hour bar over 3 clean reps ($0.761, $0.628 and
  $0.751). One more rep was excluded as contaminated. A clean pilot rep at $0.624 was not
  pooled; with it the mean is $0.691 and the session estimate below becomes about 0.091
  PSE. The meter reads whole percents, so each rep carries about ±18%. The division by
  the 0.25 tier scale is an assumption: `claude_c` reads k = 9.8 where 20x accounts read
  6.25, so a 5x plan may not be exactly a quarter of a 20x plan.
- **So one Antigravity Claude/GPT five-hour window is about 0.088 PSE**, with a range of
  0.082 to 0.100 across reps. That is 8.8% of a Max-20x five-hour window. Its weekly pool
  holds only 2 such windows, so it is about 0.177 PSE, or 2.8% of one Max-20x week, which
  holds 6.25. Capacity 1.0 overstates the pool about 11 times.

**Impact.**

1. *Routing.* A premium dry run (`pick --dry-run --no-sticky --capability premium`, one
   snapshot at 2026-10-13T03:03:47Z) ranked `antigravity_claude` third, at 0.702 PSE at
   risk. With capacity patched to 0.088 in a scratch wrapper, it scored 0.000 and ranked
   last. At the true size, a full pool in a started week shows PSE at risk only in the
   last 5 to 10 hours of that week, depending on where its five-hour window stands. The
   table uses `pse.wasted_pse` at the old default rate of 0.04 PSE/h, with a fresh
   five-hour window that resets in 5 hours. Since the evening of 2026-10-17 PT `pick`
   credits a fallback-only provider at the 0.2 PSE/h ceiling instead, so routing sees
   less at risk than this table; the sizes, not the rate, are the point here:

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

**Operator decision 2026-10-18: Encode 0.088.** The operator also mentioned a small bias
toward Antigravity, such as 0.10 in place of 0.088; step 5 sets the measured value
unless the operator asks for the bias in so many words. How the decision was reached:
on 2026-10-12 the operator asked for Antigravity
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

### 10. The Gemini pools' measured k = 15 waits on a size nobody has measured  `[difficulty: high]`  `[filed: 2026-10-18]`  `[BLOCKED: waits on #9; then show the measurement method before spending quota]`

**Background.** The measurement in #9 read k = 15.0 for both Antigravity Gemini pools
(details in `docs/measurements/2026-10-12-antigravity-capacity.md`).
The reads were consecutive and came from one `/usage` replica: 13 of 16 changes on
`antigravity_gemini` (mean 15.0006) and 12 of 16 on `antigravity_gemini_b` (mean
15.0005). Whole-rep k does not reproduce (6.5 to 31.9), because the Gemini five-hour bar
sometimes jumps in one read, out of step with the weekly bar. Of seven flagged jumps,
five read 0.0100 to 0.0113 and two read 0.0009 and 0.0058. The operator
config leaves both Gemini pools on the
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
would read 8.28 PSE at risk at the old default rate: `pse.wasted_pse` gives 15 minus
0.04 PSE/h over 168 hours, where k = 6.25 gives 0.000 (at the 0.2 PSE/h ceiling `pick`
now credits a fallback-only provider with, both read 0.000, so the mis-sizing shows in
`waste`, not in routing). A Gemini week that expires would log 14.94 PSE instead of
6.23. Nobody has checked Flash's
quality on the standard tier's real work.

**Operator decision 2026-10-18: measure the size first, then set k and capacity
together** (the third option below). Step 1's price basis is still to be shown to the
operator before any quota is spent. How the decision was reached: on 2026-10-13 the
operator questioned holding back
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
  offsets that were not constant. The largest gap was 1.4% of the weekly pool, on one
  pair read during a rep, so lag may explain part of it. Never difference a reading
  against one from the other replica.
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

### 11. Every status record repeats every reset-scan warning in retained history  `[difficulty: medium]`  `[filed: 2026-10-18]`

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

### 12. API credit pools cannot pay for agent runs, which is where scheduled headless work is  `[difficulty: xhigh]`  `[filed: 2026-10-18]`  `[BLOCKED: #5 and #6 first; the step 0 probes are #16; the rails question in step 3 is open]`

**Background.** A credit pool is reachable only through `quotapick run`, and `run`
executes one tool-less call. The child always gets `--tools "" --no-session-persistence
--strict-mcp-config --safe-mode` and never `--max-turns` (`run.py:166-196`). A pool child
also gets `--bare --max-budget-usd <min(balance, max_usd_per_call)>`, runs in the
router-owned config dir `credit-homes/<pool>`, and receives the key as
`ANTHROPIC_API_KEY` in its environment (`run.py:586-603`). The init event's `apiKeySource`
must then read `ANTHROPIC_API_KEY` (`run.py:301-303`). The key in the environment is safe
only because the child has no tools.

Four more things keep credit away from agent runs:

- **The joint objective lets credit serve the `run` stream only.** `SERVES` maps
  `STREAM_OTHER` to subscriptions alone (`joint.py:57-60`). The projection walks the two
  streams in a fixed order (`joint.py:295`).
- **The forecast counts only two streams.** It counts `run` from history lines with
  `event == "run"`, and puts everything else in `other`, including every agent run routed
  by `pick` (`forecast.py:19-24`, `forecast.py:334-352`). `replay.py:541-542` and
  `replay.py:630` hardcode the same two streams.
- **`quotapick exec` has no pool path, no timeout and no output parsing.** It picks a
  subscription with pick's own objective (`cli.py:2559`) and runs the caller's command
  to completion (`cli.py:2640`).
- **Holds expire.** A ledger hold stops counting after `HOLD_TTL_S = 3600` s
  (`credits.py:44`, `credits.py:214-223`), whether or not its call has finished.

**Where the demand is.** The operator's config declares four pools that renew monthly:
three Max 20x pools at $200 and one Max 5x pool at $100, $700 a month. The Max 5x figure
was confirmed in its Console on 2026-10-18 (#13). At `usd_per_pse = 305` that is 2.3 PSE,
about 2.5% of the current fleet's monthly capacity. Tool-less work rarely spends this credit: a client's
scheduled tool-less calls are few and cheap, and only a manual batch moves real money.
One did on 2026-10-18.

Scheduled headless work is mostly agentic: multi-turn runs that use Bash, file tools,
web tools and MCP servers. On the operator's machine those runs, priced at API rates from
the `total_cost_usd` that Claude Code reports even on subscription runs, cost a few
dollars each. They would spend the four renewing pools in a few days. Consumers launch
them in the following ways:

- **Stream format.** Some use `--output-format stream-json`, and some use `json`.
- **Context.** Some depend on the cwd's CLAUDE.md and skills, or on user-level MCP servers
  from the account's `.claude.json`.
- **Session file.** One proves that no tools ran by reading Claude Code's own session
  file under the account's config dir.
- **Side effects.** Some run several at once, and some commit and push.

Do not price runs from transcript `usage`. Its `output_tokens` average about 12 per API
message, which is too few for tool calls and looks recorded at stream start.

The consumer inventory, per-job costs and the scripts that measured them are in
`measurements/agent-credit-2026-10-18/` in the checkout container, beside the checkouts.
That folder is not in git, because it describes private projects.

**Impact.** The pools were added on 2026-10-15, so there is no month of data yet. On the
evidence above, most of each month's credit will expire unspent, because the scheduled
work that could spend it cannot reach it.

The loss would show in tight weeks. In two of the four weeks from 2026-09-14 to
2026-10-14, demand ran at 113% and 112% of the current fleet's weekly capacity
(`measurements/fleet-sizing-2026-10-14/` in the container). That is measured consumption,
not proven need. The credit could have served part of the excess instead of queueing it
or falling back from Fable to Opus.

*Root cause:* `run` was designed around three facts that hold only without tools:

- **The key can live in the environment.** With Bash, every tool subprocess may inherit
  `ANTHROPIC_API_KEY`. The key could then reach a transcript, a log or a commit, and some
  consumers' agents commit and push. The 2.1.302 binary contains a
  `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` setting, with `=0` to opt out. What it scrubs, and
  when, is UNVERIFIED; step 0a measures it.
- **The context can be empty.**
  - `claude --help` (2.1.302) says `--bare` skips hooks, LSP, plugin sync, attribution,
    auto-memory, background prefetches, keychain reads and CLAUDE.md auto-discovery.
  - Context must then come from `--system-prompt[-file]`, `--append-system-prompt[-file]`,
    `--add-dir`, `--mcp-config`, `--settings`, `--agents` and `--plugin-dir`. Skills still
    resolve through `/skill-name`.
  - The credit home also has no `.claude.json`, so user-level MCP servers do not exist
    there.
  - It has no `settings.json`, so it has none of the operator's `permissions` allow, deny
    and ask rules, no `defaultMode` and no `env` block.
- **A call can be retried and costs little.** An agent run costs from under a dollar to
  more than ten. It can stop on `--max-budget-usd` partway, with files, rows or commits
  already written, so a failure after the first tool call must never be retried
  elsewhere.

**Fix:** a credit-capable `quotapick exec` for agent runs. The consumer keeps its own
`claude -p` argv, cwd, tools and output parsing. The router owns the account choice, the
model, the key, the budget and the ledger.

Two other designs were rejected:

- **`pick` hands back a credit exec plan and the consumer spawns the run.** Then the router
  never sees the cost. The ledger would need a report-back call, and a consumer that
  crashes never sends one, so the booked balance drifts above the real one.
- **`run` gains agentic fields** (tools, cwd, MCP config, add-dirs, max turns). Its request
  schema would then mirror Claude Code's whole flag surface, and consumers would lose their
  stream.

0. **Prove the Claude Code facts first, then decide.** Run cheap Haiku probes on one pool,
   and record the commands and raw output in the container's `measurements/` folder.
   Probe output sits next to a key, so it does not go in this repo. **Operator decision
   2026-10-18:** the probes are authorized. They are filed as #16 so they can run before
   #5 and #6; their findings replace each UNVERIFIED below.
   - **0a. Auth without the key in the environment.** Run `claude -p --bare --settings
     '{"apiKeyHelper": "<absolute path>"}'` with no `ANTHROPIC_API_KEY` set, and record the
     init event's `apiKeySource`. Then have a Bash tool call run
     `env | grep -c '^ANTHROPIC_API_KEY='`. Repeat with the key in the environment, which
     must print 1, to show the probe can see a leak. Repeat that with
     `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` unset, `=1` and `=0`.
     - Record how often Claude Code calls the helper. The 2.1.302 binary reads
       `CLAUDE_CODE_API_KEY_HELPER_TTL_MS`, and its default is UNVERIFIED.
     - Probe `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`, also in the binary, as a way to hand
       over the key without a helper. Check whether tool subprocesses inherit the
       descriptor.
   - **0b. Context under `--bare`.** Does `--add-dir <cwd>` load that directory's
     CLAUDE.md? Do `--mcp-config` servers load with `--strict-mcp-config`? Do
     `permissions` and `defaultMode` from a `--settings` JSON apply?
   - **0c. The budget stop.**
     - Run a multi-turn task with `--max-budget-usd 0.05`. Record the result record
       (`subtype`, `is_error`, `total_cost_usd`), the exit code, and how far
       `total_cost_usd` overshoots the budget.
     - When `--max-budget-usd` appears twice, which value wins?
   - **0d. JSON output.** Does `--output-format json` carry `apiKeySource` anywhere? If it
     does not, a credit agent run must use stream-json.
   - **0e. Session files.** Where does the session file land under a credit home with
     persistence on, and how fast does that folder grow?
   - **0f. Price.** After at least 10 probe runs, compare the ledger's summed
     `total_cost_usd` with the Console balance, and check whether WebSearch fees are in
     `total_cost_usd`.
   - **0g. Init before spend.** Is the stream-json init event written before the first API
     request starts? Measure it with a probe that kills the child as the init arrives,
     then check the Console for a charge. If that cannot be shown, a kill on init reports
     `spent: null`, not `false`.

   **Gate:** if 0a shows that the key reaches tool subprocesses under every auth method
   and scrub setting, stop and escalate to the operator. Agent runs stay off credit until
   the key can be kept out of them. If 0b shows that `permissions` from `--settings` do not
   apply under `--bare`, drop the permissions guard in step 3 and its consumer
   prerequisite, and say so in the README.
1. **Interface.** `quotapick exec --allow-credit --expected-cost-usd X --timeout S
   (--model ID | --capability C) [--effort E] [--report PATH] [--only/--exclude] --
   claude -p ...`.
   - Without `--allow-credit`, nothing changes, and the existing exec tests pin that.
   - With it, `--expected-cost-usd` and `--timeout` are required, because a missing job
     size or deadline must not default.
   - **Refusals.** Exit 2, before anything is spent, with nothing written to the ledger,
     when:
     - the command is not `claude`, or lacks `-p`/`--print`;
     - the argv passes its own `--model`, `--effort` or `--fallback-model`;
     - the argv uses `text` output, which has no init or result event, or a format
       that step 0d rules out. A missing `--output-format` counts as `text`, because
       that is the default;
     - `--only` names nothing that is a Claude subscription or an `anthropic_api` pool.
   - Like `run` (`run.py:473-478`), exec narrows the candidates to Claude subscriptions
     and `anthropic_api` pools on its own, so a call without `--only` is not refused for
     the Codex and Antigravity accounts in the default set.
2. **Routing.** Rank as `run` does (`run.py:499`, `run.py:533-541`): the joint objective,
   on the new `agent` stream. That includes its tie rule, which sends a job to credit
   when credit and a subscription avoid the same nonzero waste
   (`choose_credit_or_subscription`, `run.py:341`).
   - **The job size.** Since c5436d6, `run` sizes a job as `expected_cost_usd /
     usd_per_pse` when the request names a cost. Without one it falls back to
     `default_job_pse`, 0.003 PSE (`config.py:670`, `run.py:488-493`). Exec uses the same
     formula but requires a positive, finite cost, because an agent run costs far more
     than the default, and `run` reads 0 as no cost.
   - Without a usable forecast, fall back to `own_waste` across subscriptions only. Say so
     in the report's `decision.fallback`, as `run` does.
   - Admit a pool only when its final cap from step 3 is at least
     `expected × admission_margin`, and its expiry is at least `--timeout` away. The cap
     uses `remaining_usd`, which already nets the other holds (`credits.py:224`). A large
     balance is not enough when `max_usd_per_agent_run` or the consumer's budget is below
     that figure. The admission logic belongs in `_pool_targets` (`run.py:383-413`),
     shared with `run`.
   - Resolve the model and effort for the chosen target the way `run`'s `model_for` does
     (`run.py:513-521`), and insert `--model` and `--effort` for every target. When
     `model_for` returns no effort, insert no `--effort`.
   - **Card-on-file pools.** Nothing enforces `hard_cap_usd` today (#2). Until a shared
     admission check enforces it for `run` and exec alike, card-on-file pools sit out of
     exec, with a warning. A refusal would fail exactly the runs where credit wins.
3. **A pool target.**
   - **Environment.** Strip `STRIP_ENV` (`run.py:72-80`) and `QUOTA_ROUTER_*`, and set
     `CLAUDE_CONFIG_DIR` to the pool's credit home.
   - **Flags.** Insert `--bare --settings <router-written JSON> --max-budget-usd <cap>`,
     plus whatever step 0b shows is needed to keep the cwd's CLAUDE.md.
   - **The cap** is `min(remaining_usd, max_usd_per_agent_run, the consumer's own
     --max-budget-usd if lower)`. The consumer's lower value is kept, never overridden.
   - **`max_usd_per_agent_run`** is a new key in each `[credit_pools.<id>]`, with no
     default. A pool without it sits out of agent runs, with a warning.
   - **Settings.** The router-written settings carry `apiKeyHelper`. A consumer's own
     `--settings` JSON is merged in. The merge is refused if that JSON sets `apiKeyHelper`
     or any `env` key named in `STRIP_ENV`, `QUOTA_ROUTER_*`, `CLAUDE_CONFIG_DIR`,
     `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` or `CLAUDE_CODE_API_KEY_*`. The router copies
     nothing from the operator's user settings.
   - **Operator decision before the build:** should the operator's `permissions` and
     `defaultMode` rails be copied into credit runs? *Operator decision pending:* on
     2026-10-18 the operator asked for a plainer explanation before choosing.
     - The recommendation is no: the consumer passes what it needs, so the run is
       explicit and testable.
     - The cost of no is that a job migrated without its own rails would run without the
       deny and ask rules. So a pool sits out of a run, with a warning, when the
       consumer's `--settings` has no `permissions` block. The loss cannot then happen
       silently.
     - The guard forces a deliberate choice; it does not carry the rails. An empty
       `{"permissions": {}}` passes it. Permission flags such as
       `--dangerously-skip-permissions`, `--allowedTools` and `--permission-mode` do not
       count, so a consumer that sets permissions only by flag must add the block.
   - **The key** reaches Claude Code only through the helper (step 4), never in the
     child's environment.
4. **Key helper.** `quotapick credit-key <pool> --token-file F` prints the pool's key only
   when F holds the token that `exec` wrote for this run.
   - **The token is valid until exec exits, not single-use.** Claude Code calls the helper
     again when its TTL runs out (step 0a), and a single-use token would fail a long run
     partway.
   - **What the token protects.** It narrows exposure to the run's lifetime: after exit
     the file is gone, and the helper prints nothing. It does not stop a same-user
     process from reading the command line from `ps` and running the helper while the run
     is live. The file's mode 0600 blocks other users only.
   - The settings JSON names the helper by absolute path, because launchd's PATH lacks
     `~/.local/bin`.
   - The helper exits non-zero, with no output, on a missing item or a wrong token.
5. **Running, pass-through and checks.** These apply to subscription targets too when
   `--allow-credit` is set. A subscription target's environment is unchanged, and its argv
   gains only the router's `--model` and `--effort` from step 2.
   - Copy stdout and stderr through byte for byte, and parse a copy.
   - **Process layout.** Exec first starts a watchdog as the leader of a new process
     group, with stdin, stdout and stderr on `/dev/null`, so it cannot hold the consumer's
     output open. It then starts the child in that group with `process_group`. It does
     not use `start_new_session`, which would put the child in a session the watchdog
     cannot share.
   - At `--timeout`, send the group SIGTERM, then SIGKILL after 10 s.
   - **A dead wrapper must not leave the child running.** On SIGTERM, SIGINT or SIGHUP,
     exec forwards the signal to the group, then exits once the child has exited. Exec
     waits for the child, not the group.
   - SIGKILL cannot be caught. The watchdog therefore holds the read end of a pipe whose
     write end only exec keeps open. When the pipe reaches EOF, it kills the group. It
     exits on SIGTERM.
   - **Exit codes.** Exec exits 124 on its own timeout, 128 plus the signal number after
     a forwarded signal, and the child's code otherwise. The report's `outcome` names
     which.
   - Consumers stop a run with SIGTERM and wait for exec to exit before they retry. The
     README states this contract.
   - From the init event, check `apiKeySource` against the target's expected value. On a
     mismatch, kill the child's group, and report `auth` with `spent` set from 0g.
   - From the result event, read `total_cost_usd`, `modelUsage` and `session_id`.
   - If the key appears in the copy, report it loudly and have `quotapick doctor` flag the
     pool for rotation. Do not rewrite the consumer's output.
6. **Ledger, report and history.**
   - Book a hold for the cap at start. Give it an expiry of `--timeout` plus 5 minutes,
     not `HOLD_TTL_S`. Book a spend of `total_cost_usd` at the end.
   - When there is no result record, book the hold as an estimated spend with `spent: null`.
     That covers a killed child, a timeout, and a wrapper that was itself killed: an exec
     hold that expires unsettled becomes an estimated spend, never a silent release.
   - `quotapick doctor` asks for a Console checkpoint (`quotapick credits set`) once a
     pool's estimated spends since its last checkpoint pass $20 or 10% of `grant_usd`,
     whichever is lower.
   - `--report PATH` writes `{target_kind, target, pool, model, config_dir, session_id,
     cost_usd, spent, apiKeySource, outcome, decision}`. The exit code passes through.
   - Never retry on another target once the child has started.
   - Once the child has exited, write one history line with `event == "exec"` and an
     `exec` block: `{stream: "agent", target, pool_kind, cost_usd, cost_pse, fable,
     group, outcome}`. Never record a pick on a subscription the run did not use (#6).
7. **Joint projection, forecast and replay.**
   - Add `STREAM_AGENT`, served by subscription and credit, to `SERVES`, and to the
     stream walk at `joint.py:295`. Walk the subscription-only `other` first, then
     `agent`, then `run`. That order is a judgment call; test that it does not starve
     `run`.
   - Measure `agent` from the `exec` lines in `forecast.py`, the same way as `run`. Add it
     to `GroupForecast` and `to_dict` (`forecast.py:69-98`), and bump `FORECAST_VERSION`
     (`forecast.py:45`) so a cached forecast without it is not read. Subtract
     subscription-served agent work from `other`, as subscription-served `run` work is
     (`forecast.py:19-24`), so a consumer that moves from `pick` to `exec` is counted once.
   - Teach `replay.py:541-542` and `replay.py:630` the new stream.
8. **Tests.** Use a real fake-`claude` script that streams stream-json lines over time and
   can block or ignore SIGTERM. The `run` tests' `FakeRunner` (`tests/test_run.py:48`)
   returns whole output, so it cannot test pass-through or kills.
   - The pool target's argv, environment and settings: no `ANTHROPIC_API_KEY` in the
     environment, and the helper named with a token file.
   - Each refusal, with the ledger untouched.
   - Admission: a pool sits out when its final cap is below `expected × margin`, even
     with a large balance. For example, with `max_usd_per_agent_run` at $5, an expected
     cost of $4 and a $150 balance, the pool sits out.
   - An `apiKeySource` mismatch kills the child.
   - The budget stop books a spend.
   - A timeout and a killed wrapper each book the hold as an estimated spend.
   - SIGKILL of the wrapper ends the child's group through the watchdog. The watchdog
     holds no output open, and the exit codes are 124 and 128 + signal.
   - The settings merge refuses each forbidden key. A pool sits out when the consumer's
     `--settings` has no `permissions` block, and so does a card-on-file pool.
   - The inserted `--model` and `--effort` match `model_for`, and a consumer `--effort` is
     refused.
   - Stdout passes through byte-identical.
   - Without `--allow-credit`, the `--dry-run --json` output is byte-identical to today's.
     With it, a pool target's dry run shows the plan, never the key.
   - The joint objective serves `agent` from credit, still keeps `other` on subscriptions,
     and a fixture forecast counts a moved consumer once.
9. **README.** Add an agent-runs section next to "API credit pools". It covers the
   interface, what `--bare` and the empty credit home remove, the consumer prerequisites
   below, and the key-handling limits under the risks.

**Consumer prerequisites.** Each consumer decides these; they are not this item's code.

- **User-level MCP servers.** A consumer that uses them must pass `--mcp-config`, plus the
  context flags from step 0b.
- **Permission rails.** A consumer that needs them passes them in its own `--settings`.
- **Proof from the session file.** A consumer that proves something from Claude Code's
  session file needs `config_dir` and `session_id` from the report.
- **Contracts.** A consumer whose results are tied to a harness contract must treat a
  credit agent run as a new harness.
- **JSON output.** A consumer on `--output-format json` depends on step 0d.

A good pilot is one scheduled job whose runs cost a few dollars at most, so the cap
cannot bite.

*Risks and edge cases:*

- **Key handling.** A tool-using agent runs as the same macOS user, so it can read the
  Keychain item itself. The aim is no accidental leak into the environment, logs,
  transcripts or commits, not secrecy against a determined agent. Say so in the README.
- **Fidelity.** `--bare` and the empty credit home drop hooks, plugins, auto-memory, user
  MCP servers and the operator's settings. A job that silently depended on them behaves
  differently, so a consumer opts in per job.
- **Budget stops.** The cap must sit above the job's p99 cost. The costliest measured
  agent runs exceed today's `max_usd_per_call` default of $10 (`config.py:816`), so agent
  runs need their own cap key. Step 0c's overshoot figure sets the margin.
- **Concurrency.** Several runs hold against one pool at once, so admission must count
  the other holds.
- **A renewal that did not happen.** A monthly pool past its expiry is assumed renewed at
  confidence 0.6. If it was not, the first model call fails with "credit balance is too
  low" before any tool runs. A refused request bills nothing, so `spent` reads `false`.
- **Card-on-file pools** sit out of exec until `hard_cap_usd` is enforced (step 2).
  Today nothing enforces it for `run` either; #2 carries that gap.
- **Credit-home growth.** Session files with persistence on accumulate. Give them a
  retention rule (step 0e).
- **The usage dashboard.** API-key runs leave no subscription windows behind, so
  `quotapick status` must show credit agent spend from the ledger. Otherwise the fleet
  looks idler than it is.

*Why xhigh, not high:*

- It wires exec, run, the ledger, the joint projection, the forecast and replay together
  in a new way.
- It handles a secret, under a Claude Code behavior (0a) that is unverified and gates the
  whole design.
- It adds a pass-through wrapper with kill semantics that must not alter output.
- A mistake bills real money or leaks a key.

**Depends on:**

- **#5.** This item adds `spent` to the exec report. Implement it once, in code that `run`
  and `exec` share.
- **#6.** Exec also prepares with `write_state=True`, so fix #6 first, or exec repeats it.
- **#2's `hard_cap_usd` bullet.** It is needed before card-on-file pools can serve
  agent runs.

**Touch points:**

- `cli.py:2550-2649` (`_cmd_exec`), `cli.py:2559`, `cli.py:2640`, `cli.py:3095` (its
  parser), `cli.py:1066-1076` (stream wiring), and the `status` rendering for credit agent
  spend.
- `run.py:72-80` (`STRIP_ENV`), `run.py:166-196`, `run.py:273-310` (`judge`, the outcome
  classification to share), `run.py:383-413` (`_pool_targets`, admission),
  `run.py:473-478` (candidate narrowing), `run.py:488-493` (job size), `run.py:341`
  (`choose_credit_or_subscription`), `run.py:499` and
  `run.py:513-521` (`model_for`), `run.py:533-541` (joint routing), `run.py:586-603`
  (credit target) and `run.py:617-698` (ledger hold, spend and release).
- `credits.py:44` and `credits.py:214-223` (hold expiry).
- `joint.py:53-60` (streams and `SERVES`), `joint.py:213` (`credit_stock`) and
  `joint.py:295` (the stream walk).
- `forecast.py:19-24`, `forecast.py:45` (`FORECAST_VERSION`), `forecast.py:69-98`
  (`GroupForecast`), `forecast.py:132-157` and `forecast.py:334-378`.
- `replay.py:541-542` and `replay.py:630`.
- `config.py:781-816` and `config.py:911-916` (`max_usd_per_agent_run` and its parser).
- `doctor.py` (rotation flag, checkpoint request), a new `credit-key` command, new
  `tests/test_exec_credit.py` with a fake-`claude` script, and the README sections
  "Running a call — quotapick run" and "API credit pools".

---

### 13. A credit pool holds one grant at a time, so overlapping grants are mispriced and the grant day is never learned  `[difficulty: high]`  `[filed: 2026-10-18]`

**Background.** The ledger keeps one balance and one expiry per pool. `balance()` starts
from the latest `grant` or `checkpoint` line, and each such line *replaces* the balance
rather than adding to it (`credits.py:172-183`). Past that expiry, a `monthly` pool is
assumed to get a fresh `grant_usd` for one more calendar month, at confidence 0.6
(`credits.py:186-198`). `add_month` anchors that assumed grant to the old expiry instant,
not to the day grants actually arrive (`credits.py:112-122`). The module docstring states
the model (`credits.py:1-24`), and every line carries `LEDGER_VERSION = 1`
(`credits.py:41`).

The rest of the router inherits the single-grant view:

- **Projection.** `credit_stock` turns a pool into one stock with one deadline and
  `id = pool_id` (`joint.py:213-237`), built once per pool in `run` (`run.py:547`).
  - The chooser (`choose_credit_or_subscription`) scores candidates by pool id
    (`run.py:341-380`).
  - The winner is matched back by `pool.id` and reported with `bal.expires_s`
    (`run.py:564-579`).
- **Waste series.** `expiries_due` writes one `expired` line per period end, de-duplicated
  by the expiry instant alone (`credits.py:251-273`).
- **`quotapick credits grant --amount --expires`** exists (`cli.py:3314-3317`, handled at
  `cli.py:4183-4210`), but its line replaces the balance. Recording a new grant erases
  what the older one had left.
- **`doctor`.** It warns on a checkpoint older than a week (`doctor.py:460-462`), on a
  balance that rests on the config or an assumed renewal, and on expiry within a day
  (`check_credit_pools`, `doctor.py:496-534`). It has no notion of when a grant is due,
  and its remedy text asks for `credits set ... --expires` (`doctor.py:509-514`).
- **Config.** A pool's keys are a closed set (`config.py:770-784`). An unknown key is
  warned about and ignored (`config.py:860-864`).

**Observed 2026-10-18.** One Max 5x pool's Console listed two grants at once (UTC dates,
as the Console shows them):

| Granted | Amount | Expires | Left |
|---|---|---|---|
| 2026-10-14 | $100 | 2026-10-20 | $98.81 |
| 2026-10-17 | $100 | 2026-11-20 | $100.00 |

A new grant arrived three days before the previous one expired, and both were live. The
inferred cadence, seen once, is a grant on the 17th that expires on the 20th of the next
month. The config had declared that pool as a $200 grant expiring 2026-10-20, so the
router treated all of it as expiring within two days.

**Impact.** Between a grant's arrival and the previous grant's expiry, the router is
wrong one way or the other:

- **If the operator records only the older grant,** the router cannot see the new one.
- **If the operator records the total under the older expiry,** the router treats money
  that lasts another month as expiring now. It then sends the pool work that a
  subscription could have served, and that subscription's quota can expire unused
  instead.

After the older grant expires, the assumed renewal arrives three days late and is never
checked against what the Console showed. The waste series books one period end per
pool, so it cannot say which grant expired with what left. None of this is learned:
every month the operator has to read the Console and correct the pool by hand.

**Fix:** all dates below are UTC, as the Console shows them.

1. **Grants as a list, and a ledger version 2.**
   - A pool holds every live grant: `{usd, granted_s, expires_s}`, identified by its
     expiry date (a UTC calendar date, not an exact float).
   - A v2 `grant` line adds a grant. `quotapick credits grant` requires `--granted`, with
     no default: the day the operator records a grant is often not the day it arrived,
     and a defaulted date would poison step 5. `--granted` must be no later than now and
     before `--expires`, or the command fails loud.
   - A grant whose expiry date matches a live grant's is merged into it: the amounts
     add, and the earlier granted date is kept. So an expiry date always names exactly
     one grant.
   - A v2 `checkpoint` with `--expires` sets the remaining amount of the grant expiring
     on that date. It is refused when no live grant expires that day.
   - A v2 `checkpoint` without `--expires` sets a single total. It is allowed only while
     one grant is live, and refused with the live grants listed when there are more.
   - **v1 lines read exactly as today.** Every live ledger line is v1: six checkpoints,
     all with `expires_s`, and no `grant` lines. A v1 `grant` or `checkpoint` resets the
     pool to one grant `{usd: remaining, expires_s}`.
   - Bump `LEDGER_VERSION` (`credits.py:41`).
2. **Spend order, applied when the ledger is read.**
   - Spend, hold and release lines stay at pool level. `balance()` allocates them to
     grants, earliest expiry first, every time it reads the ledger. So a corrected
     assumption about the order re-reads the history correctly instead of leaving it
     booked wrong.
   - An `exhausted` line empties every live grant until the next grant or checkpoint,
     as today.
   - The order is an assumption about Anthropic. Verify it against the Console's
     per-grant "Remaining balance" after a known spend, and record the result in the
     README.
3. **No double counting.**
   - An assumed or predicted grant is added only for a period that no recorded grant
     already covers: none whose expiry falls within seven days of the assumed grant's
     expiry.
   - Renewals chain only from the latest-expiring grant, recorded or assumed, so each
     period gets at most one assumed or predicted grant.
   - In the observed case, the balance at 2026-10-21 is the 11-20 grant's $100 minus
     spend, not $200. At 2026-11-25, with nothing new recorded, it is one assumed $100
     for 11-20 to 12-20, not two.
4. **Balance, display and waste.**
   - The balance is the sum of the live grants.
   - `Balance` gains a per-grant breakdown (`credits.py:125-146`). `Balance.expires_s`
     becomes the earliest live grant's expiry.
   - `Balance` also gains that grant's own remainder, `first_expiring_usd`. Every place
     that pairs an expiry with an amount uses it, never the pool total:
     - doctor's "expires within a day" warning (`doctor.py:523-530`);
     - the `run` decision reason (`run.py:577-578`);
     - the credits table (`cli.py:4122-4127`).
     Otherwise, on 2026-10-19 in the observed case, doctor would report "$198.81
     expires within a day" when only $98.81 does.
   - `quotapick credits` and `status` list each grant's amount left and expiry.
   - `expiries_due` writes one `expired` line per grant, keyed by pool and grant (its
     granted and expiry dates), not by expiry instant alone.
   - Once a period end is written it is frozen, so a schedule learned later never
     re-writes or adds `expired` lines for past periods.
   - A v1 `expired` line, keyed by instant only, matches any grant that expires on its
     UTC date. The poller writes such lines before this item ships, and the next
     `status` must not book that waste twice.
5. **A learned grant schedule.** Learn it only from recorded operator grants that carry
   `--granted`.
   - Once the last two such grants agree on the granted day of the month, the expiry
     day, the months to expiry and the amount, predict the next grant on that schedule,
     at confidence 0.6, labelled `predicted`.
   - With fewer than two agreeing grants, keep today's renewal anchored to the expiry,
     subject to step 3.
   - A change of amount, for example after a plan changes tier, breaks the agreement
     until two new grants agree.
   - The explicit `[credit_pools.<id>] grant_day` and `expires_day` keys override the
     learned schedule, so one observation can be acted on.
     - Both are required together: one without the other fails loud.
     - Each is 1-28 or `last`.
     - Add both to `_CREDIT_POOL_KEYS` (`config.py:770-784`). Otherwise they are warned
       about and ignored.
   - `grant_usd` keeps its meaning: the amount of one monthly grant, used for assumed
     and predicted grants.
   - No grant is predicted at or after the linked plan's `plan_ends`, as today.
6. **Confirmation.**
   - From the predicted grant day on, `doctor` asks for `quotapick credits grant` with
     the Console figures, until a recorded grant confirms the prediction.
   - An `exhausted` refusal **on or after** the predicted day, while the predicted grant
     is counted, proves the prediction wrong: drop it and ask for the Console figures.
   - A refusal before the predicted day is the normal end of a grant and says nothing
     about the prediction.
   - Update `check_credit_pools` and its remedy text (`doctor.py:496-534`) to the
     per-grant commands.
7. **Projection.**
   - The chooser keeps one candidate per pool. It scores the job placed across that
     pool's grants, earliest first: the part the earliest grant can cover goes there,
     and the rest goes to the next grant. Scoring on the earliest grant alone would cap
     a $4 job at a grant's last $0.50, and the pool would lose to a subscription while
     $100 sat in its next grant.
   - The pool's other live grants enter the projection as their own stocks, each with
     its own deadline and a distinct id `<pool>#<expiry date>`, so fleet waste counts
     them. They are never candidates.
   - `credit_stock`'s caller (`run.py:547`), the chooser (`run.py:341-380`), the winner
     lookup (`run.py:564-579`), the per-call budget (`run.py:599`) and the admission sum
     (`run.py:383-413`) change with it.
8. **Tests.**
   - The 2026-10-18 case: two overlapping $100 grants give $200 between 10-17 and 10-20.
     Spends drain the 10-20 grant first.
   - At 10-20, one `expired` line records that grant's remainder. At 10-21 the balance is
     the 11-20 grant's $100 minus spend, with no assumed $100 on top (step 3).
   - Two recorded grants granted on the 17th predict the next one on the 17th, expiring
     the 20th of the next month.
   - One grant falls back to the expiry anchor, and the config keys override both.
   - A refusal on the predicted day drops the prediction; one before it does not.
   - At 2026-11-25 with nothing new recorded, the pool reads one assumed $100, not two.
   - On 2026-10-19, doctor's expiry warning names $98.81, not $198.81.
   - A job larger than the earliest grant's remainder is scored across two grants.
   - A v1 `expired` line suppresses a second line for a grant expiring that day.
   - A v1 ledger reads unchanged, and a total checkpoint is refused while two grants are
     live.
   - The chooser scores a pool on its earliest grant, and later grants appear only as
     non-candidate stocks.
   - A learned schedule writes no new `expired` lines for past periods.

*Risks and edge cases:*

- **Spend order (step 2) is unverified.** If Anthropic drains the newest grant first, or
  spreads spending across grants, earliest-first allocation misstates which grant
  expires with money left. Because allocation happens at read time, fixing the rule
  corrects the history.
- **Short months.** The 29th to 31st: `add_month` already clamps (`credits.py:112-122`),
  and the schedule keys accept 1-28 or `last` only.
- **A Console figure that lags.** The Console's spend figure trails real spend by
  minutes, so a checkpoint should come from a quiet moment or from the ledger. The README
  should say so.
- **A one-off first grant.** One granted when the Console org was linked may not follow
  the cadence, which is why learning needs two agreeing grants.
- **The hard cap.** Once #2's `hard_cap_usd` check exists, it applies to the pool's
  total booked spend across all grants. Test that per-grant balances leave it unchanged.

*Why high, not medium:* it changes the ledger's meaning, with a versioned migration that
existing ledgers must survive. The change spans `credits.py`, `cli.py`, `run.py`,
`joint.py`, `doctor.py`, `config.py` and the README. The learning rule, the
no-double-count rule and the unverified spend order are all judgment calls.

**Relates to:** #12. Its pool figures assumed $200 per grant until this observation
showed that the Max 5x pool grants $100, and this commit corrects them. #12 cites hold
expiry inside `balance()` (`credits.py:214-223`) in its Background and Touch points.
This item rewrites `balance()`, so whichever item lands second must re-read the other.

**Touch points:** `credits.py:1-24`, `credits.py:41`, `credits.py:112-122`,
`credits.py:125-146` (`Balance`), `credits.py:155-248` (`balance`) and
`credits.py:251-273` (`expiries_due`); `cli.py:3304-3317` (the `credits` parser),
`cli.py:4164-4227` (`_cmd_credits`) and `cli.py:4077-4163` (`_credit_pool_rows`,
`_credit_pool_table`, including `cli.py:4122-4127`); `run.py:341-380`, `run.py:383-413`,
`run.py:547`, `run.py:564-579` and `run.py:599`; `joint.py:213-237`;
`doctor.py:460-462` and `doctor.py:496-534`; `config.py:770-784` (`_CREDIT_POOL_KEYS`),
`config.py:788-833` (`CreditPoolConfig`) and `config.py:839-924` (`_build_credit_pools`);
`tests/test_credits.py` and `tests/test_run_credit.py`; and the README section "API
credit pools".

---

### 14. A mixed-fleet operator has no setup path, and three other first-run gaps stay open  `[difficulty: high]`  `[filed: 2026-09-07]`

Found by walking a fresh clone as somebody holding several Claude accounts, more than
one Codex account, Antigravity and Cursor. Adding an account beyond the builtin set now
*warns* rather than failing in silence, and Cursor is a real provider. The gaps below
are what that audit left open. This is an umbrella item: finish one bullet, delete it.

- **Nothing walks a new operator from clone to first routed call.** There is no `setup`
  or `init` command, and no example config file: the README carries config examples
  inline (`README.md:123-137`), but no one complete file. `quotapick doctor` exists, but it
  checks that routing is in force on a configured machine (`doctor.py:1-30`); it does not
  name every account it looked for, where it looked, what it found, and the single next
  action for each one that failed. Closing it: that first-run check, in `doctor` or a
  `setup` command.

- **An empty home gets a warning that misdiagnoses itself.** `status` on a clean home
  names every builtin account it could not read (`cli.py:523-528`) and ends with `no
  candidates at all` (`cli.py:930`). The warning does not print the directory it looked
  in, and its hint says the adapter "does not read declared accounts", which blames the
  code when the directory is missing. Closing it: name the directory looked in, and drop
  the hint when a missing directory is the cause.

- **The Codex transcript adapter warns about a home the live read has already measured.**
  Every declared Codex home is read live through `codex app-server`
  (`account/rateLimits/read`), with the session transcripts as the fallback. The transcript
  adapter still warns "no session transcripts" for such a home, and the live read spawns one
  process per account on every pick, which the poller also pays every 120s.

- **Cursor's tier is not read.** `cursor-agent about --format json` reports
  `subscriptionTier`, and reading it costs a subprocess plus a network round trip (~0.9s
  measured) on the critical path of opening a terminal. The tier is therefore declared by
  the operator. Closing it: cache the value on disk with a long TTL and refresh it out of
  band, the way the statusline cache already works for Claude.

---

### 15. The joint projection ignores each provider's early-reset hazard  `[difficulty: xhigh]`  `[filed: 2026-10-18]`

**Background:** carried from #4 part (c). The joint projection gives every stock the
deadline the provider publishes (`joint.py:184-201`; credit pools at `joint.py:228`),
and the replay does the same. The
waste series labels each reset `scheduled` or `early` (`waste.py:132-135`), and
`_early_events` (`waste.py:1131`) already groups the early ones. As of 2026-10-14 the two
Codex accounts had logged 14 early resets against 4 scheduled since labelling began. The
README section "An early reset is not waste" explains why an early reset is forfeited,
not wasted: routing cannot plan for a boundary nobody published.

**Impact:** whatever is left at an early reset is forfeited, so Codex quota is more
perishable than its published deadline says, and the projection under-values spending
it early. #4's stall figure inherits the same deadlines.

**Fix:**

1. Measure an early-reset hazard per provider from `waste.jsonl`: the share of weekly
   periods that ended early and the typical lead before the published boundary, with the
   counts they rest on. A provider with fewer than a stated minimum of periods gets no
   hazard, and the report says so.
2. Bring each stock's effective deadline forward by that hazard in the projection
   (`joint.py:184-201`).
3. Report the hazard and its counts in `status --json`.
4. Re-run the replay gate in #3 before and after, and record both results in the commit
   that ships this.

*Risks and edge cases:* fourteen events is thin evidence, and one provider's behaviour
can change; the hazard must carry its counts wherever it is shown. An early reset is a
provider's one-off and stays out of the waste total, so this item changes deadlines, not
the waste figure.

*Why xhigh, not high:* it wires the waste series into the projection and the replay for
the first time, and the replay gate decides whether it ships.

**Touch points:** `waste.py:1131-1210` (`_early_events`), `joint.py:184-201` and `:228`, `replay.py`
where stocks are built, the `status --json` payload, and the waste and joint tests.

---

### 16. The step-0 probes for #12 have not been run  `[difficulty: high]`  `[filed: 2026-10-18]`

**Background:** #12 step 0 lists probes 0a to 0g: whether an API key reaches tool
subprocesses under each auth method and scrub setting, what `--bare` keeps, the budget
stop, JSON output, session files, price and init-before-spend. No output exists:
`measurements/agent-credit-2026-10-18/` in the checkout container holds cost and size
surveys only. The probes the operator remembers are different ones: `run`'s own
`apiKeySource` check on tool-less calls, and `measurements/free-plan-probe-2026-10-14/`,
which read the profile endpoint for the lapsed-plan bullet in #2. **Operator decision
2026-10-18:** the probes are authorized.

**Impact:** probe 0a gates the whole #12 design, and the design cannot start until it is
answered. The other probes decide flags, output format and margins.

**Fix:**

1. Run 0a to 0g exactly as #12 states, with cheap Haiku calls on one credit pool.
2. Record every command and its raw output in the container's `measurements/` folder,
   outside git, because probe output sits next to a key.
3. Write the findings into #12, replacing each UNVERIFIED with what was measured and the
   date.
4. Apply #12's gate: if 0a shows the key reaching tool subprocesses under every method
   and setting, stop and escalate to the operator instead of proceeding to #12.

*Risks and edge cases:* real credit is spent, a few cents per probe; never commit probe
output or a key; read one full raw response before trusting any parser.

*Why high, not medium:* the probes need judgment on raw Claude Code output, and a secret
is close to everything they write.

**Touch points:** `BACKLOG.md` #12 step 0 and its UNVERIFIED notes; the measurements
folder in the checkout container. No code.

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
