#!/usr/bin/env bash
#
# Install (or remove) the periodic usage-history poller.
#
# WHAT THIS IS NOW
# ----------------
# This job used to re-capture OAuth credentials for an external oracle every 30 minutes.
# That oracle was removed: reading usage through it redeemed the account's refresh token,
# and because Anthropic rotates refresh tokens, doing so revoked the copy Claude Code
# held and logged the operator out. See README "The single-writer rule".
#
# What remains is strictly a READER. It runs `quotapick status`, which reads each
# account's existing access token and calls the vendor usage endpoint with it. Nothing is
# minted, nothing is written back to any credential store. It does two things:
#
#   1. keeps history.jsonl dense, so burn-rate learning has a series to work from even
#      for accounts that are only ever driven headlessly; and
#   2. writes waste.jsonl -- one row per window reset, which is the measurement the whole
#      project is judged on (README, "Measuring the thing it exists for").
#
# It also keeps the per-account usage cache warm. The router serves a cached payload
# for DEFAULT_USAGE_TTL_S (120s) before reading live again, and every `cl` launch
# that finds the cache cold pays for four endpoint round trips inside a three-second
# budget. Not running this costs no correctness -- the router still reads live -- but
# it does cost the measurement: a reset seen by nothing leaves an `observed: false`
# row and its remainder is unrecoverable, so the longer this job is down the more of
# the series is holes. That is why the writer lives here rather than in a command
# someone has to remember to run.
#
#   ./ops/install-launchd.sh              install and start
#   ./ops/install-launchd.sh --uninstall  stop and remove
#   ./ops/install-launchd.sh --print      print the plist, install nothing
#
# Env: POLL_INTERVAL_S    (default 120 -- see below; do not raise it casually)
#      POLL_LOG_MAX_BYTES (default 20000000 -- the log rotates itself past this)

set -uo pipefail

LABEL="local.llm-quota-router.usage-poll"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/llm-quota-router"
# 120s is not a taste; two Python constants bound it, and tests/test_k_calibration.py
# asserts this default against both, because a shell default and a Python constant
# cannot share a definition:
#
#   * `quota_router.providers.claude_oauth.DEFAULT_USAGE_TTL_S` (120s) is how long a
#     fetched payload is reused. Polling slower than that leaves the cache cold for
#     the difference -- at the old 900s it was cold 87% of the time, so nearly every
#     `cl` launch read live, and on 2026-09-06 four such reads blew the launcher's
#     three-second deadline.
#   * `quota_router.history.ADOPTION_MAX_STEP_S` (900s) refuses any calibration
#     series whose TYPICAL step exceeds it, and this job is what produces that
#     series -- so polling slower than the gate means no k estimate is ever
#     adoptable, with no symptom beyond calibrate quietly never saying ADOPT. The
#     waste series has the same exposure: a reset can only be located to within one
#     interval.
#
# Cost of 120s: one endpoint read per account every two minutes. The endpoint
# throttled at roughly seventeen reads on one token inside six minutes, so this
# sits well under it, and the adapter serves its cache on a 429 anyway.
INTERVAL="${POLL_INTERVAL_S:-120}"

# launchd appends every run's full JSON to one file and never rotates it: ~19 KB a
# run, 25 MB after six weeks at 900s, ~13 MB a day at 120s. The job trims itself
# (see the ProgramArguments below): past this many bytes the log is moved aside as
# `.1`, one generation kept, so at most twice this is ever on disk.
LOG_MAX_BYTES="${POLL_LOG_MAX_BYTES:-20000000}"
LOG_FILE="${LOG_DIR}/usage-poll.log"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
    || launchctl unload "$PLIST" 2>/dev/null
  rm -f "$PLIST"
  echo "removed ${LABEL}"
  exit 0
fi

# Resolve quotapick to an ABSOLUTE path and bake it in. launchd's minimal PATH excludes
# ~/.local/bin (where uv tool / pipx install), so a bare `quotapick` would resolve
# interactively and then fail under the scheduler -- the single most common way a job
# like this "works when I test it" and never runs.
QUOTAPICK="${QUOTAPICK_BIN:-$(command -v quotapick 2>/dev/null || true)}"
if [ -z "$QUOTAPICK" ] || [ ! -x "$QUOTAPICK" ]; then
  echo "error: quotapick not found. Install it (uv tool install .) or set QUOTAPICK_BIN." >&2
  exit 1
fi

read -r -d '' PLIST_XML <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <!-- \$0 is quotapick, \$1 the log, \$2 the cap. The run's own output lands in the
       log first; the rotation happens after it, from inside the same process, which is
       safe because launchd reopens StandardOutPath fresh for every run. -->
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>"\$0" status --json; rc=\$?; size=\$(stat -f %z "\$1" 2>/dev/null || echo 0); if [ "\$size" -gt "\$2" ]; then mv -f "\$1" "\$1.1"; fi; exit \$rc</string>
    <string>${QUOTAPICK}</string>
    <string>${LOG_FILE}</string>
    <string>${LOG_MAX_BYTES}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "$QUOTAPICK"):/usr/bin:/bin:/usr/sbin:/sbin</string>
    <!-- Let the poller wake an account whose access token has lapsed, by spawning
         the vendor CLI so that IT renews (this tool never redeems a token itself).
         Set here and nowhere else: a refresh spawn costs seconds, and the
         interactive launcher caps its whole decision at three, so it is opt-in and
         only the job with nobody waiting on it opts in. -->
    <key>QUOTA_ROUTER_REFRESH_AUTH</key><string>1</string>
  </dict>
  <key>StartInterval</key><integer>${INTERVAL}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${LOG_FILE}</string>
  <key>StandardErrorPath</key><string>${LOG_FILE}</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
XML

if [ "${1:-}" = "--print" ]; then
  printf '%s\n' "$PLIST_XML"
  exit 0
fi

mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
printf '%s\n' "$PLIST_XML" > "$PLIST"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
  :
else
  launchctl load "$PLIST" 2>/dev/null || {
    echo "error: could not load ${PLIST}" >&2
    exit 1
  }
fi

echo "installed ${LABEL}"
echo "  every ${INTERVAL}s   quotapick=${QUOTAPICK}"
echo "  log: ${LOG_FILE}  (rotates itself past ${LOG_MAX_BYTES} bytes)"
