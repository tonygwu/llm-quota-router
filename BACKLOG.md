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

**What is still open, in the order it blocks publishing:**

1. **No data yet.** The first row appears at the first reset after the poller next runs.
   One full weekly cycle across the fleet is the minimum the publishing bar asks for.
   Nothing can shorten this but waiting.
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

**Closes when:** at least one full weekly cycle of resets is recorded for every account,
and the absolute wasted-PSE figure is stated. If the number is unimpressive, that is the
finding — publish it or shelve the project, but do not keep shipping scoring changes
justified by argument alone. The counterfactual comparison becomes its own item at that
point, not a precondition: a fleet that wastes almost nothing shelves the project whatever
the counterfactual says.

## 2. Pileup reservations expire on a timer, not on completion

Each pick books a reservation that decays after 60s. Nothing releases it when the call
finishes, and nothing reconciles it once the usage endpoint reflects the real burn — so
for a window the same consumption is counted twice, once as a reservation and once as
measured usage.

Bounded by the 25% cap, so it degrades routing quality rather than breaking it. Fixing
it properly needs callers to report completion, which is an API change every consumer
must adopt — worth designing before building.

**Closes when:** a reservation is released by the event that made it obsolete, and a
test covers the overlap window where both signals are present.

## 3. Small, but each one costs trust

- **`EligibilityConfig.min_remaining_configured`** distinguishes an explicitly
  configured floor from the identical built-in default, so a config file that spells out
  the default value behaves differently from omitting it. Defensible, but it was
  introduced without review and is the kind of semantics that surprises someone later.
- **`fable_fraction = 0.5`** is documented by the vendor and *consistent* with local data
  (a static bound gives <= 0.551) but not pinned by it. Recorded honestly in `pse.py`;
  noted here so it is not mistaken for a measurement.

---

## Closed

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
