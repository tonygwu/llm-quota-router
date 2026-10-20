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
| Antigravity Claude/GPT | `claude-sonnet-5-5-low` | same agy recipe; independent pool |

Claude uses `CL_CLAUDE_BIN` or `~/.local/bin/claude`; Codex uses
`QUOTA_ROUTER_CODEX_BIN` or `/opt/homebrew/bin/codex`. Codex's child PATH puts that
binary's directory first so its `env node` shim finds the sibling Node executable
under launchd's restricted PATH, then preserves the other search directories.
Antigravity uses the usage adapter's binary resolution, including the configured
alternate-user wrapper.
API-key/provider override environment variables are stripped. Credential renewal
inside the vendor's CLI is allowed; the router never performs it.

The operator selected Claude Sonnet 5.5 (Low) on 2026-10-20 to replace GPT-OSS
before its retirement. Native `agy models` returned the exact id
`claude-sonnet-5-5-low` with label `Claude Sonnet 5.5 (Low)`. This is the operator's
model choice, not a claim about the cheapest token price or Antigravity's
undisclosed subscription quota weights. There is no silent fallback.

The audit records the asked and served model, tokens, trigger reading, start and
completion times, and outcome. A successful exit alone is insufficient.
Missing tokens, a different served model, wrong arithmetic answer and account
mismatch are named failures. Tokens remain recorded when model validation fails
after a call. Antigravity compares its own sign-in log with the configured identity.

Under the operator's 2026-10-20 decision, a response without a served-model stamp
records `model_evidence: "selected_model_only"` and `model_served: null`.
If its other checks pass, it succeeds and starts the full pool cooldown.
A matching stamp records `model_evidence: "served_model"`. Neither a selection
echo nor a model's answer about its own name becomes a served stamp. A malformed
stamp or any conflicting Claude `modelUsage` entry, including a supplied `canonicalModel`, fails. Saved successes without
a served stamp require the explicit selection-only label when the journal reloads;
older successes with matching served stamps remain readable.

**Dry-run qualification:** three separately approved native diagnostic calls ran at
2026-10-20T00:37:24Z–00:37:34Z, one per provider, with no retry. All returned the
arithmetic answer. Full raw responses were inspected and retained privately. The
Claude response used `1 + 1 = **2**`; the initial strict answer check rejected that
format. A failing-then-passing regression now accepts the observed plain and bold
arithmetic forms, while rejecting unrelated text. Replaying the same saved responses
with that fix gives attempted=3, succeeded=1, failed=2,
failed_by_kind={model_unverified: 2}; no additional prompts were sent.

| Provider | Served-model evidence | Recorded tokens |
|---|---|---|
| Claude | `modelUsage` names `claude-haiku-5-5` | input 2, output 12, cache read 649, cache creation 2242 |
| Codex | `turn.completed` has usage, no model stamp | input 21267, cached input 11648, output 5 |
| Antigravity | selected label `GPT-OSS 120B (Medium)`, no served stamp | input 4292, output 43, thinking 0, cache read 2880 |

Native auth checks before those calls reported Claude's `claude.ai` Max subscription
and Codex's `Logged in using ChatGPT`. Claude/Codex account selection is evidenced by
the configured native CLI directory and auth status, not an inference response's
served-account stamp. Antigravity's native sign-in identity matched configuration.
Its "Propagating selected model" log remains only selection evidence. The initial
parser reported the missing stamps as `model_unverified`; the new operator decision
changes future calls to selection-only successes when the remaining checks pass.
The diagnostic calls did not establish a waiting-window start: those accounts
already had running windows.

Keep recurring pings dry-run until the coordinating session activates them under #29.
The operator approved live pings and accepted the missing served-model stamps, the
exact previous-reset-plus-length waiting test, and literal elapsed-time cooldowns.
The calibration ledger and v3 reset writer were adopted on October 20 at `7f87887`.
That installation preserved the plist, which still has no ping invocation, and
does not include the later ping changes or waiting-capacity writer. The separate
raw-history adoption under #32 completed on October 20. Installing those changes
still needs a revision at or after `e979c1a`, and a replay of that revision's writer
against the adopted records whose first run passes, as #32 records.
The coordinating session owns activation. Post-ping boundary verification remains
necessary, including the Antigravity fixed stretch.

### Model-name probes, 2026-10-20

A fresh reviewer approved the concrete execution plan before native calls.
Between 05:16:00Z and 05:17:49Z, nine pools each received exactly one
`what model are you?` question through their own CLI, with a 60-second timeout
and no retries. `claude_b` was skipped before invocation because its latest
account-wide window was exhausted. The disabled `claude_d` was excluded.
Native preflights: attempted=9, succeeded=9, failed=0, failed_by_kind={}.
Model-name probes: attempted=9, succeeded=9, failed=0, failed_by_kind={}.
Success here means the diagnostic returned a nonempty reply and passed the
applicable telemetry checks. It does not establish that a self-reported model
name is true or that the production arithmetic check was exercised.

Full raw stdout, stderr, invocation timestamps, auth checks and derived audits
are retained privately. Every full inference response was inspected. The
following replies are summarized to omit temporary workspace paths; all are
**weak self-reports**, separately classified from telemetry.

| Pool | Model asked | Reply says | Telemetry | Input / output tokens | Cache read / creation |
|---|---|---|---|---|---|
| `claude` | `claude-haiku-5-5` | Claude Haiku 5.5, exact requested id | Matching `modelUsage` | 2 / 60 | 649 / 2240 |
| `claude_b` | `claude-haiku-5-5` | Not called: quota exhausted | No probe | — | — |
| `claude_c` | `claude-haiku-5-5` | Claude Haiku 5.5, exact requested id | Matching `modelUsage` | 2 / 101 | 649 / 2243 |
| `claude_e` | `claude-haiku-5-5` | Claude Haiku 5.5, exact requested id | Matching `modelUsage` | 2 / 97 | 649 / 2243 |
| `codex` | `gpt-6-luna` | “I'm GPT-6.” | Selected model only | 22114 / 10 | 11648 / 0 |
| `codex_b` | `gpt-6-luna` | “I'm GPT-6.” | Selected model only | 22226 / 10 | 11648 / 0 |
| `antigravity_gemini` | `gemini-3.8-flash-low` | Gemini 3.8 Flash | Selected model only; log names Flash (Low) | 8356 / 19 | 0 / not reported |
| `antigravity_claude` | `claude-sonnet-5-5-low` | Antigravity assistant; selected Claude Sonnet 5.5 (Low) | Selected model only; log names Sonnet (Low) | 13088 / 56 | 0 / not reported |
| `antigravity_gemini_b` | `gemini-3.8-flash-low` | Gemini 3.8 Flash | Selected model only; log names Flash (Low) | 7661 / 17 | 0 / not reported |
| `antigravity_claude_b` | `claude-sonnet-5-5-low` | Antigravity assistant; selected Claude Sonnet 5.5 (Low) | Selected model only; log names Sonnet (Low) | 11912 / 56 | 0 / not reported |

Codex's cached input is included in its reported input, and its raw response
reports cache-write and reasoning-output tokens as zero. Antigravity reports
thinking tokens as zero on all four calls. Claude's cache counts are separate
fields from its two input tokens. These are vendor counters, not comparable
token-cost totals.

Native auth status identified the three Claude accounts as `claude.ai` Max
subscriptions; both Codex accounts reported `Logged in using ChatGPT`.
Their configured CLI directories select the account, but neither provider's
inference response gives a served-account stamp. Each Antigravity native sign-in
matched its configured identity, including both alternate-user invocation paths.
No raw credentials were read or used by the probe harness.

These diagnostics wrote no production ping journal entries and activated no
recurring job. The model-name allowance is spent for the nine called pools;
an exhausted pool was not retried. #29 remains open for ledger adoption,
installation, and actual poller/journal verification.

The ordinary poller's 05:19:23Z observation showed nonzero five-hour usage in
all four Antigravity pools, with these resets. No extra usage request was sent.

| Pool | Reset, UTC | Offset from probe start + five hours |
|---|---|---|
| `antigravity_gemini` | 2026-10-20T10:16:48Z | +3.994 seconds |
| `antigravity_claude` | 2026-10-20T10:17:02Z | +3.610 seconds |
| `antigravity_gemini_b` | 2026-10-20T10:17:27Z | +2.818 seconds |
| `antigravity_claude_b` | 2026-10-20T10:17:47Z | +3.023 seconds |

These observations support starts near the native inference calls. They do not
exercise the production journal, and the preflight showed sliding resets rather
than the ambiguous fixed stretch after a scheduled reset. That specific case
remains to be observed during activation.

At 05:19:39Z, `PYTHONPATH=src ./.venv/bin/python -m quota_router.cli window-ping --dry-run --json`
returned attempted=0, succeeded=0, failed=0, failed_by_kind={}, would_ping=0.
All enabled supported pools reported `no_waiting_window`; `claude_d` was disabled
and `cursor` unsupported. This was a read of live history, not an installed job.

## Verification and recovery

`window-pings.jsonl` in the router state directory is append-only. An exclusive lock
covers planning, the durable pre-spawn attempt and the result. A malformed journal
or history read fails closed. The next conclusive fresh observation after a success
must show a reset within 240 seconds of the ping start plus the window length.
For Claude's five-hour window only, the allowed reset ranges from 840 seconds
before that expected time through 240 seconds after it, inclusive. This adds
Claude's ten-minute start flooring to the existing tolerance on the early side.
Claude's weekly window and other providers keep the symmetric 240-second band.
An early zero reading is inconclusive because it could still be sliding, so
verification waits beyond 840 seconds after completion for Claude's five-hour
window, or beyond 240 seconds for the others. A nonzero reading can verify earlier.
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

The scorer still treats published reset timestamps as deadlines, including waiting
windows. A synthetic pool with both windows waiting, capacity 2.43, weekly/session
ratio 4 and demand 0.04 PSE/h reports **3.000 PSE at risk**. Correcting that ranking
remains #28's work. The shared temporal classifier now prevents dense and sparse
sliding reads from becoming resets in the waste detector. A 310000-second gap
between waiting observations produces no early forfeit. Instead, the forgone
report counts elapsed paid capacity at the pool's binding rate. A running weekly
window beside a waiting five-hour window retains real reset risk. Historical
`waste.jsonl` rows are never rewritten. See the
[waiting-capacity reconstruction](measurements/2026-10-20-forgone-capacity.md).

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
`[]`.
