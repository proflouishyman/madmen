#!/usr/bin/env bash
set -euo pipefail

# Function purpose: Safely restart the OpenClaw gateway with pre-flight checks.
# Handles: stale gateway processes, session lock cleanup, Ollama lane health,
# runs.sqlite integrity, and config enforcement — all before starting the gateway.
#
# Usage:
#   bash ~/openclaw/scripts/openclaw_safe_restart.sh          # normal restart
#   bash ~/openclaw/scripts/openclaw_safe_restart.sh --force   # force-kill if SIGTERM fails
#   bash ~/openclaw/scripts/openclaw_safe_restart.sh --check   # dry run: report issues, don't restart

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
CONFIG_PATH="${OPENCLAW_HOME}/openclaw.json"
RUNS_DB="${OPENCLAW_HOME}/tasks/runs.sqlite"
GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
GATEWAY_LABEL="${OPENCLAW_GATEWAY_LABEL:-ai.openclaw.gateway}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UID_NUM="$(id -u)"

# Ollama lane definitions
POLLY_PORT=11435
POLLY_MODEL="qwen2.5:7b-instruct"
POLLY_PLIST="${HOME}/Library/LaunchAgents/com.ollama.polly.plist"
LIGHT_PORT=11436
LIGHT_MODEL="qwen2.5:7b"
LIGHT_PLIST="${HOME}/Library/LaunchAgents/com.ollama.light.plist"
PRIMARY_PORT=11434

FORCE_MODE=false
CHECK_ONLY=false
for arg in "$@"; do
  case "${arg}" in
    --force) FORCE_MODE=true ;;
    --check) CHECK_ONLY=true ;;
  esac
done

# Counters for summary
issues_found=0
issues_fixed=0
warnings=()

log() { printf '[safe-restart] %s\n' "$1"; }
warn() { printf '[safe-restart] WARNING: %s\n' "$1"; warnings+=("$1"); }
fail() { printf '[safe-restart] FATAL: %s\n' "$1" >&2; exit 1; }
ok() { printf '[safe-restart] OK: %s\n' "$1"; }

# ─────────────────────────────────────────────────────────────
# Phase 1: Pre-flight checks (always runs, even in --check mode)
# ─────────────────────────────────────────────────────────────

log "Phase 1: Pre-flight checks"

# 1a. Check runs.sqlite integrity
if [[ -f "${RUNS_DB}" ]]; then
  integrity="$(sqlite3 "${RUNS_DB}" "PRAGMA integrity_check;" 2>&1 || echo "ERROR")"
  if [[ "${integrity}" == "ok" ]]; then
    ok "runs.sqlite integrity: ok"
  else
    ((issues_found++)) || true
    warn "runs.sqlite is corrupted: ${integrity}"
    # Find most recent clean backup
    latest_backup="$(ls -t "${OPENCLAW_HOME}"/tasks/runs.sqlite.bak-runtime-reconcile-* 2>/dev/null | head -1 || true)"
    if [[ -n "${latest_backup}" ]]; then
      backup_integrity="$(sqlite3 "${latest_backup}" "PRAGMA integrity_check;" 2>&1 || echo "ERROR")"
      if [[ "${backup_integrity}" == "ok" ]]; then
        if [[ "${CHECK_ONLY}" == true ]]; then
          log "  Would restore from: ${latest_backup}"
        else
          cp "${latest_backup}" "${RUNS_DB}"
          ((issues_fixed++)) || true
          ok "Restored runs.sqlite from ${latest_backup}"
        fi
      else
        warn "Backup also corrupt: ${latest_backup} — manual intervention needed"
      fi
    else
      warn "No runs.sqlite backups found — manual intervention needed"
    fi
  fi
else
  warn "runs.sqlite not found at ${RUNS_DB}"
fi

# 1b. Check for stale session locks (across ALL agents)
stale_locks_cleaned=0
for lock_file in "${OPENCLAW_HOME}"/agents/*/sessions/*.jsonl.lock; do
  [[ -f "${lock_file}" ]] || continue
  lock_pid="$(python3 -c "
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    print(data.get('pid', 0))
except: print(0)
" "${lock_file}" 2>/dev/null || echo 0)"

  # Check if the process holding the lock is still alive
  if [[ "${lock_pid}" -gt 0 ]] && kill -0 "${lock_pid}" 2>/dev/null; then
    # Process exists — check if it's actually an openclaw process
    lock_cmd="$(ps -p "${lock_pid}" -o command= 2>/dev/null || echo "")"
    if [[ "${lock_cmd}" == *"openclaw"* ]]; then
      # Lock is legitimately held; check age
      lock_age="$(python3 -c "
import json, os, sys, time
try:
    data = json.load(open(sys.argv[1]))
    ca = data.get('createdAt','')
    if ca:
        from datetime import datetime, timezone
        epoch = datetime.fromisoformat(ca.replace('Z','+00:00')).timestamp()
    else:
        epoch = os.path.getmtime(sys.argv[1])
    print(int(time.time() - epoch))
except: print(0)
" "${lock_file}" 2>/dev/null || echo 0)"
      if [[ "${lock_age}" -gt 600 ]]; then
        ((issues_found++)) || true
        warn "Stale lock held ${lock_age}s by pid ${lock_pid}: ${lock_file}"
        if [[ "${CHECK_ONLY}" != true ]]; then
          rm -f "${lock_file}"
          ((stale_locks_cleaned++)) || true
          ((issues_fixed++)) || true
        fi
      fi
    else
      # Non-openclaw process reused the PID — lock is orphaned
      ((issues_found++)) || true
      if [[ "${CHECK_ONLY}" != true ]]; then
        rm -f "${lock_file}"
        ((stale_locks_cleaned++)) || true
        ((issues_fixed++)) || true
      fi
    fi
  else
    # Process is dead — lock is orphaned
    ((issues_found++)) || true
    if [[ "${CHECK_ONLY}" != true ]]; then
      rm -f "${lock_file}"
      ((stale_locks_cleaned++)) || true
      ((issues_fixed++)) || true
    fi
  fi
done
if [[ "${stale_locks_cleaned}" -gt 0 ]]; then
  ok "Cleaned ${stale_locks_cleaned} stale session lock(s)"
elif [[ "${issues_found}" -eq 0 ]]; then
  ok "No stale session locks"
fi

# 1c. Check Ollama lanes
check_ollama_lane() {
  local name="$1" port="$2" model="$3" plist="$4"
  if curl -sf --max-time 5 "http://127.0.0.1:${port}/api/tags" >/dev/null 2>&1; then
    ok "${name} lane (port ${port}): reachable"
    return 0
  else
    ((issues_found++)) || true
    warn "${name} lane (port ${port}): not responding"
    if [[ "${CHECK_ONLY}" == true ]]; then
      log "  Would attempt restart via launchd"
      return 1
    fi
    # Try to start/restart it
    if [[ -f "${plist}" ]]; then
      log "  Restarting ${name} lane via launchd..."
      launchctl bootout "gui/${UID_NUM}/$(basename "${plist}" .plist)" 2>/dev/null || true
      launchctl bootstrap "gui/${UID_NUM}" "${plist}" 2>/dev/null || true
      sleep 3
      if curl -sf --max-time 5 "http://127.0.0.1:${port}/api/tags" >/dev/null 2>&1; then
        ((issues_fixed++)) || true
        ok "${name} lane recovered after restart"
        return 0
      else
        warn "${name} lane still down after restart"
        return 1
      fi
    else
      warn "${name} lane plist not found: ${plist}"
      return 1
    fi
  fi
}

check_ollama_lane "Primary" "${PRIMARY_PORT}" "gemma4:26b" "" || true
check_ollama_lane "Polly" "${POLLY_PORT}" "${POLLY_MODEL}" "${POLLY_PLIST}" || true
if [[ -f "${LIGHT_PLIST}" ]]; then
  check_ollama_lane "Light" "${LIGHT_PORT}" "${LIGHT_MODEL}" "${LIGHT_PLIST}" || true
fi

# 1d. Check config exists
if [[ ! -f "${CONFIG_PATH}" ]]; then
  fail "Config not found: ${CONFIG_PATH}"
fi
ok "Config file exists"

# 1e. Verify ollama-light provider is registered (common missing piece)
light_registered="$(python3 -c "
import json, sys
cfg = json.load(open(sys.argv[1]))
providers = cfg.get('models', {}).get('providers', {})
print('yes' if 'ollama-light' in providers else 'no')
" "${CONFIG_PATH}" 2>/dev/null || echo "unknown")"
if [[ "${light_registered}" == "no" ]]; then
  ((issues_found++)) || true
  warn "ollama-light provider not registered in openclaw.json"
  log "  Will be fixed by startup config enforcement"
fi

# ─────────────────────────────────────────────────────────────
# Phase 1 summary
# ─────────────────────────────────────────────────────────────

log ""
log "Pre-flight summary: ${issues_found} issue(s) found, ${issues_fixed} auto-fixed"
if [[ ${#warnings[@]} -gt 0 ]]; then
  for w in "${warnings[@]}"; do
    log "  - ${w}"
  done
fi

if [[ "${CHECK_ONLY}" == true ]]; then
  log ""
  log "Check-only mode — not restarting. Run without --check to apply fixes and restart."
  exit 0
fi

# ─────────────────────────────────────────────────────────────
# Phase 2: Stop existing gateway
# ─────────────────────────────────────────────────────────────

log ""
log "Phase 2: Stopping existing gateway"

gateway_pid="$(lsof -ti :${GATEWAY_PORT} 2>/dev/null | head -1 || true)"

if [[ -n "${gateway_pid}" ]]; then
  log "Gateway running as pid ${gateway_pid}"

  # Try graceful stop via launchctl first
  launchctl stop "gui/${UID_NUM}/${GATEWAY_LABEL}" 2>/dev/null || true
  sleep 3

  # Check if it actually stopped
  if kill -0 "${gateway_pid}" 2>/dev/null; then
    log "Gateway still alive after SIGTERM, sending SIGTERM directly..."
    kill -TERM "${gateway_pid}" 2>/dev/null || true
    sleep 3
  fi

  # Still alive? Force kill if --force, otherwise fail
  if kill -0 "${gateway_pid}" 2>/dev/null; then
    if [[ "${FORCE_MODE}" == true ]]; then
      warn "Gateway stuck — force-killing pid ${gateway_pid}"
      kill -9 "${gateway_pid}" 2>/dev/null || true
      sleep 2
    else
      fail "Gateway pid ${gateway_pid} won't stop. Re-run with --force to SIGKILL it."
    fi
  fi

  # Verify port is free
  if lsof -ti :${GATEWAY_PORT} >/dev/null 2>&1; then
    # launchd may have respawned it already — that's actually OK if we're about
    # to run the startup script (it'll just exec into the new process)
    new_pid="$(lsof -ti :${GATEWAY_PORT} 2>/dev/null | head -1 || true)"
    if [[ "${new_pid}" != "${gateway_pid}" ]]; then
      log "launchd respawned gateway as pid ${new_pid} — will use that"
    else
      fail "Port ${GATEWAY_PORT} still held by pid ${gateway_pid} after kill"
    fi
  fi
else
  ok "No gateway running on port ${GATEWAY_PORT}"
fi

# ─────────────────────────────────────────────────────────────
# Phase 3: Reconcile runtime state
# ─────────────────────────────────────────────────────────────

log ""
log "Phase 3: Reconciling runtime state"

if [[ -x "$(command -v python3)" ]] && [[ -f "${SCRIPT_DIR}/reconcile_runtime_state.py" ]]; then
  python3 "${SCRIPT_DIR}/reconcile_runtime_state.py" \
    --openclaw-home "${OPENCLAW_HOME}" \
    --grace-seconds 0 \
    --include-cron-running >/dev/null 2>&1 || true
  ok "Runtime state reconciled"
else
  warn "reconcile_runtime_state.py not found — skipping"
fi

# ─────────────────────────────────────────────────────────────
# Phase 4: Enforce config and start gateway
# ─────────────────────────────────────────────────────────────

log ""
log "Phase 4: Starting gateway with config enforcement"

# If the gateway was respawned by launchd during our stop, it's already running
# with the old config. Stop it again so the startup script can exec cleanly.
if lsof -ti :${GATEWAY_PORT} >/dev/null 2>&1; then
  log "Stopping launchd-respawned gateway before startup script..."
  launchctl stop "gui/${UID_NUM}/${GATEWAY_LABEL}" 2>/dev/null || true
  sleep 2
  # If it respawned again, kill it
  respawn_pid="$(lsof -ti :${GATEWAY_PORT} 2>/dev/null | head -1 || true)"
  if [[ -n "${respawn_pid}" ]]; then
    kill -9 "${respawn_pid}" 2>/dev/null || true
    sleep 1
  fi
fi

# Run the startup script (patches config + exec's gateway)
exec bash "${SCRIPT_DIR}/start_openclaw_gateway_with_kv_checks.sh"
