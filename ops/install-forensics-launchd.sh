#!/usr/bin/env bash
#
# Install (or remove) the credential-forensics sampler.
#
# WHY THIS EXISTS
# ---------------
# Accounts on this machine intermittently lose their stored credential and need an
# interactive `/login`. Two outages are on record in the usage-poll log: claude_d dark
# for 27 hours from 2026-08-18 06:15 PDT, and claude_b dark for 6.7 hours from
# 2026-08-20 12:22 PDT. The cause is NOT established. Three hypotheses were tested
# against the existing data and two died -- see src/quota_router/forensics.py for the
# full account.
#
# The usage poller cannot settle it. It runs every 900s, so both outages have a
# fifteen-minute blind spot around the transition, and that is exactly the window the
# answer lives in. This job samples every 60s and, on the sample where a credential is
# lost, writes down the preceding ten minutes: what the credential looked like and
# which processes held that directory at each step.
#
# WHAT IT COSTS
# -------------
# One Keychain read per account plus one `ps` per live session. Measured at ~21ms.
# There is NO network call and NO spawn, so this is close to free and, unlike the usage
# poller, it cannot itself perturb what it is measuring.
#
# WHAT IT WILL NEVER DO
# ---------------------
# It does not set QUOTA_ROUTER_REFRESH_AUTH, and it must never be given it. That
# variable authorizes the usage poller to spawn the vendor CLI so that IT renews a
# lapsed token. A measuring instrument that spawns processes into the directory it is
# measuring is not an instrument, it is a second suspect. This job is a pure reader.
#
#   ./ops/install-forensics-launchd.sh              install and start
#   ./ops/install-forensics-launchd.sh --uninstall  stop and remove
#   ./ops/install-forensics-launchd.sh --print      print the plist, install nothing
#
# Env: FORENSICS_INTERVAL_S (default 60 -- see below before raising it)

set -uo pipefail

LABEL="local.llm-quota-router.credential-forensics"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/llm-quota-router"
# 60s is the measurement resolution, and it is the whole point of the job. The blind
# spot around a transition equals this number, and 900s (the usage poller's interval)
# was already proven too coarse to see either recorded outage happen. Raising this
# trades away the only thing this job produces.
INTERVAL="${FORENSICS_INTERVAL_S:-60}"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
    || launchctl unload "$PLIST" 2>/dev/null
  rm -f "$PLIST"
  echo "removed ${LABEL}"
  exit 0
fi

# Absolute path, baked in. launchd's minimal PATH excludes ~/.local/bin (where uv tool
# and pipx install), so a bare `quotapick` resolves interactively and then never runs
# under the scheduler -- the single most common way a job like this "works when I test
# it" and is silently dead in production.
QUOTAPICK="${QUOTAPICK_BIN:-$(command -v quotapick 2>/dev/null || true)}"
if [ -z "$QUOTAPICK" ] || [ ! -x "$QUOTAPICK" ]; then
  echo "error: quotapick not found. Install it (uv tool install .) or set QUOTAPICK_BIN." >&2
  exit 1
fi

# Fail before installing rather than after. A plist pointing at a binary whose
# subcommand does not exist yet installs cleanly and then fails every 60 seconds into a
# log nobody is reading.
if ! "$QUOTAPICK" forensics --help >/dev/null 2>&1; then
  echo "error: ${QUOTAPICK} has no 'forensics' subcommand." >&2
  echo "       Reinstall the tool from this checkout: uv tool install --force ." >&2
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
    <string>forensics</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "$QUOTAPICK"):/usr/bin:/bin:/usr/sbin:/sbin</string>
    <!-- QUOTA_ROUTER_REFRESH_AUTH is deliberately absent. See the header. -->
  </dict>
  <key>StartInterval</key><integer>${INTERVAL}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${LOG_DIR}/credential-forensics.stdout.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/credential-forensics.stdout.log</string>
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
echo "  transitions: ${LOG_DIR}/credential-forensics.jsonl   <- read this after an outage"
echo "  job stdout:  ${LOG_DIR}/credential-forensics.stdout.log"
