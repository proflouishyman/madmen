#!/usr/bin/env bash
set -euo pipefail

# Purpose: Minimal Outlook Web access check using Playwright CLI.
# Assumptions/invariants:
# - npx is installed (required by the Playwright wrapper).
# - User completes interactive sign-in + MFA in the opened browser window.
# - Outlook row accessibility labels are stable enough to sample inbox items.

EXPECTED_DOMAIN="jh.edu"
SESSION="jhu-outlook-check"
KEEP_OPEN="0"

print_usage() {
  # Function purpose: Show invocation options and expected behavior.
  cat <<'EOF'
Usage:
  scripts/test_jhu_email_access_playwright.sh [options]

Options:
  --expected-domain <domain>  Expected email domain hint (default: jh.edu)
  --session <name>            Playwright CLI session name (default: jhu-outlook-check)
  --keep-open                 Do not close browser tab at the end
  -h, --help                  Show this help text

Behavior:
  1) Opens Outlook Web in a headed browser.
  2) You manually sign in with JHU credentials and complete MFA.
  3) Script samples mailbox UI state and top message row labels.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --expected-domain)
      EXPECTED_DOMAIN="${2:-}"
      shift 2
      ;;
    --session)
      SESSION="${2:-}"
      shift 2
      ;;
    --keep-open)
      KEEP_OPEN="1"
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      print_usage >&2
      exit 2
      ;;
  esac
done

if ! command -v npx >/dev/null 2>&1; then
  echo "npx is required. Install Node.js/npm first." >&2
  exit 1
fi

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
PWCLI="$CODEX_HOME/skills/playwright/scripts/playwright_cli.sh"
if [[ ! -x "$PWCLI" ]]; then
  echo "Playwright wrapper not found at: $PWCLI" >&2
  echo "Install/enable the playwright skill, then retry." >&2
  exit 1
fi

mkdir -p output/playwright

echo "Opening Outlook Web..."
"$PWCLI" --session "$SESSION" open "https://outlook.office.com/mail/" --headed >/dev/null
"$PWCLI" --session "$SESSION" snapshot >/dev/null || true

echo
echo "Complete sign-in and MFA in the browser window."
read -r -p "Press Enter here once your inbox is visible... "

# Non-obvious logic: we use page URL + lightweight DOM checks as a pragmatic
# access signal, because UI element IDs are dynamic and tenant-branded.
CURRENT_URL="$("$PWCLI" --session "$SESSION" eval "window.location.href" | tr -d '\r')"
TITLE="$("$PWCLI" --session "$SESSION" eval "document.title" | tr -d '\r')"

MAILBOX_HINT="$("$PWCLI" --session "$SESSION" eval "(() => { const body = (document.body && document.body.innerText) || ''; const re = /[A-Z0-9._%+-]+@${EXPECTED_DOMAIN//./\\.}/i; const m = body.match(re); return m ? m[0] : ''; })()" | tr -d '\r')"

ROW_LABELS_JSON="$("$PWCLI" --session "$SESSION" eval "(() => { const rows = Array.from(document.querySelectorAll('[role=\\\"option\\\"][aria-label]')); const labels = rows.map(r => (r.getAttribute('aria-label') || '').trim()).filter(Boolean); return JSON.stringify(labels.slice(0, 5)); })()" | tr -d '\r')"

echo
echo "Observed state:"
echo "  URL:   $CURRENT_URL"
echo "  Title: $TITLE"
if [[ -n "$MAILBOX_HINT" ]]; then
  echo "  Mail hint: $MAILBOX_HINT"
fi

echo "  Top inbox rows (aria-label sample): $ROW_LABELS_JSON"

if [[ "$CURRENT_URL" == *"outlook.office.com"* || "$CURRENT_URL" == *"office.com/mail"* ]]; then
  echo
echo "Outlook UI check: PASS"
  EXIT_CODE=0
else
  echo
echo "Outlook UI check: WARN (URL does not look like mailbox view)" >&2
  EXIT_CODE=1
fi

if [[ "$KEEP_OPEN" != "1" ]]; then
  "$PWCLI" --session "$SESSION" close >/dev/null || true
fi

exit "$EXIT_CODE"
