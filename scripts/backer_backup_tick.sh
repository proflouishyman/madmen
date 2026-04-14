#!/usr/bin/env bash
# backer_backup_tick.sh — nightly backup of polly.db and runs.sqlite
#
# Purpose: Creates dated backup copies of the two critical databases.
# Keeps 7 days of polly.db backups and 3 days of runs.sqlite backups.
# Safe to run while gateway is live — uses sqlite3 .backup to get a
# consistent snapshot even while the database is being written.
#
# Restore procedure (polly.db):
#   cp ~/.openclaw/backups/polly.db.YYYY-MM-DD ~/.openclaw/workspaces/polly-workspace/polly.db
#   (restart gateway after restoring)
#
# Restore procedure (runs.sqlite):
#   Stop gateway: launchctl stop gui/$(id -u)/ai.openclaw.gateway
#   cp ~/.openclaw/backups/runs.sqlite.YYYY-MM-DD ~/.openclaw/tasks/runs.sqlite
#   Restart: launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway

set -euo pipefail

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
POLLY_DB="${OPENCLAW_HOME}/workspaces/polly-workspace/polly.db"
RUNS_DB="${OPENCLAW_HOME}/tasks/runs.sqlite"
BACKUP_DIR="${OPENCLAW_HOME}/backups"
LOG_DIR="${OPENCLAW_HOME}/workspaces/backer-workspace/logs"
TODAY="$(date +%Y-%m-%d)"
KEEP_POLLY_DAYS=7
KEEP_RUNS_DAYS=3

mkdir -p "${BACKUP_DIR}" "${LOG_DIR}"

timestamp_utc() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

append_log() {
  printf '%s %s\n' "$(timestamp_utc)" "$1" >> "${LOG_DIR}/${TODAY}.log"
}

errors=0

# ── polly.db backup ───────────────────────────────────────────────────────────
if [[ -f "${POLLY_DB}" ]]; then
  POLLY_BACKUP="${BACKUP_DIR}/polly.db.${TODAY}"
  # sqlite3 .backup is safe for live databases (uses backup API, not cp)
  if sqlite3 "${POLLY_DB}" ".backup '${POLLY_BACKUP}'"; then
    size="$(du -sh "${POLLY_BACKUP}" | cut -f1)"
    append_log "{\"event\":\"backup_ok\",\"file\":\"polly.db\",\"dest\":\"${POLLY_BACKUP}\",\"size\":\"${size}\"}"
    # Prune backups older than KEEP_POLLY_DAYS
    find "${BACKUP_DIR}" -name "polly.db.*" -mtime "+${KEEP_POLLY_DAYS}" -delete 2>/dev/null || true
  else
    append_log "{\"event\":\"backup_failed\",\"file\":\"polly.db\",\"error\":\"sqlite3 exit non-zero\"}"
    errors=$((errors + 1))
  fi
else
  append_log "{\"event\":\"backup_skip\",\"file\":\"polly.db\",\"reason\":\"file not found\"}"
fi

# ── runs.sqlite backup ────────────────────────────────────────────────────────
if [[ -f "${RUNS_DB}" ]]; then
  RUNS_BACKUP="${BACKUP_DIR}/runs.sqlite.${TODAY}"
  if sqlite3 "${RUNS_DB}" ".backup '${RUNS_BACKUP}'" 2>/dev/null; then
    size="$(du -sh "${RUNS_BACKUP}" | cut -f1)"
    append_log "{\"event\":\"backup_ok\",\"file\":\"runs.sqlite\",\"dest\":\"${RUNS_BACKUP}\",\"size\":\"${size}\"}"
    find "${BACKUP_DIR}" -name "runs.sqlite.*" -mtime "+${KEEP_RUNS_DAYS}" -delete 2>/dev/null || true
  else
    # runs.sqlite may be locked during active cron runs; not a hard error
    append_log "{\"event\":\"backup_skip\",\"file\":\"runs.sqlite\",\"reason\":\"locked or unavailable\"}"
  fi
else
  append_log "{\"event\":\"backup_skip\",\"file\":\"runs.sqlite\",\"reason\":\"file not found\"}"
fi

# ── summary ───────────────────────────────────────────────────────────────────
status="ok"
[[ "${errors}" -gt 0 ]] && status="error"

summary="{\"ts\":\"$(timestamp_utc)\",\"event\":\"nightly_backup\",\"status\":\"${status}\",\"errors\":${errors}}"
append_log "${summary}"
printf '%s\n' "${summary}"
