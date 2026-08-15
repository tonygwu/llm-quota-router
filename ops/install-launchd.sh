#!/usr/bin/env bash
#
# Install (or remove) the periodic credential-refresh job.
#
# The cswap oracle stores COPIES of each account's credentials; the live config dirs
# keep refreshing their own OAuth tokens while those copies quietly expire. Once a copy
# expires the oracle reports `relogin_required` and the router -- correctly, but
# silently -- stops seeing that account. Left alone, the fleet narrows itself.
#
# This job re-captures every configured account on a cadence shorter than the observed
# expiry (~7 hours in practice), so the oracle never goes dark on its own.
#
#   ./ops/install-launchd.sh              install and start
#   ./ops/install-launchd.sh --uninstall  stop and remove
#   ./ops/install-launchd.sh --print      print the plist, install nothing
#
# Env: REFRESH_INTERVAL_S (default 1800)

set -uo pipefail

LABEL="local.llm-quota-router.credential-refresh"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/llm-quota-router"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REFRESH="$SCRIPT_DIR/refresh-oracle-credentials.sh"
INTERVAL="${REFRESH_INTERVAL_S:-1800}"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
    || launchctl unload "$PLIST" 2>/dev/null
  rm -f "$PLIST"
  echo "removed ${LABEL}"
  exit 0
fi

# Resolve cswap to an ABSOLUTE path and bake it in. launchd's minimal PATH excludes
# ~/.local/bin (where uv tool / pipx install), so a bare `cswap` would resolve
# interactively and then fail under the scheduler -- the single most common way a job
# like this "works when I test it" and never runs.
CSWAP="${CSWAP_BIN:-$(command -v cswap 2>/dev/null || true)}"
if [ -z "$CSWAP" ] || [ ! -x "$CSWAP" ]; then
  echo "error: cswap not found. Install it (uv tool install claude-swap) or set CSWAP_BIN." >&2
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
    <string>/bin/bash</string>
    <string>${REFRESH}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CSWAP_BIN</key><string>${CSWAP}</string>
    <key>CLAUDE_B_CONFIG_DIR</key><string>${CLAUDE_B_CONFIG_DIR:-$HOME/.claude-b}</string>
    <key>CLAUDE_C_CONFIG_DIR</key><string>${CLAUDE_C_CONFIG_DIR:-$HOME/.claude-c}</string>
    <key>PATH</key><string>$(dirname "$CSWAP"):/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartInterval</key><integer>${INTERVAL}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${LOG_DIR}/refresh.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/refresh.log</string>
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

# Replace any previous copy so re-running is idempotent.
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
echo "  every ${INTERVAL}s   cswap=${CSWAP}"
echo "  log: ${LOG_DIR}/refresh.log"
echo "  status: launchctl list | grep ${LABEL}"
