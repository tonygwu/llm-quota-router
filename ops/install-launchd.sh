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
# minted, nothing is written back to any credential store. Its only purpose is to keep
# history.jsonl dense, so burn-rate learning has a series to work from even for accounts
# that are only ever driven headlessly.
#
# Safe to not run at all. The router reads usage live on every invocation; this only
# improves the history it learns from.
#
#   ./ops/install-launchd.sh              install and start
#   ./ops/install-launchd.sh --uninstall  stop and remove
#   ./ops/install-launchd.sh --print      print the plist, install nothing
#
# Env: POLL_INTERVAL_S (default 1800)

set -uo pipefail

LABEL="local.llm-quota-router.usage-poll"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/llm-quota-router"
INTERVAL="${POLL_INTERVAL_S:-1800}"

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
  <key>ProgramArguments</key>
  <array>
    <string>${QUOTAPICK}</string>
    <string>status</string>
    <string>--json</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "$QUOTAPICK"):/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartInterval</key><integer>${INTERVAL}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${LOG_DIR}/usage-poll.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/usage-poll.log</string>
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
echo "  log: ${LOG_DIR}/usage-poll.log"
