#!/usr/bin/env bash
# Purpose: Read today + tomorrow's Outlook calendar events via AppleScript and write
# to otto-workspace/state/calendar-today.yaml for Polly ingestion.
# Safe: read-only, no network calls, no email sends.
set -euo pipefail

WORKSPACE="${HOME}/.openclaw/workspaces/otto-workspace"
OUTPUT="${WORKSPACE}/state/calendar-today.yaml"
mkdir -p "${WORKSPACE}/state"

# Get today and tomorrow as date strings for the output header
TODAY="$(date '+%Y-%m-%d')"
TOMORROW="$(date -v+1d '+%Y-%m-%d' 2>/dev/null || date -d 'tomorrow' '+%Y-%m-%d')"
NOW_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# Read today + tomorrow's Outlook calendar events via AppleScript
EVENTS="$(osascript <<'APPLESCRIPT' 2>/dev/null
tell application "Microsoft Outlook"
  set today to current date
  -- Start of today (midnight)
  set dayStart to today - (time of today)
  -- End of tomorrow (midnight + 2 days - 1 second)
  set dayEnd to dayStart + (2 * days) - 1

  set theEvents to every calendar event whose start time >= dayStart and start time <= dayEnd
  set output to ""
  repeat with evt in theEvents
    set evtStart to start time of evt as string
    set evtEnd to end time of evt as string
    set evtSubject to subject of evt
    -- location may error on some events; guard it
    set evtLocation to ""
    try
      set evtLocation to location of evt
    end try
    set evtOrganizer to ""
    try
      set evtOrganizer to organizer of evt as string
    end try
    -- Attendees count (not names, to keep it compact)
    set attendeeCount to count of attendees of evt
    set output to output & "- start: \"" & evtStart & "\"\n"
    set output to output & "  end: \"" & evtEnd & "\"\n"
    set output to output & "  subject: " & quoted form of evtSubject & "\n"
    if evtLocation is not "" then
      set output to output & "  location: " & quoted form of evtLocation & "\n"
    end if
    if evtOrganizer is not "" then
      set output to output & "  organizer: " & quoted form of evtOrganizer & "\n"
    end if
    set output to output & "  attendees: " & attendeeCount & "\n"
    set output to output & "\n"
  end repeat
  return output
end tell
APPLESCRIPT
)"

# Check if Outlook is available
if [[ $? -ne 0 ]] || [[ -z "${EVENTS}" ]]; then
  cat > "${OUTPUT}" <<YAML
# Outlook Calendar — ${TODAY}
# Generated: ${NOW_UTC}
# Status: no_data (Outlook unavailable or calendar empty)
source: outlook
generated_at: "${NOW_UTC}"
date_range:
  from: "${TODAY}"
  to: "${TOMORROW}"
events: []
YAML
  echo '{"status":"no_data","source":"outlook","date":"'"${TODAY}"'","events":0}'
  exit 0
fi

# Count events
EVENT_COUNT="$(echo "${EVENTS}" | grep -c '^\- start:' || true)"

# Write YAML output
cat > "${OUTPUT}" <<YAML
# Outlook Calendar — ${TODAY} through ${TOMORROW}
# Generated: ${NOW_UTC}
source: outlook
generated_at: "${NOW_UTC}"
date_range:
  from: "${TODAY}"
  to: "${TOMORROW}"
events:
${EVENTS}
YAML

echo '{"status":"ok","source":"outlook","date":"'"${TODAY}"'","events":'"${EVENT_COUNT}"'}'
