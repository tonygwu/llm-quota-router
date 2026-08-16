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

```
slack(account, window) = remaining_fraction - expected_demand_before_reset
    expected_demand    = burn_rate * time_to_reset
    burn_rate prior    = 1 / window_length          (uniform pacing prior)

min_slack(account)  = min over applicable windows of slack
capacity(account)   = tier capacity ratio

REGIME A - any candidate has min_slack > 0:
    score = provider_weight * capacity * min_slack      -> argmax
REGIME B - every candidate has min_slack <= 0:
    score = provider_weight * capacity * min_remaining  -> argmax
```

**When anyone has surplus, spend from the pool with the most quota at risk of
expiring unused. When nobody does, spend from the pool that can actually serve
the call.**

Three details that are easy to get wrong:

- **`min` across windows.** A five-hour window throttles access to weekly
  surplus, so an account is only as good as its tightest *applicable* window.
  Windows scoped to a model class (a Fable-specific weekly allowance, or
  Codex's per-model buckets) are skipped when you ask for a different class —
  which is the whole mechanism by which model choice gates routing.
- **Two regimes, not one formula.** Multiplying a *negative* slack by a
  capacity ratio moves it toward zero, which makes a smaller account look
  *better* under scarcity. Surplus and deficit are different objectives:
  minimize waste versus maximize served requests.
- **The hysteresis margin applies to unscaled slack.** An additive epsilon on a
  capacity-scaled score is four times stricter for a quarter-size account —
  an accidental bias, not a policy.

Use-it-or-lose-it policies oscillate, so a switch requires clearing a margin
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

```sh
uv tool install llm-quota-router     # or: pipx install llm-quota-router
```

## Use

```sh
quotapick pick --model fable --json      # decision + full ranking as JSON
quotapick explain --model fable          # why that account won
quotapick status                         # every pool at a glance
quotapick exec --model opus -- claude -p "..."   # pick, then run
```

From Python:

```python
from quota_router import select_account
decision = select_account(model="fable")
env = {**os.environ, **decision.exec_env}
```

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
