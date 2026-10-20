# Notes for coding agents working in this repo

## Coordinator notice, 2026-09-06: history rewritten, repo now public

STOP before any git command if your checkout still has commit `0f5bfd4`.
Every commit hash changed on 2026-09-06 when author emails were rewritten to
the GitHub noreply address ahead of making the repo public. The commit that
was `0f5bfd4` ("Say which checkout owns the live cl") is now `ac0b610`.

- Clean checkout: `git fetch origin && git reset --hard origin/main`
- Local commits on top of the old history:
  `git fetch origin && git rebase --onto ac0b610 0f5bfd4`
- Then, in every checkout:
  `git config user.email 446441+tonygwu@users.noreply.github.com`
  GitHub rejects pushes that carry the old address.

Never force-push over `origin/main`. This notice is the canonical statement of
the new base.

## A word this repo already owns

**"Fleet" in the README and in the code means the operator's fleet of
*accounts*** — `claude`, `claude_b`, `claude_c`, `claude_d`, `codex`. It never
means clones of this repository. For clones this file says **checkout**.

## Checkout layout on this machine

`~/Code/llm-quota-router/` is a container, not a checkout. It holds several
checkouts of this same repository:

| Checkout | Role |
|---|---|
| `repo-prod` | **The live install source.** `~/.local/bin/quotapick` and `~/.local/bin/cl` are installed from here, and the uv receipt records this path. Keep it clean: it should only ever move by `git pull`. |
| `repo-0` … `repo-3` | Working checkouts for agent sessions. Edit here. |

Derive the repo root at runtime (`git rev-parse --show-toplevel`, or
`Path(__file__).resolve()`); never write a checkout-absolute path into
committed code, because it will be wrong in the other four checkouts.

## `uv tool install` is a live mutation — run it only in repo-prod

`uv tool install --force .` replaces the `cl` and `quotapick` binaries that
**every shell launch on this machine uses**. `cl` is the operator's interactive
launcher, so installing work-in-progress code makes every new terminal session
route on it. The failure mode is a wedged or mis-routed launch, not a red test.

To test your own changes, use your checkout's venv, which is isolated from the
installed binaries but not from live state:

```sh
./.venv/bin/python -m pytest -q          # ~1670 tests, ~30s
XDG_STATE_HOME=$(mktemp -d) ./.venv/bin/quotapick status   # your code, scratch state
```

Set `XDG_STATE_HOME` to a scratch folder for every `quotapick` command you run from
a checkout. Without it, the checkout writes to `~/.local/state/quota-router`:

- `pick`, `explain`, `exec` and `run` append history.
- `status` also appends reset rows and new encodings to the calibration ledger.
- From commit `e2cd00c`, `status` and `waste --backfill` also append waiting records
  to `forgone.jsonl`.
- `--dry-run` does not stop these writes.

The ledger and the waiting records only grow, so records from unfinished code stay
there. A waiting record that the installed writer disagrees with can make it refuse
later appends. An empty scratch folder skips the paths that need real history and a
reconstructed ledger. To exercise them, copy live state into the scratch folder
first: `S=$(mktemp -d); cp -Rp ~/.local/state/quota-router "$S/"`, then
`XDG_STATE_HOME=$S`.

To ship a change to the live binaries, after it is pushed:

```sh
git -C ~/Code/llm-quota-router/repo-prod pull
uv tool install --force --reinstall --no-cache ~/Code/llm-quota-router/repo-prod
```

`--reinstall --no-cache` is not belt-and-braces. The version is static at
`0.1.0`, so `--force` alone can rebuild from uv's cache and reinstall the code
you just replaced, reporting success the whole way. Observed on 2026-09-07: the
pull landed, `--force` said `Installed 2 executables`, and the installed
`launcher.py` was still the previous revision.

Verify the receipt still names `repo-prod` afterwards:

```sh
cat ~/.local/share/uv/tools/llm-quota-router/uv-receipt.toml
```

If it names a working checkout, the live launcher is running someone's
uncommitted work. Reinstall from repo-prod.

The receipt only proves *where* it installed from, never *what* landed. Check a
symbol your change actually introduced:

```sh
grep -c '<a name your change added>' \
  ~/.local/share/uv/tools/llm-quota-router/lib/python3.12/site-packages/quota_router/launcher.py
```

## The two launchd jobs run the installed copy, not your checkout

`local.llm-quota-router.usage-poll` (every 120s) and
`local.llm-quota-router.credential-forensics` (every 60s) both invoke
`/Users/tonygwu/.local/bin/quotapick`. Editing your checkout does not change
what they run; a `uv tool install` does, immediately, with no restart. That is
the second reason the install belongs to repo-prod alone.

`ops/install-launchd.sh` rewrites the poller's plist and bakes in whatever
`quotapick` resolves on your PATH. Run it from a working checkout only if you
intend to change the live schedule.

The receipt and the symbol grep above both check the *file on disk*. Neither
shows that a poller has actually run the new code and survived it. The poller's
own log does, because it appends one `status --json` record every 120s:

```sh
grep -o '"generated_at": "[^"]*"' \
  ~/Library/Logs/llm-quota-router/usage-poll.log | tail -3
```

Take the two records either side of your install and compare a field your change
touches. On 2026-09-10, dropping Antigravity from the builtin accounts showed up
as nine account ids at `08:50:32Z` and seven at `08:52:39Z` -- proof the job
picked up the new code and exited cleanly, from the data rather than from an
mtime. `launchctl list | grep llm-quota-router` gives the last exit status, and
the run before yours is the one it may still be reporting.

The poller also runs `quotapick capabilities --refresh` after `status`, which
writes `capabilities.json` (the only input `pick --capability` reads). It logs
to `capability-refresh.log`, one JSON report per run, never to
`usage-poll.log`. That second command exists only in a plist written by an
`ops/install-launchd.sh` that has it, so after shipping a change to the script,
re-run it **from repo-prod**. Then check the refresh from its own data:

```sh
tail -1 ~/Library/Logs/llm-quota-router/capability-refresh.log \
  | jq -c '{at: .generated_at} + (.refresh | {attempted, succeeded, failed, failed_by_kind})'
quotapick capabilities          # REFRESHED column within the last 15 min
```

Testing capability code from a working checkout: point `XDG_STATE_HOME` at a
scratch directory, so your `capabilities --refresh` never rewrites the live
table that every `pick --capability` on this machine reads.

## Git protocol between checkouts

Other agents work the other checkouts at the same time, and git is the only
channel between them.

- **Sync before you touch.** `git fetch && git rebase origin/main` before you
  read or edit. Your HEAD is a hypothesis, not a fact.
- **Push every completed unit immediately.** Never end a turn with a
  publishable commit sitting unpushed. If something blocks the push, say what.
- **Stage by name.** `git add -A` and `git add .` are forbidden here. Untracked
  files you did not create are another agent's work in progress. A dirty tree
  full of foreign files is normal, not a mess to clean.
- **Divergence is a blocker.** More than 5 commits ahead, or anything behind
  for more than a day, gets surfaced to the operator rather than accumulating.

## Test-suite notes

The historical calibration reconstruction is read-only by default:
`PYTHONPATH=src ./.venv/bin/python ops/reconstruct-calibrations.py --help`.
The waiting-stretch reconstruction is also read-only by default:
`PYTHONPATH=src ./.venv/bin/python ops/reconstruct-forgone.py --help`.
See `docs/measurements/2026-10-20-forgone-capacity.md`. Under #32, the closed historical
stretches were adopted into live state on 2026-10-20, after operator approval and a
backup. The calibration ledger and v3 reset writer were adopted under #26 at
revision `7f87887`; that installation does not include the waiting-capacity writer,
whose installation belongs to #29.
The Gemini re-analysis uses saved responses only:
`./.venv/bin/python ops/reanalyze-gemini-oct12.py --help`.
See `docs/measurements/2026-10-19-calibration-reconstruction.md` for the required
inputs and the operator gate. Do not install the v3 waste writer until the operator
has approved the reconstructed ledger and it has been backed up and adopted in live
state. Develop against scratch XDG state; never rewrite `waste.jsonl`.

The offline #25 demand-allocation experiment is reproducible with
`PYTHONPATH=src ./.venv/bin/python ops/replay-demand-allocation.py`; it reads no operator
config or state and emits JSON. See `docs/measurements/2026-10-19-demand-allocation.md`.

`pytest` runs straight from a checkout (`pythonpath = ["src"]`), so no editable
install is needed. Tests marked `live` touch the real machine — real Keychain
entries, real `~/.claude*` config dirs, the real usage endpoint — and are
deselected by default. Run them with `-m live`, and only when you accept the
side effects.

**Never let anything in this repo redeem a Claude refresh token.** Anthropic
rotates refresh tokens, so redeeming one revokes the copy Claude Code holds and
silently logs the operator out of that account. The README section "The
single-writer rule" is the full account. Reading usage uses the existing
*access* token as a plain bearer, and mints nothing.


## Waiting-window pings

`PYTHONPATH=src ./.venv/bin/python -m quota_router.cli window-ping --dry-run`
reads status history without sending calls or changing state. See
`docs/window-pings.md`. The launchd installer defaults to dry-run; live activation
requires the operator's decision and a repo-prod reinstall with
`WINDOW_PING_MODE=execute`. Do not enable it before provider telemetry is qualified.
