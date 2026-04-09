#!/usr/bin/env bash
set -euo pipefail

# Purpose: Enforce readiness gating, then launch Otto's 12-month Outlook ingestion.
# Assumptions/invariants:
# - Otto should only ingest after test suite critical checks pass.
# - Ingestion is read/process/draft behavior only; no autonomous external sends.

SUITE_SCRIPT="/Users/louishyman/openclaw/scripts/test_otto_suite.sh"

if [[ ! -x "$SUITE_SCRIPT" ]]; then
  echo "Missing or non-executable suite script: $SUITE_SCRIPT" >&2
  exit 1
fi

echo "Running Otto readiness suite..."
if ! "$SUITE_SCRIPT"; then
  echo "Otto readiness suite failed. Fix failures before ingestion." >&2
  exit 1
fi

echo ""
echo "Launching Otto 12-month Outlook ingestion + triage bootstrap..."
openclaw agent --agent otto --message "Startup ingestion — read Outlook from the last 12 months and full calendar.

Build four outputs in otto-workspace/startup/:
1. calendar-structure.yaml — recurring events, committee meetings, teaching cadence, institutional patterns
2. contacts-raw.yaml — everyone from Outlook email/calendar with frequency and recency
3. institutional-map.md — committees, courses, center role, reporting relationships inferred from communications
4. triage-queue.yaml — current actionable threads with urgency classification

After ingestion, begin triage:
- Categorize last 14 days of Outlook messages as URGENT, PRIORITY, ROUTINE
- Flag any URGENT messages unanswered >48h for escalation to Polly via acp
- Draft reply suggestions for high-priority unanswered items (draft only, never send)

Write completion marker to otto-workspace/startup/DONE-12M and include a concise summary."

echo ""
echo "Otto ingestion request submitted."
