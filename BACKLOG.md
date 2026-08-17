# Backlog

Open gaps, roughly by how much they matter. Each entry says what would close it, so
"done" is checkable rather than a judgment call.

Kept free of machine-specific paths and account identities: publishing this repo is a
live option, and a backlog is the file most likely to leak them.

---

## 1. The core claim has never been measured

The tool exists to reduce **wasted quota** — budget that expires unspent at a window
reset. That number has never been computed. Every justification so far has been
mechanical ("the scorer now compares like units") rather than empirical.

This is the gate the rollout plan puts on publishing, and it is still wide open. It also
cannot be back-filled from existing history: the observation-clock fix changed which
readings count, so any measurement taken before it is invalid.

**Closes when:** remaining-fraction-at-reset is reported per account per window, router
versus the counterfactual dial, across at least one full weekly cycle. If the number is
unimpressive, that is the finding — publish it or shelve the project, but do not keep
shipping scoring changes justified by argument alone.

## 2. An idle account becomes unreadable, and unreadable means unroutable

OAuth *access* tokens are renewed only by **using** an account, and this tool
deliberately refuses to redeem a refresh token (see the single-writer rule). So an
account nobody touches for ~8h goes dark: the usage read fails, the snapshot comes
back with no windows, and the account drops out of routing.

That is a starvation loop, and it inverts the entire objective. The account with the
most quota left is by definition the one being used least, so it is the *first* to go
dark — and then the router cannot spend from it, which keeps it dark. Observed live:
the account at 47% Fable was invisible while the router picked one at 97%, and the
loop only broke because the operator happened to launch that account by hand.

The degradation ladder is supposed to cover this (cached reading, then synthesise at
sibling-mean with reduced confidence, plus an exploration bonus for accounts dark >6h)
but none of it fired — the cache was past its staleness bound and the empty live
result was taken at face value instead of being treated as a failure.

**Closes when:** a read failure cannot produce a scored snapshot; the ladder is
exercised by a test that darkens one account and asserts it is still reachable; and
the "dark account" case is explicitly the one the exploration bonus is measured on.

## 3. An unreadable account is reported as a readable one with no limits

Same incident, separable bug. The failed account was published with `source: "live"`,
zero windows, and no entry in `degraded`. The only clue in the ranked output blamed
the data shape — "no fable window in this snapshot ... its fable limit is unmeasured"
— while the real cause ("access token expired") sat in a `note` field that nothing
surfaced. Anyone reading that goes hunting for a window-parsing bug.

This is the accept-and-guess failure in its exact classic form: an error became an
empty-but-plausible value, and every consumer downstream treated it as data.

**Closes when:** a snapshot that came from a failed fetch cannot be labelled `live`,
the reason travels with it, and exclusion messages distinguish "measured and spent"
from "could not be read".

## 4. The k adoption gate cannot fire on poller-only data

`ADOPTION_MAX_GAP_S` is 900s. The usage poller's `StartInterval` is 900s. The gate
therefore requires the data to be denser than the process producing it, and launchd
scheduling jitter alone puts every real gap over the line. Live, every account reports
"worst sample gap 30m > 15m" and no estimate is ever adopted; the one account that ever
passed had dense interactive traffic, not poller data.

The bar should be expressed relative to the sampling cadence rather than as an absolute
that silently duplicates it.

**This is the second unreachable gate in this file's history** (the first required 20pp
of weekly movement, which no plausible k permits within one session window). Both read
as "not enough evidence yet" rather than as a bug, which is what made them survive.

**Closes when:** the gate is derived from the observed cadence, and a test asserts a
clean series sampled at the poller's own interval actually passes.

## 5. `calibrate` recommends a `calls_per_window` adoption we believe is harmful

It prints a paste-ready config stanza. Adopting it is a bad idea, for a reason the
estimator cannot fix: the numerator counts only picks the router made, while the
denominator moves for **all** consumption, including interactive use of the same
account. The confound is structural, so more data will not resolve it.

The docstring already concedes the low bias but calls it safe — "an over-eager
reservation wastes a few seconds of routing preference". Contradicted in practice: a
batch produced 79 reservations that subtracted 39.5% of a window and self-throttled the
router into refusing to route. Adopting the estimate would roughly double every
reservation.

**Closes when:** the suggestion is either suppressed, or emitted with the confound
stated and the pileup consequence quantified. A tool that recommends an action its own
authors consider harmful is worse than one that stays quiet.

## 6. Pileup reservations expire on a timer, not on completion

Each pick books a reservation that decays after 60s. Nothing releases it when the call
finishes, and nothing reconciles it once the usage endpoint reflects the real burn — so
for a window the same consumption is counted twice, once as a reservation and once as
measured usage.

Bounded by the 25% cap, so it degrades routing quality rather than breaking it. Fixing
it properly needs callers to report completion, which is an API change every consumer
must adopt — worth designing before building.

**Closes when:** a reservation is released by the event that made it obsolete, and a
test covers the overlap window where both signals are present.

## 7. `calibrate --days` filters on a different clock than it measures on

`--days N` selects records by write time; the series is now keyed on observation time.
A stale republished reading can therefore pull data older than the requested window into
the estimate, which is why `--days 0.5` can report a 28-hour span.

Benign today — the gap guard drops exactly those pairs — but the output invites the
reader to trust a window boundary the tool is not enforcing.

**Closes when:** both the filter and the series use the observation clock, or the
reported span is labelled as what it is.

## 8. Small, but each one costs trust

- **The adoption message can print "interval 15% wide, want <=15%"** and then refuse.
  The true value is 15.9%, rounded for display before being compared. It reads as the
  tool contradicting itself, which is worse than the rounding error.
- **`EligibilityConfig.min_remaining_configured`** distinguishes an explicitly
  configured floor from the identical built-in default, so a config file that spells out
  the default value behaves differently from omitting it. Defensible, but it was
  introduced without review and is the kind of semantics that surprises someone later.
- **`fable_fraction = 0.5`** is documented by the vendor and *consistent* with local data
  (a static bound gives <= 0.551) but not pinned by it. Recorded honestly in `pse.py`;
  noted here so it is not mistaken for a measurement.
