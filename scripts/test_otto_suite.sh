#!/usr/bin/env bash
set -euo pipefail

# Purpose: Run a focused operational test suite for Otto's Outlook processing path.
# Assumptions/invariants:
# - Otto's canonical mail path is local Microsoft Outlook via AppleScript.
# - This suite should detect misrouting (e.g., Gmail responses from Otto).
# - A zero-message inbox is possible; it is warning-level unless Outlook account is missing.

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
RUN_AGENT_TURN=1

print_usage() {
  # Function purpose: Explain how to run the Otto suite and optional modes.
  cat <<'EOF'
Usage:
  scripts/test_otto_suite.sh [options]

Options:
  --quick       Skip live Otto agent-turn validation
  -h, --help    Show this help text

Exit codes:
  0 = all critical checks passed (warnings may exist)
  1 = one or more critical checks failed
EOF
}

pass() {
  # Function purpose: Record and display a passing check.
  local name="$1"
  local details="$2"
  echo "PASS: $name - $details"
  PASS_COUNT=$((PASS_COUNT + 1))
}

warn() {
  # Function purpose: Record and display a warning-level issue.
  local name="$1"
  local details="$2"
  echo "WARN: $name - $details"
  WARN_COUNT=$((WARN_COUNT + 1))
}

fail() {
  # Function purpose: Record and display a failure-level issue.
  local name="$1"
  local details="$2"
  echo "FAIL: $name - $details"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

require_command() {
  # Function purpose: Ensure required binaries are available before running checks.
  local command_name="$1"
  if command -v "$command_name" >/dev/null 2>&1; then
    pass "Command available" "$command_name"
  else
    fail "Command available" "$command_name not found"
  fi
}

check_otto_identity_contract() {
  # Function purpose: Ensure Otto's local identity files enforce Outlook ownership.
  local soul_path="$HOME/.openclaw/workspaces/otto-workspace/SOUL.md"
  local tools_path="$HOME/.openclaw/workspaces/otto-workspace/TOOLS.md"
  local soul_text tools_text

  if [[ ! -f "$soul_path" ]]; then
    fail "Otto identity contract" "Missing SOUL.md at $soul_path"
    return
  fi
  if [[ ! -f "$tools_path" ]]; then
    fail "Otto identity contract" "Missing TOOLS.md at $tools_path"
    return
  fi

  soul_text="$(cat "$soul_path")"
  tools_text="$(cat "$tools_path")"

  # Non-obvious logic: Generic starter SOUL files indicate agent role drift risk.
  if printf '%s\n' "$soul_text" | grep -Fq '# SOUL.md - Who You Are'; then
    fail "Otto identity contract" "SOUL.md is generic template; Otto role boundaries are not configured"
    return
  fi

  if ! printf '%s\n' "$soul_text" | grep -Eiq '\boutlook\b'; then
    fail "Otto identity contract" "SOUL.md does not mention Outlook ownership"
    return
  fi

  if ! printf '%s\n' "$tools_text" | grep -Eiq 'osascript|microsoft outlook'; then
    warn "Otto identity contract" "TOOLS.md lacks explicit Outlook AppleScript commands"
  else
    pass "Otto identity contract" "SOUL/TOOLS include Outlook-specific contract details"
  fi
}

check_otto_registered() {
  # Function purpose: Verify Otto is present in the OpenClaw agent registry.
  local output
  if ! output="$(openclaw agents list 2>&1)"; then
    fail "Otto registered" "openclaw agents list failed: $output"
    return
  fi

  if printf '%s\n' "$output" | grep -Eq '^- otto(\s|$)'; then
    pass "Otto registered" "agent 'otto' exists"
  else
    fail "Otto registered" "agent 'otto' not found in registry"
  fi
}

check_otto_channel_probe() {
  # Function purpose: Confirm Otto's channel stack is reachable from gateway probes.
  local output
  if ! output="$(openclaw channels status --probe 2>&1)"; then
    fail "Otto channel probe" "openclaw channels status --probe failed: $output"
    return
  fi

  if printf '%s\n' "$output" | grep -Eq 'Telegram otto: .*works'; then
    pass "Otto channel probe" "Telegram otto channel is healthy"
  elif printf '%s\n' "$output" | grep -Eq 'Telegram otto:'; then
    warn "Otto channel probe" "Otto channel present but not healthy: $(printf '%s\n' "$output" | grep -E 'Telegram otto:' | head -n 1)"
  else
    fail "Otto channel probe" "No Telegram otto entry in probe output"
  fi
}

check_outlook_runtime() {
  # Function purpose: Validate local Outlook is reachable via AppleScript and inspect inbox metadata.
  local inbox_name
  if ! inbox_name="$(osascript -e 'tell application "Microsoft Outlook" to get name of inbox' 2>&1)"; then
    fail "Outlook runtime" "AppleScript access failed: $inbox_name"
    return
  fi
  pass "Outlook runtime" "Inbox reachable as '$inbox_name'"

  local inbox_count
  if ! inbox_count="$(osascript -e 'tell application "Microsoft Outlook" to get count of messages of inbox' 2>&1)"; then
    fail "Outlook inbox count" "Could not read inbox count: $inbox_count"
    return
  fi

  if [[ "$inbox_count" =~ ^[0-9]+$ ]]; then
    if [[ "$inbox_count" -eq 0 ]]; then
      warn "Outlook inbox count" "Inbox is readable but currently reports 0 messages"
    else
      pass "Outlook inbox count" "Inbox reports $inbox_count messages"
    fi
  else
    fail "Outlook inbox count" "Non-numeric count returned: $inbox_count"
    return
  fi

  local latest_time
  latest_time="$(osascript -e 'tell application "Microsoft Outlook" to get time received of first message of inbox' 2>&1 || true)"
  if [[ "$latest_time" == *"error:"* ]]; then
    warn "Outlook last message timestamp" "Could not read latest message timestamp (may be unsynced)"
  else
    pass "Outlook last message timestamp" "$latest_time"
  fi
}

check_otto_slack_access() {
  # Function purpose: Validate Otto's CES Slack token path when token is configured.
  local token="${SLACK_TOKEN_OTTO:-}"
  if [[ -z "$token" ]]; then
    warn "Otto Slack access" "SLACK_TOKEN_OTTO not set in environment; Slack check skipped"
    return
  fi

  local response
  if ! response="$(curl -s -H "Authorization: Bearer $token" https://slack.com/api/auth.test 2>&1)"; then
    fail "Otto Slack access" "Slack auth.test request failed: $response"
    return
  fi

  if printf '%s\n' "$response" | grep -q '"ok":true'; then
    pass "Otto Slack access" "Slack token is valid"
  else
    fail "Otto Slack access" "Slack auth.test did not return ok=true: $response"
  fi
}

check_otto_crons() {
  # Function purpose: Detect whether Otto automation jobs are scheduled.
  local json
  if ! json="$(openclaw cron list --json 2>&1)"; then
    fail "Otto cron coverage" "openclaw cron list --json failed: $json"
    return
  fi

  local json_clean
  if ! json_clean="$(
    python3 -c 'import sys; text=sys.stdin.read(); i=text.find("{"); 
if i < 0: raise SystemExit(1)
print(text[i:])' <<<"$json" 2>/dev/null
  )"; then
    fail "Otto cron coverage" "Could not locate JSON payload in cron output"
    return
  fi

  local otto_count
  if ! otto_count="$(python3 -c 'import json,sys; data=json.load(sys.stdin); print(sum(1 for j in data.get("jobs", []) if j.get("agentId")=="otto"))' <<<"$json_clean" 2>/dev/null)"; then
    fail "Otto cron coverage" "Could not parse cron JSON"
    return
  fi

  if [[ "$otto_count" =~ ^[0-9]+$ ]] && [[ "$otto_count" -gt 0 ]]; then
    pass "Otto cron coverage" "$otto_count Otto cron job(s) configured"
  else
    warn "Otto cron coverage" "No Otto cron jobs found; Otto will only process mail when manually invoked"
  fi
}

check_otto_agent_turn() {
  # Function purpose: Verify Otto can execute Outlook-only processing and avoid Gmail misrouting.
  local prompt output
  prompt="Otto test mode. Use Outlook via AppleScript only. Do not use Gmail.
Return exactly four lines:
STATUS: <ok|error>
INBOX_COUNT: <number or unknown>
TOP_SUBJECTS: <semicolon-separated list or none>
NOTES: <short note>"

  if ! output="$(
    python3 - "$prompt" <<'PY'
import subprocess
import sys

prompt = sys.argv[1]
try:
    cp = subprocess.run(
        ["openclaw", "agent", "--agent", "otto", "--timeout", "45", "--message", prompt],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    sys.stdout.write((cp.stdout or "") + (cp.stderr or ""))
    raise SystemExit(cp.returncode)
except subprocess.TimeoutExpired:
    sys.stdout.write("TIMEOUT: openclaw agent --agent otto exceeded 90s")
    raise SystemExit(124)
PY
  )"; then
    if printf '%s\n' "$output" | grep -Fq "TIMEOUT:"; then
      fail "Otto live turn" "$output"
    else
      fail "Otto live turn" "openclaw agent invocation failed: $output"
    fi
    return
  fi

  if printf '%s\n' "$output" | grep -Eiq '\bgmail\b|@gmail\.com|google account'; then
    fail "Otto live turn" "Output appears Gmail-routed, not Outlook-specific"
    return
  fi

  if printf '%s\n' "$output" | grep -Eiq 'rate limit|usage limit|status[[:space:]]*429|try again in'; then
    fail "Otto live turn" "Model provider rate-limited; retry after cooldown: $output"
    return
  fi

  if printf '%s\n' "$output" | grep -Eq '^STATUS:[[:space:]]*error'; then
    fail "Otto live turn" "Otto reported STATUS:error: $output"
    return
  fi

  if printf '%s\n' "$output" | grep -Eiq 'no exchange account|not configured|0 messages are visible because.*exchange'; then
    fail "Otto live turn" "Outlook account appears missing/unconfigured: $output"
    return
  fi

  if printf '%s\n' "$output" | grep -Eiq 'exchange accounts[[:space:]]*=[[:space:]]*none'; then
    fail "Otto live turn" "No Exchange account detected in Outlook context: $output"
    return
  fi

  if printf '%s\n' "$output" | grep -Eq '^STATUS:[[:space:]]*ok' && \
     printf '%s\n' "$output" | grep -Eq '^INBOX_COUNT:[[:space:]]*([0-9]+|unknown)'; then
    pass "Otto live turn" "Outlook-only response format validated"
  else
    warn "Otto live turn" "Output returned but format was non-standard: $output"
  fi
}

for arg in "$@"; do
  case "$arg" in
    --quick)
      RUN_AGENT_TURN=0
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      print_usage >&2
      exit 2
      ;;
  esac
done

echo "Otto Suite starting..."
require_command "openclaw"
require_command "osascript"

check_otto_registered
check_otto_identity_contract
check_otto_channel_probe
check_outlook_runtime
check_otto_slack_access
check_otto_crons

if [[ "$RUN_AGENT_TURN" -eq 1 ]]; then
  check_otto_agent_turn
else
  warn "Otto live turn" "Skipped due to --quick"
fi

echo ""
echo "Otto Suite summary:"
echo "  PASS: $PASS_COUNT"
echo "  WARN: $WARN_COUNT"
echo "  FAIL: $FAIL_COUNT"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
