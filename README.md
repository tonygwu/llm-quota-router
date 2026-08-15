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

These are enforced by tests, not merely by intent: the suite asserts that no
exec plan ever carries a proxy environment variable, and that no library or CLI
code path invokes an account-mutating command.

## Relationship to `claude-swap`

[`claude-swap`](https://github.com/realiti4/claude-swap) (MIT) already solved
the hard, drift-prone parts: polling each account's usage, Keychain access,
per-model rows, and safe switching. This project **depends on it as a quota
oracle** and reimplements none of that.

**`cswap` is an oracle here, never an executor. That is a contract guarantee,
not an implementation detail.** Only `cswap list --json` is ever invoked.
Execution always goes through the vendor CLI under a per-invocation
`CLAUDE_CONFIG_DIR`, because that is what keeps concurrent runs against
*different* accounts genuinely parallel — credential swapping serializes them
behind a lock, which would roughly double the wall-clock of a fan-out workload.
Do not "simplify" this by adopting `cswap run`.

The genuine difference in scope: `cswap` switches the **global default
account**; this switches **per invocation**, across providers, using
deadline-aware scoring.

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

## Keeping the oracle fresh

`cswap` stores a **copy** of each account's credentials. The live config dirs keep
refreshing their own OAuth tokens; those copies do not. After a few hours a copy
expires, `cswap` reports `usageStatus: "relogin_required"`, and the router — correctly,
but silently — stops seeing that account. Observed in practice: two of three accounts
went dark within roughly seven hours of registration.

The router degrades safely (it warns and skips rather than guessing), but a skipped
account is an invisible one, so left alone the fleet narrows itself.

```sh
./ops/refresh-oracle-credentials.sh --check   # report status, mutate nothing
./ops/refresh-oracle-credentials.sh           # re-capture every configured account
./ops/install-launchd.sh                      # do it every 30 min (macOS)
./ops/install-launchd.sh --uninstall
```

Re-capturing is idempotent and non-destructive: it copies credentials *into* cswap's
own registry and never changes which account is globally active, so concurrent spawns
against different accounts stay isolated.

**This is operator tooling, deliberately outside `src/`.** The package's oracle-only
guarantee — `cswap list --json` and nothing else — is about the *router* never mutating
account state while making a routing decision, and `tests/test_portability.py` enforces
it across the package. Refreshing the oracle's own credential cache is a separate,
explicitly-invoked maintenance action, so it lives in `ops/` where that rule does not
and should not reach.

## Design notes

- **Pure core.** `scoring.py` and `select.py` import only stdlib and the type
  module, take `now_s` as a parameter, and touch no filesystem, clock, or
  environment. A portability test enforces it.
- **Degrades, never fails.** Per account: a live oracle reading, then a cached
  statusline reading with confidence decaying by age, then a synthesized
  estimate shrunk toward the sibling mean. Unknown accounts are never buried —
  shrinking toward the mean rather than scaling by confidence keeps them
  eligible, and an exploration bonus occasionally probes a dark one.
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
