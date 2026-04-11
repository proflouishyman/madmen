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
OPENCLAW_GATEWAY_LABEL="${OPENCLAW_GATEWAY_LABEL:-ai.openclaw.gateway}"
UID_NUM="$(id -u)"
STALE_RECONCILE_GRACE_SECONDS="${BACKER_STALE_RECONCILE_GRACE_SECONDS:-420}"
LOCK_MAX_AGE_SECONDS="${BACKER_LOCK_MAX_AGE_SECONDS:-240}"

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

restart_openclaw_gateway() {
  launchctl kickstart -k "gui/${UID_NUM}/${OPENCLAW_GATEWAY_LABEL}" >/dev/null 2>&1 || true
}

detect_stale_session_locks() {
  python3 - "${OPENCLAW_HOME}" "${LOCK_MAX_AGE_SECONDS}" <<'PY'
import datetime as dt
import glob
import json
import os
import pathlib
import subprocess
import sys
import time

openclaw_home = pathlib.Path(sys.argv[1]).expanduser()
max_age_seconds = int(sys.argv[2])
now = time.time()
matches = []

def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def pid_command(pid: int) -> str:
    try:
        cp = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    if cp.returncode != 0:
        return ""
    return (cp.stdout or "").strip()

for lock_path in sorted(glob.glob(str(openclaw_home / "agents" / "*" / "sessions" / "*.jsonl.lock"))):
    path = pathlib.Path(lock_path)
    payload = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    pid = int(payload.get("pid", 0) or 0)
    if not pid_exists(pid):
        continue

    command = pid_command(pid)
    if "openclaw" not in command:
        continue

    created_at = payload.get("createdAt")
    created_epoch = None
    if isinstance(created_at, str):
        try:
            created_epoch = dt.datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            created_epoch = None
    if created_epoch is None:
        created_epoch = path.stat().st_mtime

    age_seconds = max(0, int(now - created_epoch))
    if age_seconds >= max_age_seconds:
        matches.append(
            {
                "path": str(path),
                "pid": pid,
                "ageSeconds": age_seconds,
                "command": command,
            }
        )

print(json.dumps({"count": len(matches), "locks": matches}))
PY
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
stale_locks_detected=0
stale_lock_heal_triggered=false

if stale_lock_json="$(detect_stale_session_locks 2>/dev/null)"; then
  stale_locks_detected="$(
    python3 -c 'import json,sys; print(int(json.loads(sys.argv[1]).get("count", 0)))' \
      "${stale_lock_json}" 2>/dev/null || echo 0
  )"
  if [[ "${stale_locks_detected}" -gt 0 ]]; then
    restart_openclaw_gateway
    stale_lock_heal_triggered=true
    sleep 2
  fi
fi

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

summary="{\"ts\":\"$(timestamp_utc)\",\"status\":\"${status}\",\"primary_ok\":${primary_ok},\"polly_ok\":${polly_ok},\"primary_restarted\":${primary_restarted},\"polly_restarted\":${polly_restarted},\"prewarm_ok\":${prewarm_ok},\"pending_alerts\":${pending_alerts},\"stale_tasks_marked\":${stale_tasks_marked},\"stale_sessions_cleared\":${stale_sessions_cleared},\"stale_locks_detected\":${stale_locks_detected},\"stale_lock_heal_triggered\":${stale_lock_heal_triggered}}"
append_log "${summary}"
printf '%s\n' "${summary}"
