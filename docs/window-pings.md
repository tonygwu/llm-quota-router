# Starting waiting subscription windows

`quotapick window-ping --dry-run` reads the poller's status history and lists every
configured subscription pool, its selected model, waiting readings and skip reasons.
It sends nothing and writes nothing. Omitting the mode also means dry-run.
`--json` includes attempted, succeeded, failed and failed_by_kind. API credit pools
are never candidates. The command itself never fetches usage or reads credentials.

A waiting account-wide window either has an explicit provider `not_running` marker,
or has used fraction zero and a reset within 240 seconds of **its observation time**
plus its length. The tolerance covers the 120-second poll cadence and Antigravity's
measured offsets, including rare 97-second offsets. A model-scoped limit never
triggers a ping. Claude's no-deadline answer is retained as `not_running` in status
JSON/history; it is not manufactured into a Window with a fake reset.

The test also accepts used zero with reset **exactly** the previous reset plus the
window length, where the prior reading preceded that reset. This is the explicitly
requested widening for Antigravity's fixed stretch after a scheduled reset. Without
an observed predecessor it cannot infer that case. The command scans eight days of
retained history (one weekly predecessor plus a day); absent history remains absent.
Readings over 600 seconds old (five polls), future readings, failed reads, disabled
accounts, ended plans and unsupported providers cannot trigger a call. An exhausted
account-wide companion window also blocks the call to avoid vendor credit overages.

One successful pool ping blocks each waiting window for that window's full length,
counted from the success time: five hours or seven days. An early reset does **not**
clear this cooldown. Either window may trigger a ping when its own length has elapsed.
There is no recent-run test, schedule, or per-window "already pinged" flag. A failed
attempt retries no sooner than 30 minutes later. A crash after the durable attempt
record receives the same backoff. The CLI timeout is 60 seconds for every provider;
process-group termination also stops a child behind the Antigravity user wrapper.

## Vendor calls and evidence

Each call asks exactly `1 + 1 = ?` in an empty workspace through the account's CLI:

| Pool | Model | Invocation |
|---|---|---|
| Claude | `claude-haiku-5-5` | `claude -p`, account config, safe mode, no tools |
| Codex | `gpt-6-luna` | `codex exec`, account CODEX_HOME, read-only, low effort |
| Antigravity Gemini | `gemini-3.8-flash-low` | same `agy_argv_env` user-switch recipe as usage |
| Antigravity Claude/GPT | `gpt-oss-120b-medium` | same agy recipe; independent pool |

Claude uses `CL_CLAUDE_BIN` or `~/.local/bin/claude`; Codex uses
`QUOTA_ROUTER_CODEX_BIN` or `/opt/homebrew/bin/codex`. Antigravity uses the usage
adapter's binary resolution, including the configured alternate-user wrapper.
API-key/provider override environment variables are stripped. Credential renewal
inside the vendor's CLI is allowed; the router never performs it.

GPT-OSS 120B is the least expensive listed Claude/GPT model by published Google
Cloud token prices: $0.09/M input and $0.36/M output. This is a price-based choice,
not a measurement of Antigravity's undisclosed subscription quota weights.
The current `agy models` listing includes `gpt-oss-120b-medium`. Google announces
its removal on **November 2, 2026**; replace it before live operation beyond that
point. There is no silent fallback to another model.
Sources: [Google pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing),
[Antigravity models and retirement](https://www.antigravity.google/docs/models/).

The audit records the asked and served model, tokens, trigger reading, start and
completion times, and outcome. A successful exit or model-selection echo is
insufficient. Missing tokens, a missing/different served model, wrong arithmetic
answer and account mismatch are named failures. Tokens remain recorded when model
validation fails after a call. Antigravity compares its own sign-in log with the
configured identity.

**Dry-run qualification:** saved Claude result JSON provides `modelUsage`. Saved
agy result JSON provides tokens but no served model; its "Propagating selected model"
log is only selection evidence. Codex JSONL can likewise provide `turn.completed`
usage without a model. Those shapes produce `model_unverified`, never a successful
cooldown entry. A response/turn model stamp, if provided, must match the asked model.
Current live inference telemetry has not been qualified by this dry-run release.
Do not enable recurring pings until that provider evidence is resolved; otherwise
an unverified call is retried after the failure backoff. No provider prompt has been
sent as part of this release's dry-run verification.

## Verification and recovery

`window-pings.jsonl` in the router state directory is append-only. An exclusive lock
covers planning, the durable pre-spawn attempt and the result. A malformed journal
or history read fails closed. The next conclusive fresh observation after a success
must show a reset within 240 seconds of the ping start plus the window length.
An early zero reading within that tolerance is inconclusive (it could still be
sliding), so verification waits for a later observation outside the tolerance.
Cached pre-ping observations and failed reads cannot count as verification.

The verification record includes the actual reading and whether the boundary
remained unchanged or moved. This preserves evidence for the Antigravity fixed-stretch
question. A mismatched boundary or explicit no-window answer stops **that window**
until operator inspection; other windows retain their eligibility. The stop persists
across restarts and is printed on every run. After inspection:

```sh
quotapick window-ping --execute --resume-window ACCOUNT:5h
```

This appends a resume record and sends nothing. It does not erase cooldown history.
Successful pings are still limited at pool level.

## Deployment gate

The existing 120-second usage-poll job gains `window-ping --dry-run --json` after
status and capability refresh. Its output goes to `window-ping.log`, with the same
log rotation as the other poller logs. The job retains status's exit code; ping
failures are visible in the ping log. No new schedule is created.

After the operator approves deployment and resolves the existing live-install gates
in AGENTS.md, install **only from repo-prod**, then re-run the installer there.
Its default is dry-run. Only a deliberate operator-approved reinstallation with
`WINDOW_PING_MODE=execute ./ops/install-launchd.sh` enables scheduled pings.
Returning to the default installer disables them again. Inspect both the ping log's
counts and subsequent journal verification records; an installed file is not proof
of a successful ping or window start.

## Router-model follow-up

The scorer and waste detector still treat published reset timestamps as deadlines,
including waiting windows. A synthetic pool with both windows waiting, capacity 2.43,
weekly/session ratio 4 and demand 0.04 PSE/h reports **3.000 PSE at risk**. There is no
expiring stock while both windows wait. A weekly waiting window observed twice with
a 310000-second gap also becomes an "early" reset with the whole weekly stock
forfeited (9.72 PSE with that plan). Dense sliding reads instead produce drift warnings.
This is follow-up work, deliberately outside the ping implementation: retain the
real weekly risk when only the five-hour window waits, and never rewrite historical
waste rows to fix the model.

Reproduce the router-model observations without operator state:

```sh
PYTHONPATH=src ./.venv/bin/python - <<'PY'
from quota_router.config import AccountConfig, Config
from quota_router.pse import Plan
from quota_router.scoring import rank
from quota_router.types import AccountSnapshot, Window
from quota_router.waste import detect_resets
T = 1800000000.0  # injected fixture time, not a file timestamp
account = 'antigravity_gemini'
def reading(t):
    return AccountSnapshot(id=account, source='live', capacity_override=2.43,
        windows=tuple(Window(key=k, used_fraction=0, length_s=n,
                            resets_at_s=t+n, observed_at_s=t)
                      for k, n in [('5h', 18000), ('7d', 604800)]))
config = Config(accounts={account: AccountConfig(id=account, weekly_to_session=4)})
print(rank([reading(T)], T, config)[0].reason)
records = [{'t': t, 'accounts': [reading(t).to_dict()]}
           for t in [T, T+310000]]
result = detect_resets(records, plans={account: Plan(weekly_to_session=4)})
print([(r.kind, r.observed, r.wasted_pse) for r in result.records])
PY
```

Output: `3.000 PSE at risk of expiring (binding 5h, tier scale 2.43)` and
`[('early', True, 9.72)]`.
