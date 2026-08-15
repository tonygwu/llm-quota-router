#!/usr/bin/env bash
#
# Keep the cswap oracle's credential copies fresh.
#
# WHY THIS EXISTS
# ---------------
# `cswap add` stores a *copy* of an account's credentials. The live config dir keeps
# refreshing its own OAuth token; cswap's copy does not. After a few hours the copy
# expires and cswap reports `usageStatus: "relogin_required"` for that account.
#
# The router degrades correctly when that happens -- it warns and skips the account
# rather than guessing -- but a skipped account is an invisible one, so the router
# quietly narrows to a subset of the fleet on its own schedule. Observed in practice:
# two of three accounts went dark within ~7 hours of registration.
#
# Re-running `cswap add` against a config dir re-captures the live token in place. It
# is idempotent and non-destructive: it copies credentials INTO cswap's own registry
# and never changes which account is globally active, so concurrent spawns against
# different accounts stay isolated.
#
# THIS IS OPERATOR TOOLING, NOT LIBRARY CODE.
# The quota_router package is an oracle-only consumer of cswap: `cswap list --json` is
# the only invocation it may ever make, enforced by tests/test_portability.py. That
# guarantee is about the ROUTER never mutating account state during a routing decision.
# Refreshing the oracle's own credential cache is a separate, explicitly-invoked
# maintenance action, which is why it lives here in ops/ and not in src/.
#
# Usage:
#   refresh-oracle-credentials.sh            # refresh every configured account
#   refresh-oracle-credentials.sh --check    # report status only, mutate nothing
#
# Env:
#   CSWAP_BIN             absolute path to cswap (default: resolved from PATH)
#   CLAUDE_B_CONFIG_DIR   default ~/.claude-b
#   CLAUDE_C_CONFIG_DIR   default ~/.claude-c
#   REFRESH_TIMEOUT_S     per-account cap, default 60

# Deliberately NOT `set -e`: one unusable account must never abort the others.
set -uo pipefail

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

CSWAP="${CSWAP_BIN:-$(command -v cswap 2>/dev/null || true)}"
TIMEOUT_S="${REFRESH_TIMEOUT_S:-60}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [ -z "$CSWAP" ] || [ ! -x "$CSWAP" ]; then
  # Not an error: a machine that does not use the cswap oracle simply has nothing to
  # refresh. Exit 0 so a scheduled job does not alarm on an intentional setup.
  log "cswap not found (CSWAP_BIN unset and not on PATH); nothing to refresh"
  exit 0
fi

# Portable timeout. macOS ships no coreutils `timeout`, so a hung Keychain prompt
# under a TTY-less launchd session would otherwise wedge this job forever.
run_capped() {
  local secs="$1"; shift
  "$@" &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$secs" ]; then
      kill -TERM "$pid" 2>/dev/null
      sleep 2
      kill -KILL "$pid" 2>/dev/null
      return 124
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid"
}

# slot|config dir. An empty dir means the CLI default (~/.claude), whose config lives
# at ~/.claude.json rather than inside the directory -- an asymmetry in Claude Code's
# own layout that the extra slots do not share.
ACCOUNTS=(
  "claude|"
  "claude_b|${CLAUDE_B_CONFIG_DIR:-$HOME/.claude-b}"
  "claude_c|${CLAUDE_C_CONFIG_DIR:-$HOME/.claude-c}"
)

if [ "$CHECK_ONLY" = "1" ]; then
  log "checking oracle credential status (read-only)"
  run_capped "$TIMEOUT_S" "$CSWAP" list --json 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("  (could not parse cswap output)")
    sys.exit(0)
stale = 0
for acct in data.get("accounts", []):
    status = acct.get("usageStatus")
    email = acct.get("email") or "?"
    flag = "ok " if status == "ok" else "STALE"
    if status != "ok":
        stale += 1
    print("  %s %-32s usageStatus=%s" % (flag, email, status))
print("  %d account(s) need a refresh" % stale if stale else "  all accounts fresh")
'
  exit 0
fi

refreshed=0
skipped=0
failed=0

for entry in "${ACCOUNTS[@]}"; do
  slot="${entry%%|*}"
  dir="${entry#*|}"

  if [ -n "$dir" ] && [ ! -d "$dir" ]; then
    log "skip $slot: config dir not present ($dir)"
    skipped=$((skipped + 1))
    continue
  fi

  if [ -n "$dir" ]; then
    out="$(CLAUDE_CONFIG_DIR="$dir" run_capped "$TIMEOUT_S" "$CSWAP" add 2>&1)"
  else
    out="$(run_capped "$TIMEOUT_S" "$CSWAP" add 2>&1)"
  fi
  rc=$?

  if [ "$rc" -eq 124 ]; then
    log "FAIL $slot: timed out after ${TIMEOUT_S}s (a Keychain prompt cannot be answered under launchd)"
    failed=$((failed + 1))
  elif [ "$rc" -ne 0 ]; then
    # Most commonly "No active Claude account found" -- that dir is logged out, which
    # is the operator's business, not a job failure.
    log "skip $slot: $(printf '%s' "$out" | tail -1)"
    skipped=$((skipped + 1))
  else
    log "ok   $slot: $(printf '%s' "$out" | tail -1)"
    refreshed=$((refreshed + 1))
  fi
done

log "refreshed=$refreshed skipped=$skipped failed=$failed"

# Only a hard failure (timeout/crash) is worth a non-zero exit. A logged-out account is
# an expected steady state, not something to alarm on every 30 minutes.
[ "$failed" -eq 0 ] || exit 1
exit 0
