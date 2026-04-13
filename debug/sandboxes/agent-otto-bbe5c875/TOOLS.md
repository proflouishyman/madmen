# TOOLS.md — Otto

## Outlook Access via AppleScript

### Read unread emails
```bash
osascript <<'EOF'
tell application "Microsoft Outlook"
  set theMessages to every message of inbox whose read status is false
  set output to ""
  repeat with msg in theMessages
    set output to output & "FROM: " & (sender of msg as string) & "\n"
    set output to output & "SUBJECT: " & subject of msg & "\n"
    set output to output & "DATE: " & (time received of msg as string) & "\n"
    set output to output & "BODY: " & (content of msg) & "\n"
    set output to output & "---\n"
  end repeat
  return output
end tell
EOF
```

### Search by sender
```bash
osascript <<'EOF'
tell application "Microsoft Outlook"
  set theMessages to every message of inbox whose sender contains "SENDER_TERM"
  -- process as above
end tell
EOF
```

### Search by subject
```bash
osascript <<'EOF'
tell application "Microsoft Outlook"
  set theMessages to every message of inbox whose subject contains "SUBJECT_TERM"
  -- process as above
end tell
EOF
```

### Create draft reply (NEVER send)
```bash
osascript <<'EOF'
tell application "Microsoft Outlook"
  set theMessages to every message of inbox whose subject contains "ORIGINAL_SUBJECT"
  if (count of theMessages) > 0 then
    set originalMsg to item 1 of theMessages
    set replyMsg to reply originalMsg
    set content of replyMsg to "DRAFT_BODY_HERE"
    save replyMsg
    -- saved to Drafts, NOT sent
  end if
end tell
EOF
```

### Get today's calendar
```bash
osascript <<'EOF'
tell application "Microsoft Outlook"
  set today to current date
  set todayStart to today - (time of today)
  set todayEnd to todayStart + (23 * hours + 59 * minutes)
  set theEvents to every calendar event whose start time >= todayStart and start time <= todayEnd
  set output to ""
  repeat with evt in theEvents
    set output to output & (start time of evt as string) & " — " & subject of evt & "\n"
  end repeat
  return output
end tell
EOF
```

### Force Outlook sync before reading
```bash
osascript -e 'tell application "Microsoft Outlook" to sync'
sleep 8
```

### If Outlook is not running
```bash
open -a "Microsoft Outlook" && sleep 10
# Then run AppleScript
```

## CES Slack Access
```bash
# Read channel history
curl -s "https://slack.com/api/conversations.history" \
  -H "Authorization: Bearer $SLACK_TOKEN_OTTO" \
  -d "channel=CHANNEL_ID&limit=50"

# List channels to find IDs
curl -s "https://slack.com/api/conversations.list" \
  -H "Authorization: Bearer $SLACK_TOKEN_OTTO"
```

## Rules
- Outlook: read + draft only. Never call AppleScript send command.
- Slack: read + draft only. Never post autonomously.
- Escalate publisher emails to Polly immediately as URGENT.
- Weber queries me for committee emails — respond promptly with structured data.
