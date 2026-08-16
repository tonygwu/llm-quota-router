# llm-quota-router

Picks which of **your own already-authorized** LLM accounts to spend on each
invocation, so quota that would otherwise expire unused gets used first.

> **Status: private.** This repo is not published. See
> [Publishing](#publishing) for the bar it has to clear first.

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
weekly capacity  = weekly_to_session  x tier_scale     (k ~ 12, calibrated)
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
report the account `unknown` rather than mint a new one, so an account nobody has
touched in a day goes dark. In practice the gap is narrow -- any account the
router routes work to is refreshed by that traffic, and headless `claude -p` runs
refresh exactly like interactive sessions do.

## Install

As a **command-line tool** (gives you `quotapick` on PATH, isolated venv, not
importable elsewhere):

```sh
uv tool install git+ssh://git@github.com/tonygwu/llm-quota-router
```

As a **library dependency** of another project (this is what you want if you are
going to `import quota_router`):

```sh
uv add git+ssh://git@github.com/tonygwu/llm-quota-router
# or: uv pip install git+ssh://git@github.com/tonygwu/llm-quota-router
```

Those are different things. `uv tool install` deliberately isolates the package,
so it will *not* satisfy an `import` in your project.

Zero runtime dependencies, Python >= 3.12.

## Use

From the command line:

```sh
quotapick status                         # every pool at a glance
quotapick explain --model fable          # why that account won
quotapick pick --model fable --json      # decision + full ranking as JSON
quotapick exec --only claude_b -- claude -p "..."   # pick, then run
```

From Python:

```python
import os, subprocess
from quota_router import select_account

decision = select_account(model="fable", only=["claude", "claude_b", "claude_c"])

env = {**os.environ, **decision.exec_env}
subprocess.run(["claude", "-p", prompt], env=env)
```

Three things that will bite you if missed:

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

`select_account` never raises on routing failure — an unreachable endpoint, an
exhausted fleet, and a malformed config all come back as a degraded `Selection`
with `warnings`. Pass `record=False` when you are only inspecting, so a decision
you never act on does not book anyone's quota.

From any other language, shell out to `quotapick pick --json` and honor the
`exec.env` block — that is the whole integration surface.

`pick` emits valid JSON and exits 0 on every path except a flag/config error,
including when every account is exhausted (you get the earliest-reset candidate
with `fits: false`). Callers should never have to handle a crash.

## Keeping history dense (optional)

The router reads usage live on every invocation, so nothing needs to run on a
schedule for it to work. The optional poller exists only to keep `history.jsonl`
dense, which is what burn-rate learning reads:

```sh
./ops/install-launchd.sh              # `quotapick status` every 30 min
./ops/install-launchd.sh --uninstall
```

It is a reader like everything else here: it mints nothing and writes to no
credential store. Skipping it costs you learning quality, never correctness.

## Design notes

- **Pure core.** `scoring.py` and `select.py` import only stdlib and the type
  module, take `now_s` as a parameter, and touch no filesystem, clock, or
  environment. A portability test enforces it.
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

This repo stays private until the routing actually demonstrates its premise.
The bar is a measured reduction in **wasted quota** — remaining fraction at each
window reset, per account, compared against the previous router — across at
least one full weekly cycle. Agreement rate against the old dial is explicitly
*not* the metric: the point is to disagree with it, correctly.

Before any public push: strip absolute home paths, account emails and identity
keys, and operator-specific measurements in favor of synthetic fixtures. Once
published, `contract_version` becomes a public stability commitment.

Upstreaming the scorer into `claude-swap` is a live alternative to publishing
separately, and would likely reach more people with less maintenance — both
projects depend on the same undocumented, drift-prone surfaces.

## License

MIT.
