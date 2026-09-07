# llm-quota-router

Picks which of **your own already-authorized** LLM accounts to spend on each
invocation, so quota that would otherwise expire unused gets used first.

> **Status: published 2026-09-06.** Installable from GitHub, not on PyPI. See
> [Publishing](#publishing) for what the router has still not measured.

## The problem

Subscription quota is **perishable**. If your weekly window has 30% left and
resets in three hours, that 30% is gone either way. If another account has 35%
left and six days to go, that quota is genuinely scarce. A router that only
compares "percent remaining" cannot tell those apart, so it hedges — and you
throw away the perishable half.

## The rule

Percentages are the only thing the vendor publishes, and they are **not comparable
to each other**: 89% of a five-hour budget and 23% of a weekly budget have
different denominators, and a 20x account's percentage point is four times a 5x
account's. So the first step is always to convert into one absolute unit.

The unit is the **PSE**: one Max-20x five-hour budget.

```
session capacity = 1.0                x tier_scale     (20x -> 1.0, 5x -> 0.25)
weekly capacity  = weekly_to_session  x tier_scale     (k = 6.25, per account)
fable capacity   = fable_fraction     x weekly         (0.5, documented)

fable_remaining  = min(fable sub-cap remaining, weekly remaining)

absorbable = min( what the 5h window supplies over the horizon,
                  your working rate x the horizon )

waste      = max(0, weekly_remaining - absorbable)      ->  argmax
```

**Spend from the pool carrying the most quota that will expire unused.** When no
account will waste anything, drain the one whose window resets soonest.

Four details that are easy to get wrong:

- **Fable is a constraint, not a pile.** Fable work draws down its own sub-cap
  *and* the shared weekly pool, so its true availability is the minimum of the
  two. Modelling it as an independent bucket overstates every account whose
  weekly pool is nearly dry.
- **The five-hour window is a flow, not a stock.** It refills every five hours,
  so a nearly-full window about to reset is not "about to be wasted" -- it is
  about to be replaced. Valuing it as a stock makes the router chase windows it
  cannot fill.
- **Absorbable is capped by your own rate.** The five-hour window caps
  consumption at one PSE per five hours, so 0.20 PSE/hour is the physical
  ceiling on any sustained rate. Anything above that is not a workload, it is an
  arithmetic error.
- **No regime switch.** An earlier version needed one because its objective could
  go negative, and multiplying a negative by a tier capacity ratio inverts the
  ordering -- making a smaller account look *better* under scarcity. Every term
  here is non-negative by construction, so that failure mode cannot arise.

Use-it-or-lose-it policies oscillate, so a switch still requires clearing a margin
*and* a minimum dwell measured in **calls, not seconds** (bursts of invocations
land inside the same second).

## What this is

A tool for **one operator who personally holds more than one subscription**,
deciding which of *their own* already-authorized accounts to spend from on a
given run. It shells out to each vendor's official CLI using that account's own
credentials.

### Providers, and what can actually be measured

A provider is a billing pool with its own CLI. What the router can *know* about
each one differs, and it never pretends otherwise:

| Provider | Accounts | Usage readable? | Read from |
|---|---|---|---|
| `claude` | many, one config directory each | **yes**, real windows | vendor usage endpoint, statusline cache |
| `codex` | one | partial | `$CODEX_HOME/sessions` transcript tails |
| `cursor` | many, one API key each | **no** | `~/.cursor/cli-config.json`, identity only |
| `antigravity` | two pools behind one binary | **no** | nothing; failure-learned deadlines only |

"Usage readable: no" is a statement about the vendor, not a gap to be filled by
estimating. Cursor's CLI has `status` and `about`, and neither reports a quota
figure; Antigravity exposes nothing at all. Those accounts carry no windows and
`confidence=0.0`, so they are routable but never scored as if measured.

Cursor has no per-account config directory, so a second Cursor account is
declared with its own key:

```toml
# ~/.config/quota-router/config.toml
[accounts.cursor_work]
provider = "cursor"
tier = "pro"
env = { CURSOR_API_KEY = "..." }
```

An account this router has no adapter for is **reported, not ignored**. If you
declare a provider that does not exist here, or a second account under a
provider that only ever reports one, `status` says so on stderr rather than
leaving the account quietly missing.

## What this is *not*

- **Not a gateway or proxy.** It never sets `ANTHROPIC_BASE_URL` or
  `ANTHROPIC_AUTH_TOKEN`, and never terminates or forwards an API connection.
  Anthropic blocked third-party harnesses from consuming Claude subscription
  quota via OAuth on 2026-04-04; the only durable architecture is running the
  vendor's real binary under that account's own config directory.
- **Not quota pooling, sharing, or resale.** It never moves quota between
  people. Credentials are never shared, forwarded, or exposed to another party.
- **Not a rate-limit bypass.** It selects among accounts you are already
  authorized to use and stops when they are exhausted. It cannot and does not
  raise any limit.
- **Not a credential manager.** It never mints, refreshes, rotates, or writes a
  token. It reads the access token an account already holds and nothing else.
  See [The single-writer rule](#the-single-writer-rule) for why that boundary is
  load-bearing rather than fastidious.

These are enforced by tests, not merely by intent: the suite asserts that no
exec plan ever carries a proxy environment variable, and that no module contains
refresh-token machinery or a rotating dependency in executable code.

## The single-writer rule

Anthropic's OAuth **rotates refresh tokens**: redeeming one invalidates it and
issues a replacement. Only one holder can win that exchange.

So exactly one process on the machine may redeem a refresh token, and that
process is the vendor's own CLI. **This package is a pure reader.** It reads the
access token the account already holds out of the Keychain and calls the usage
endpoint with it. It never performs a token exchange.

This is not a style preference. An earlier version of this project delegated
usage reads to [`claude-swap`](https://github.com/realiti4/claude-swap), which
redeems the refresh token before every read and stores the replacement in its own
keychain namespace rather than writing it back to the entry the vendor CLI reads.
The CLI was left holding a revoked token and blanked the login on its next
launch. Two of this operator's three accounts were destroyed that way before the
mechanism was understood.

The lesson worth carrying: the old contract said "`cswap` is an oracle; the only
permitted invocation is `cswap list --json`", and a test enforced exactly that.
The contract was satisfied and the accounts still died, because the invocation
was read-only *on disk* and mutating *on the server*. **No argv-shaped assertion
can see that distinction.** The replacement guard bans the dependency outright
and bans refresh-token machinery from executable code
(`tests/test_no_token_rotation.py`).

What we give up: an access token lives about eight hours. If one has expired we
report the account as unreadable rather than mint a new one, so an account nobody
has touched in a day goes dark.

**That gap is not narrow, and an earlier version of this paragraph claimed it
was.** The reasoning was that any account the router routes work to is refreshed
by that traffic — which is circular, because the router will not route to an
account it cannot read. It is self-reinforcing in the worst direction: the
account with the most quota left is the one being used least, so it is the first
to go dark and then stays dark. Seen live, holding 53% of its Fable allowance
while the router picked a pool with 3% left.

Two measurements also contradict the old claim that traffic keeps a token alive.
`claude -p` against a *healthy* token makes a real authenticated call and leaves
the credential byte-identical, and `claude auth status` never reaches the network
at all. Renewal happens only once the token has already lapsed, so ordinary
traffic does not top a token up — it only repairs one that has already died.

The reader boundary is unchanged; see [Waking a dark
account](#waking-a-dark-account) for how the poller breaks the cycle without
crossing it.

## Install

As a **command-line tool** (gives you `quotapick` and `cl` on PATH, isolated
venv, not importable elsewhere):

```sh
uv tool install git+https://github.com/tonygwu/llm-quota-router
```

As a **library dependency** of another project (this is what you want if you are
going to `import quota_router`):

```sh
uv add git+https://github.com/tonygwu/llm-quota-router
# or: uv pip install git+https://github.com/tonygwu/llm-quota-router
```

Those are different things. `uv tool install` deliberately isolates the package,
so it will *not* satisfy an `import` in your project.

On the author's machine this repo has several checkouts, and the live `cl` and
`quotapick` are installed from exactly one of them. See `AGENTS.md` before
running `uv tool install` from a checkout.

Zero runtime dependencies, Python >= 3.12. **macOS only**, and that is the code
rather than the testing: every credential read shells out to `security
find-generic-password`, and both schedulers in `ops/` write launchd plists.
There is no second code path.

## Use

From the command line:

```sh
quotapick status                         # every pool at a glance
quotapick explain --model fable          # why that account won
quotapick pick --model fable --json      # decision + full ranking as JSON
quotapick exec --only claude_b -- claude -p "..."   # pick, then run
quotapick waste                          # what expired unused, per account, in PSE
```

From Python:

```python
import os, subprocess
from quota_router import select_account

decision = select_account(model="fable", only=["claude", "claude_b", "claude_c"])

env = {**os.environ, **decision.exec_env}
subprocess.run(["claude", "-p", prompt], env=env)
```

Five things that will bite you if missed:

- **The winner is not a promise it can serve you — check `fits`.** The objective
  is quota *about to expire*, so an account whose 5-hour window is fully spent
  can legitimately rank first: it has the most weekly quota at risk. That is the
  right answer for a batch job that can wait and a useless one for an
  interactive session. If you need to run *now*, walk `ranked` for the first
  entry with `fits: true`, and have a plan for when none do.
- **`exec_env` is often empty, and that is correct.** The default Claude account
  is selected by the *absence* of `CLAUDE_CONFIG_DIR` — its config lives at
  `~/.claude.json`, outside `~/.claude`, so setting the variable makes the CLI
  scaffold a brand-new empty account. Merge the overlay; never require it to be
  non-empty.
- **Mapping provider to binary is your job.** `exec_env` is an environment
  overlay, not a command. The router says *which account*; it does not know
  whether you meant `claude -p` or `codex exec`, and it will not translate one
  into the other. Branch on `decision.provider`.
- **`--model` does not restrict providers.** It gates which of an account's own
  quota windows are counted. To constrain the provider, use `only`.
- **An account the router could not read is reported, not omitted.** Its
  `excluded` row and a `degraded` entry both carry a reason beginning
  `unreadable:` and naming the cause the provider recorded — typically an expired
  OAuth access token (see [Waking a dark account](#waking-a-dark-account)). Its
  `remaining` is `null`, not `0`: the quota is unknown, not spent. Treat that
  differently from an exhausted account — one needs a login, the other only needs
  time.

`select_account` never raises on routing failure — an unreachable endpoint, an
exhausted fleet, and a malformed config all come back as a degraded `Selection`
with `warnings`. Pass `record=False` when you are only inspecting, so a decision
you never act on does not book anyone's quota.

From any other language, shell out to `quotapick pick --json` and honor the
`exec.env` block — that is the whole integration surface.

`pick` emits valid JSON and exits 0 on every path except a flag/config error,
including when every account is exhausted (you get the earliest-reset candidate
with `fits: false`). Callers should never have to handle a crash.

### `cl` — the reference consumer

`cl` starts an interactive Claude Code session on whichever account has the most
quota about to expire, in bypass-permissions mode. `claude` stays the raw
binary; `cl` only adds account selection.

```sh
cl                        # pick an account, start a session
cl --resume <id>          # same, routed for the model that session ENDED on
cl --model opus[1m] ...   # an explicit --model wins over session detection
```

`--resume` reads the model from the **end** of the transcript, not from a vote
over it: the question is what the next turn will burn, and a session that
switched models mid-way is routed on where it ended up. Sentinel entries like
`<synthetic>` are skipped.

Detection **predicts**; it does not control. `cl` never passes `--model` to
`claude` on the strength of a guess, because a transcript records the server's
*stamp* (`claude-opus-5`) rather than your *selection* (`opus[1m]`) — forcing the
stamp back in would silently drop the variant. Getting detection wrong therefore
costs accuracy in which window was checked, never a change to what you run.

The one exception is an explicit substitution, which is opt-in:

```toml
# ~/.config/quota-router/config.toml
fallback_model = "opus[1m]"
```

When the model a session needs is exhausted on *every* account, `cl` re-routes
for `fallback_model`, passes it through with `--model`, and says so:

```
cl → claude_b · opus[1m]
cl: claude-fable-5 is exhausted on every account; substituted opus[1m] (back in 42m).
```

Here passing `--model` is correct precisely because it is an *override* rather
than a reproduction — the string is yours, taken verbatim from config, so
nothing is lost in translation, and an override that is not passed through does
not happen. Unset by default: quietly running a weaker model is not a decision a
quota router should make for you. Never applied when you named a model yourself.

### When `cl` did not route

`cl` always starts a session — a quota router that cannot answer must never be
why you cannot work — but a launch nobody chose never wears the banner of one
that was chosen. Anything other than a routing decision says so on stderr,
`CL_QUIET` or not:

```
cl → claude_d · opus                       # routed: the router picked this

cl: ROUTER FAILED: pick exceeded 3.0s. No usage data for any account.
cl ⚠ claude_b · opus  [NOT ROUTED -- WEEKLY-RESET FALLBACK, not live quota: of
claude, claude_b, claude_c, claude_d, claude_b's weekly window expires first in 3h20m]
```

The second form is the fallback below. There are two others: `NOT ROUTED --
hardcoded default; every account was read and every one is spent`, and the same
with `no [accounts.<id>] weekly_reset is configured`.

Until 2026-08-24 the give-up line was byte-identical to a real decision, with
the cause written only under `CL_DEBUG`. A blown deadline therefore reached the
operator as a confident-looking route onto an account whose weekly window was
100% spent, while the router's actual answer — recoverable afterwards only by
replaying the recorded snapshot — had been a different account entirely.

**The weekly-reset fallback.** A Claude account's weekly window rolls over on a
fixed weekday and wall time, settled when the account is created, so it is
knowable with no network, no Keychain and no token. Tell the router when:

```toml
# ~/.config/quota-router/config.toml
[accounts.claude]
weekly_reset = "Mon 16:00 America/Los_Angeles"   # wall time in a NAMED zone
```

A zone name, never an offset: `UTC-08:00` is right for eight months a year and
an hour wrong for four, and building the instant from the machine's local zone
passes on a UTC CI box and fails on a laptop. Read your own values off
`quotapick status` — the `7d` window's reset — rather than typing them from
memory.

This is consulted **only** when the router obtained no usage reading for *any*
candidate: a pick that blew its deadline, a pick that crashed, or every token
dark. A reading that says an account is empty is still a reading, and it wins;
`launcher.measured_any` is the gate, and `remaining: null` (unreadable) versus
`remaining: 0.0` (read, and empty) is the distinction it turns on. With no
schedule configured it degrades to the old hardcoded default and names the
setting that would have done better.

**The cached reading breaks the tie.** A schedule alone cannot tell a week that
expires in an hour with everything left from one that expires in an hour with
nothing left; both look equally urgent and only one is worth having. Run against
the 2026-08-24 fleet, the schedule alone picks the exhausted account, because its
week genuinely did expire soonest. So before ranking, candidates whose last
*cached* weekly reading was already spent are dropped:

```
cl: ROUTER FAILED: pick exceeded 3.0s. No usage data for any account.
cl ⚠ claude_d · opus  [NOT ROUTED -- WEEKLY-RESET FALLBACK, not live quota: of
claude_b, claude_c, claude_d, claude_d's weekly window expires first in 21h46m;
skipped claude (cached weekly reading 100% spent this week)]
```

A stale reading is admissible here specifically because this path runs only when
there is no live one — it competes with nothing. Two rules keep it honest:

- **Which week, not how old.** A reading counts only if it was taken inside the
  window we are still in, decided by `WeeklyReset.same_window` against the
  account's own schedule. A reading from before the last rollover describes a
  window that no longer exists, and skipping on it would reject an account that
  has since refilled. Age answers the wrong question: six days old can be
  current, ten minutes old can be a week out of date.
- **The router's own bar.** The threshold is `eligibility.min_remaining`, the
  same floor the live decision layer excludes on — not a second number to keep
  in agreement with the first.

Every uncertainty keeps the account: no cache file, an unparseable one, a
reading from another week. Skipping is the destructive move, so it needs
positive evidence. If every candidate looks spent, the soonest rollover wins
anyway — that account becomes usable first — and the banner says so.

The read is `providers.claude_oauth.cached_weekly_usage`: one local file, no
Keychain, no socket, no token. This path is a recovery from a failure and must
not be able to repeat it.

It ships in this package (`quota_router.launcher`) rather than as a shell script
on PATH because it is the worked example of the integration contract above — a
bug in it gets copied outward — and living here means the suite covers it. Read
it before writing your own consumer: it is ~150 lines and every non-obvious line
has the failure that motivated it written next to it.

Environment overrides: `CL_CLAUDE_BIN` (default `~/.local/bin/claude`),
`CL_PICK_TIMEOUT_S` (default 3 — a wall-clock cap, after which the session
starts on the fallback above rather than a terminal hanging on a Keychain
prompt), `CL_ONLY` (default: every Claude-provider account in your config, so a
subscription you add there is launchable without a second edit), `CL_QUIET`,
`CL_DEBUG`.

## The poller

The router reads usage live on every invocation, so nothing needs to run on a
schedule for routing to work. The poller runs `quotapick status` and produces the two
things the router cannot produce for itself:

```sh
./ops/install-launchd.sh              # `quotapick status` every 15 min ($POLL_INTERVAL_S)
./ops/install-launchd.sh --uninstall
```

- **`history.jsonl` density**, which is what burn-rate learning and `calibrate` read.
- **`waste.jsonl`**, one row per window reset — the measurement described above.

It is a reader like everything else here: it mints nothing and writes to no
credential store. Skipping it costs no correctness, but it does cost the measurement,
and permanently: a reset nothing observed leaves an `observed: false` row whose
remainder can never be recovered.

### Waking a dark account

An OAuth *access* token lasts about eight hours, and this tool will never redeem
a refresh token to renew one. So an account nobody uses goes dark — its usage
read fails, its snapshot has no windows, and it drops out of routing.

That inverts the whole objective: **the account with the most quota left is the
one being used least, so it is the first to go dark, and once dark the router
cannot spend from it, which keeps it dark.** Seen live — an account holding 53%
of its Fable allowance was invisible while the router picked one with 3% left.

The poller breaks the loop by asking Claude Code to do its own job: it spawns the
vendor CLI against that account's config directory for one Haiku-sized prompt,
lets *it* renew the token (it is the only authorised redeemer), and reads again.
No credential is touched here. Enabled by `QUOTA_ROUTER_REFRESH_AUTH=1`, which
`install-launchd.sh` sets for the poller and nothing else sets anywhere.

It is deliberately **not** a scheduled keepalive, because measurement says that
would not work: `claude -p` against a *healthy* token makes a real API call and
leaves the credential byte-identical, and `claude auth status` never touches the
network at all. Renewal happens only when the token has already lapsed, so a
prompt every six hours against an eight-hour token buys nothing and bills you for
it. The spend is worth something only at the moment a read fails.

Rate-limited to one attempt per account per 30 minutes. Some accounts are dark
for reasons a refresh cannot fix — revoked credentials, a logged-out slot — and
without that cooldown they would spawn a CLI on every invocation forever.

## The credential-forensics sampler

Accounts on this machine intermittently lose their stored credential outright and
need an interactive `/login`. Two outages are on record in the poll log:
`claude_d` dark for 27 hours from 2026-08-18 06:15 PDT, and `claude_b` dark for
6.7 hours from 2026-08-20 12:22 PDT plus four brief blips.

**The cause is not established.** Three hypotheses were tested against the
existing data and two of them died:

| Hypothesis | Verdict | What killed it |
| --- | --- | --- |
| The poller's own refresh spawn did it | **Ruled out** for `claude_d` | Healthy at 06:00, blank at 06:15; the cooldown stamp in the 06:30 record proves the first spawn that day was *at* 06:15, after the damage |
| Sheer volume of concurrent readers | **Ruled out** | The default account took 354 headless spawns over five days on top of two live sessions and never went dark; `claude_b` took 22 and went dark five times |
| A config dir was re-pointed at another identity | **Ruled out** | Every slot reported one stable identity across all 460 polls |

What survives is unproven: the risk *may* track the number of concurrently
**active long-lived** sessions on one directory, because a short `claude -p`
against a healthy token performs no renewal while a session crossing the
eight-hour boundary must.

The 15-minute poller cannot settle it — both outages have a fifteen-minute blind
spot around the transition, which is exactly where the answer is. This samples
every 60 seconds instead:

```sh
./ops/install-forensics-launchd.sh              # every 60s ($FORENSICS_INTERVAL_S)
./ops/install-forensics-launchd.sh --uninstall
quotapick forensics --json                      # one sample, by hand
```

On the sample where a credential is lost it appends one record to
`~/Library/Logs/llm-quota-router/credential-forensics.jsonl` containing the
preceding ten minutes: the credential's measured shape and the PIDs, ages and
argv of every process holding that directory, at each step. **That file is the
artifact to read after the next outage.**

Two design points worth knowing:

- **It never spawns anything.** `QUOTA_ROUTER_REFRESH_AUTH` is deliberately absent
  from its plist and must stay absent. An instrument that spawns processes into
  the directory it is measuring is a second suspect, not an instrument.
- **It names no credential field.** The guard in `tests/test_no_token_rotation.py`
  bans the renewal credential's name from executable code, and that field's length
  is one of the two most worth recording — an entry with a live renewal credential
  and a blanked access token failed differently from one where both are gone. So
  the sampler measures the length of *every* string in the OAuth blob generically.
  That satisfies the guard honestly rather than by evasion, and it is strictly more
  informative: it surfaced `rateLimitTier`, which nobody thought to ask for.

## Measuring the thing it exists for

The premise is that quota expires unspent. That number had never once been computed —
every change was justified mechanically ("the scorer now compares like units") rather
than by evidence. `waste.jsonl` is the measurement.

```sh
quotapick waste            # per account per window: resets, observed, missed, PSE
quotapick waste --json
quotapick waste --backfill # re-scan retained history for resets not yet recorded
```

```
account     window     resets  observed  missed  wasted PSE  worst gap
claude      7d              4         2       2        6.56       7.0d
claude      fable           4         2       2        3.50       7.0d
claude_c    7d              1         1       0        0.58       3.0d

7.14 PSE expired unused across 5 weekly reset(s) -- 3 observed, 2 missed.
```

One line per window reset per account, written by `quotapick status` — which the poller
already runs, so a reset is at most one poll interval stale when it is seen. Never on
`pick`: scanning retained history costs ~130 ms once history is a month deep, and the
launcher caps its whole routing decision at three seconds.

Five things the record does deliberately:

- **PSE as well as the fraction.** 55% of a max_20x pool and 55% of a max_5x pool are
  different amounts of lost work, so fractions cannot be summed across a fleet — and a
  fleet total is the point. The conversion is `pse.py`'s, evaluated at the reset instant
  where the horizon is zero and nothing can be absorbed, so the objective the router
  optimizes and the number it is scored on are the same function.
- **`observation_gap_s`.** How long before the reset the last reading was taken. A
  remainder measured eight minutes out is solid; one measured seven hours out is a
  guess. Recorded rather than thresholded — picking a cutoff now would bake one reader's
  answer into a series meant to outlive the question.
- **`k` on every row**, because it is calibrated per account and will change. A series
  computed under one `k` must not be silently reinterpreted under a later one.
- **Idempotent on `(account, window, reset_at)`.** The reset instant identifies the
  occurrence, so re-running — or `--backfill` over a longer history — cannot double-count.
- **Never rotated, never pruned, no clock that could shorten it.** That is the
  requirement, not an oversight; ~400 rows a year is a few hundred KB for the life of the
  project. History pruning trusted a caller-supplied clock and one ad-hoc probe with a
  synthetic `--now` deleted 38 hours of the calibration data the `k` measurement rested
  on. A measurement series that can be silently shortened is worse than none, because it
  still renders — it just answers a smaller question than the one that was asked.

A reset is recognized by `resets_at` moving forward **by at least a full window length**
— a replacement window ends one length after it starts and cannot start before the old
one ended, so nothing smaller can be a rollover. The used fraction dropping is recorded
alongside as a corroborating signal, never as the trigger. Anything that moves the
boundary by less is reported and not recorded: read as a reset, a window that *slid*
rather than tiled would mint a row on every poll, forever, into a file with no retention
policy.

**A missed reset is not a zero.** When the machine sleeps or the poller stops,
`resets_at` comes back several windows on: those resets happened and their remainders are
gone. They are written as rows with `observed: false` and no remainder, and reported as
"3 observed, 2 missed", so a total is always readable as a *lower bound* rather than as
an average taken over a hole.

Two windows are recorded and one is not. The weekly pool and the model-scoped Fable
sub-cap both expire; the five-hour window does not — it refills, so its rollover replaces
quota rather than losing it (see [The rule](#the-rule)). The Fable figure is a *slice of
the same weekly pool*, so it is listed separately and never added into the total. When
that coupling binds — the sub-cap held plenty but the weekly pool it draws on did not —
`remaining_fraction` and `wasted_pse` on that row stop agreeing, and the row carries a
`weekly-coupled` note saying which ceiling produced the number. Without it that
divergence is indistinguishable from an arithmetic bug six months later.

`--backfill` reaches back only as far as `history.jsonl` still goes, which its own
rotation caps at 30 days. It recovers a gap in the waste series; it cannot recover one in
history.

## Design notes

- **Pure core.** `scoring.py` and `select.py` import only stdlib and the type
  module, take `now_s` as a parameter, and touch no filesystem, clock, or
  environment. A portability test enforces it.
- **Two eligibility passes, one judgment.** A call is filtered twice: `cli.py`
  applies *policy* (`--only`/`--exclude`, disabled accounts, `--min-remaining`),
  then the pure layer applies *eligibility* (cooldowns, no applicable window, the
  remaining floor). They stay separate because the first needs `Config` — which
  the pure layer may not import — and the second must work for a caller who has
  no `Config` at all. What must not stay separate is any judgment they both make.
  "Could this account be read?" is one, it lived in `cli.py`, and it was duly
  fixed there and left wrong in `select.select` for a day. It now lives in
  `types.py`, which both already import, and a test asserts the two passes render
  the same verdict for the same snapshot.
- **Degrades, never fails.** Per account: a live usage-endpoint reading, then a
  cached statusline reading with confidence decaying by age, then a synthesized
  estimate shrunk toward the sibling mean. Unknown accounts are never buried —
  shrinking toward the mean rather than scaling by confidence keeps them
  eligible, and an exploration bonus occasionally probes a dark one.
- **Usage reads are cached for two minutes.** The endpoint rate-limits (HTTP 429
  under development-rate probing), and the router runs once per LLM call across
  every account, so an uncached read throttles itself within minutes. When the
  endpoint is unreachable a cache up to 15 minutes old is served *and labelled as
  such*; past that the account reports `unknown` rather than route on numbers old
  enough to be wrong.
- **`history.jsonl` is load-bearing.** Statusline caches only refresh during
  *interactive* sessions, so an account you drive only headlessly reports
  nothing. Every snapshot the router fetches is appended to its own history,
  which within a week yields a dense per-account series that depends on no
  statusline at all.
- **Failure text is classified into three buckets, not two.** Exhausted with a
  deadline, exhausted without one, and *transient*. That last bucket matters:
  `Not logged in · Please run /login` is an OAuth refresh race under concurrent
  headless spawns — the account is fine and an immediate retry succeeds.
  Treating it as exhaustion benches a healthy account.

## Publishing

This repo went public on 2026-09-06 because
[verbatim-index](https://github.com/tonygwu/verbatim-index) imports it as a
library. The bar it was meant to clear first still stands as the test of whether
the router works: a measured reduction in **wasted quota** — remaining fraction at each
window reset, per account, compared against the previous router — across at
least one full weekly cycle. Agreement rate against the old dial is explicitly
*not* the metric: the point is to disagree with it, correctly.

Half of that now exists: `quotapick waste` reports the absolute number (see
[Measuring the thing it exists for](#measuring-the-thing-it-exists-for)), and it starts
accumulating at the next reset. What is still missing is the *comparison* — what a naive
dial would have chosen, replayed against the same history. That is the harder half and
it is deliberately not attempted yet; the absolute number is decision-grade on its own,
because a fleet that turns out to waste almost nothing shelves the project regardless of
what the counterfactual says. `BACKLOG.md` item 1 states what a counterfactual would
need.

The pre-publication check ran on 2026-09-06: the history holds no account
emails or identity keys outside the synthetic fixtures, and the only absolute
home paths describe the author's machine in `AGENTS.md`. `contract_version` is
now a public stability commitment.

Upstreaming the scorer into `claude-swap` is a live alternative to publishing
separately, and would likely reach more people with less maintenance — both
projects depend on the same undocumented, drift-prone surfaces.

## License

MIT.
