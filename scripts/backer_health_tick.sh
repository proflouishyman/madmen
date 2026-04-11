#!/usr/bin/env bash
set -euo pipefail

# Function purpose: deterministic infrastructure health check for Backer cron.
# This avoids long free-form LLM turns that can overlap and block the task queue.

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
BACKER_WS="${OPENCLAW_HOME}/workspaces/backer-workspace"
ALERTS_FILE="${BACKER_WS}/alerts/urgent.yaml"
LOG_DIR="${BACKER_WS}/logs"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLLY_MODEL="${POLLY_MODEL:-qwen2.5:7b-instruct}"
POLLY_PLIST="${HOME}/Library/LaunchAgents/com.ollama.polly.plist"
UID_NUM="$(id -u)"
STALE_RECONCILE_GRACE_SECONDS="${BACKER_STALE_RECONCILE_GRACE_SECONDS:-420}"

mkdir -p "${LOG_DIR}" "$(dirname "${ALERTS_FILE}")"
[[ -f "${ALERTS_FILE}" ]] || printf '[]\n' >"${ALERTS_FILE}"

timestamp_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

probe_url() {
  local url="$1"
  curl -sf --max-time 10 "${url}" >/dev/null
}

restart_polly_lane() {
  launchctl bootout "gui/${UID_NUM}/com.ollama.polly" >/dev/null 2>&1 || true
  launchctl bootout "gui/${UID_NUM}" "${POLLY_PLIST}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/${UID_NUM}" "${POLLY_PLIST}" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/${UID_NUM}/com.ollama.polly" >/dev/null 2>&1 || true
}

restart_primary_lane() {
  # Fallback sequence for non-homebrew launch labels.
  launchctl kickstart -k "gui/${UID_NUM}/com.ollama.ollama" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/${UID_NUM}/homebrew.mxcl.ollama" >/dev/null 2>&1 || true
}

append_log() {
  local line="$1"
  local log_file="${LOG_DIR}/$(date +%Y-%m-%d).log"
  printf '%s %s\n' "$(timestamp_utc)" "${line}" >>"${log_file}"
}

primary_ok=true
polly_ok=true
primary_restarted=false
polly_restarted=false
prewarm_ok=true
stale_tasks_marked=0
stale_sessions_cleared=0

if ! probe_url "http://127.0.0.1:11434/api/tags"; then
  sleep 1
  if ! probe_url "http://127.0.0.1:11434/api/tags"; then
    restart_primary_lane
    primary_restarted=true
    sleep 2
    if ! probe_url "http://127.0.0.1:11434/api/tags"; then
      primary_ok=false
    fi
  fi
fi

if ! probe_url "http://127.0.0.1:11435/api/tags"; then
  sleep 1
  if ! probe_url "http://127.0.0.1:11435/api/tags"; then
    restart_polly_lane
    polly_restarted=true
    sleep 2
    if ! probe_url "http://127.0.0.1:11435/api/tags"; then
      polly_ok=false
      prewarm_ok=false
    fi
  fi
fi

if [[ "${polly_ok}" == true ]]; then
  if ! curl -sf --max-time 30 "http://127.0.0.1:11435/api/generate" \
    -d "{\"model\":\"${POLLY_MODEL}\",\"prompt\":\"ping\",\"stream\":false}" \
    >/dev/null; then
    prewarm_ok=false
  fi
fi

# Non-obvious invariant: OpenClaw may leave stale `running` rows/status markers
# after hard timeouts. Reconcile them each health cycle so queues do not clog.
if reconcile_json="$(python3 "${SCRIPT_DIR}/reconcile_runtime_state.py" \
  --openclaw-home "${OPENCLAW_HOME}" \
  --grace-seconds "${STALE_RECONCILE_GRACE_SECONDS}" \
  --force 2>/dev/null)"; then
  stale_tasks_marked="$(python3 -c 'import json,sys; print(int(json.loads(sys.argv[1]).get("tasks_marked_lost", 0)))' "${reconcile_json}" 2>/dev/null || echo 0)"
  stale_sessions_cleared="$(python3 -c 'import json,sys; print(int(json.loads(sys.argv[1]).get("sessions_marked_idle", 0)))' "${reconcile_json}" 2>/dev/null || echo 0)"
fi

# Count uncleared alerts with a tolerant parser: explicit "cleared: true" lines
# are considered handled; everything else is treated as pending.
pending_alerts="$(python3 - "${ALERTS_FILE}" <<'PY'
import pathlib
import re
import sys
text = pathlib.Path(sys.argv[1]).read_text().strip()
if not text or text == "[]":
    print(0)
    raise SystemExit(0)
cleared = len(re.findall(r"cleared\s*:\s*true", text, flags=re.I))
items = text.count("- ")
if items == 0:
    # fallback: count mapping blocks by "message:" keys
    items = len(re.findall(r"message\s*:", text))
pending = max(items - cleared, 0)
print(pending)
PY
)"

status="ok"
if [[ "${primary_ok}" != true || "${polly_ok}" != true || "${prewarm_ok}" != true ]]; then
  status="degraded"
fi

summary="{\"ts\":\"$(timestamp_utc)\",\"status\":\"${status}\",\"primary_ok\":${primary_ok},\"polly_ok\":${polly_ok},\"primary_restarted\":${primary_restarted},\"polly_restarted\":${polly_restarted},\"prewarm_ok\":${prewarm_ok},\"pending_alerts\":${pending_alerts},\"stale_tasks_marked\":${stale_tasks_marked},\"stale_sessions_cleared\":${stale_sessions_cleared}}"
append_log "${summary}"
printf '%s\n' "${summary}"
