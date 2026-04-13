#!/usr/bin/env bash
# Purpose: Sweep up to 10 most recent Outlook inbox messages from the last 48h,
# classify each as URGENT/PRIORITY/ROUTINE, and append one timestamped YAML block
# to otto-workspace/state/sweep-log.yaml for Polly ingestion.
# Safe: read-only, no outbound email, no network calls.
set -euo pipefail

WORKSPACE="${HOME}/.openclaw/workspaces/otto-workspace"
LOG="${WORKSPACE}/state/sweep-log.yaml"
mkdir -p "${WORKSPACE}/state"
NOW_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
TODAY="$(date '+%Y-%m-%d')"

# ── Run the AppleScript sweep ──────────────────────────────────────────────────
SWEEP="$(osascript <<'APPLESCRIPT' 2>/dev/null
tell application "Microsoft Outlook"
  set cutoff to (current date) - (48 * 60 * 60)
  set msgs to every message of inbox whose time received >= cutoff
  -- Sort by most recent first; cap at 10
  set msgCount to count of msgs
  if msgCount = 0 then
    return "EMPTY"
  end if
  if msgCount > 10 then set msgCount to 10

  set output to ""
  repeat with i from 1 to msgCount
    set m to item i of msgs
    set subj to subject of m
    set recv to time received of m as string
    set sndr to ""
    try
      set sndr to sender of m as string
    end try

    -- Classification by subject keywords
    set cls to "ROUTINE"
    if subj contains "URGENT" or subj contains "ACTION REQUIRED" or subj contains "IMMEDIATE" then
      set cls to "URGENT"
    else if subj contains "PRIORITY" or subj contains "IMPORTANT" or subj contains "DEADLINE" then
      set cls to "PRIORITY"
    end if

    set output to output & "- subject: " & quoted form of subj & "\n"
    set output to output & "  sender: " & quoted form of sndr & "\n"
    set output to output & "  received: " & quoted form of recv & "\n"
    set output to output & "  class: " & cls & "\n"
    set output to output & "\n"
  end repeat
  return output
end tell
APPLESCRIPT
)"

# ── Handle Outlook unavailable ─────────────────────────────────────────────────
if [[ $? -ne 0 ]] || [[ -z "${SWEEP}" ]] || [[ "${SWEEP}" == "EMPTY" ]]; then
  STATUS="no_data"
  SWEPT=0
  URGENT=0
  # Still write a minimal log entry so polly_ingest knows we ran
  cat >> "${LOG}" <<YAML

# Sweep: ${NOW_UTC}
sweep_run:
  timestamp: "${NOW_UTC}"
  status: "${STATUS}"
  messages_swept: 0
  urgent: 0
YAML
  echo '{"status":"'"${STATUS}"'","source":"outlook","date":"'"${TODAY}"'","swept":0,"urgent":0}'
  exit 0
fi

# ── Count urgent items ─────────────────────────────────────────────────────────
SWEPT="$(echo "${SWEEP}" | grep -c '^- subject:' || true)"
URGENT="$(echo "${SWEEP}" | grep -c 'class: URGENT' || true)"

# ── Append to sweep log ────────────────────────────────────────────────────────
cat >> "${LOG}" <<YAML

# Sweep: ${NOW_UTC}
sweep_run:
  timestamp: "${NOW_UTC}"
  status: "ok"
  messages_swept: ${SWEPT}
  urgent: ${URGENT}
messages:
${SWEEP}
YAML

echo '{"status":"ok","source":"outlook","date":"'"${TODAY}"'","swept":'"${SWEPT}"',"urgent":'"${URGENT}"'}'
