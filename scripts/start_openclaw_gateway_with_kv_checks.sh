#!/usr/bin/env bash
set -euo pipefail

# Function purpose: Start OpenClaw gateway with an Ollama shadow-plugin preflight.
# This script is intended to be used as the launchd ProgramArguments entrypoint.

CONFIG_PATH="${HOME}/.openclaw/openclaw.json"
SHADOW_PLUGIN_PATH="/Users/louishyman/openclaw/openclaw-ollama-kv-cache-plugin"
LEGACY_SHADOW_PLUGIN_PATH="/Users/louishyman/openclaw-ollama-kv-cache-plugin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_KEY="${OPENCLAW_OLLAMA_MODEL_KEY:-ollama/gemma4:26b}"
KEEP_ALIVE_DEFAULT="${OPENCLAW_OLLAMA_KEEPALIVE:-45m}"
NUM_BATCH_DEFAULT="${OPENCLAW_OLLAMA_NUM_BATCH:-16}"
REQUEST_TIMEOUT_DEFAULT="${OPENCLAW_OLLAMA_REQUEST_TIMEOUT_MS:-300000}"
MAX_RETRIES_DEFAULT="${OPENCLAW_OLLAMA_MAX_RETRIES:-0}"
RETRY_BACKOFF_DEFAULT="${OPENCLAW_OLLAMA_RETRY_BACKOFF_MS:-250}"
POLLY_MODEL_KEY="${OPENCLAW_POLLY_MODEL_KEY:-ollama-polly/qwen2.5:7b-instruct}"
POLLY_KEEP_ALIVE_DEFAULT="${OPENCLAW_POLLY_KEEPALIVE:--1}"
POLLY_REQUEST_TIMEOUT_DEFAULT="${OPENCLAW_POLLY_REQUEST_TIMEOUT_MS:-3000}"
POLLY_MAX_RETRIES_DEFAULT="${OPENCLAW_POLLY_MAX_RETRIES:-0}"
POLLY_RETRY_BACKOFF_DEFAULT="${OPENCLAW_POLLY_RETRY_BACKOFF_MS:-100}"
LIGHT_MODEL_KEY="${OPENCLAW_LIGHT_MODEL_KEY:-ollama-light/qwen2.5:7b}"
LIGHT_KEEP_ALIVE_DEFAULT="${OPENCLAW_LIGHT_KEEPALIVE:-10m}"
LIGHT_NUM_BATCH_DEFAULT="${OPENCLAW_LIGHT_NUM_BATCH:-8}"
LIGHT_REQUEST_TIMEOUT_DEFAULT="${OPENCLAW_LIGHT_REQUEST_TIMEOUT_MS:-60000}"
LIGHT_MAX_RETRIES_DEFAULT="${OPENCLAW_LIGHT_MAX_RETRIES:-0}"
LIGHT_RETRY_BACKOFF_DEFAULT="${OPENCLAW_LIGHT_RETRY_BACKOFF_MS:-250}"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "[kv-startup] missing config: ${CONFIG_PATH}" >&2
  exec openclaw gateway run "$@"
fi

# Function purpose: clean stale runtime markers before the gateway process owns new runs.
python3 "${SCRIPT_DIR}/reconcile_runtime_state.py" \
  --openclaw-home "${HOME}/.openclaw" \
  --grace-seconds 0 \
  --include-cron-running >/dev/null || true

# Function purpose: Remove orphaned session lock files left by dead processes.
# These cause "session file locked" errors that cascade into total model failure.
python3 - "${HOME}/.openclaw" <<'LOCKPY'
import glob, json, os, pathlib, sys
openclaw_home = pathlib.Path(sys.argv[1]).expanduser()
cleaned = 0
for lock_path in glob.glob(str(openclaw_home / "agents" / "*" / "sessions" / "*.jsonl.lock")):
    try:
        data = json.loads(pathlib.Path(lock_path).read_text())
        pid = int(data.get("pid", 0) or 0)
    except Exception:
        pid = 0
    # If pid is 0, or the process is dead, the lock is orphaned
    alive = False
    if pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except (ProcessLookupError, PermissionError):
            alive = False
    if not alive:
        os.remove(lock_path)
        cleaned += 1
if cleaned:
    print(f"[kv-startup] cleaned {cleaned} orphaned session lock(s)")
LOCKPY

# Function purpose: Idempotently enforce startup config for the shadow Ollama plugin.
python3 - \
  "${CONFIG_PATH}" \
  "${SHADOW_PLUGIN_PATH}" \
  "${LEGACY_SHADOW_PLUGIN_PATH}" \
  "${MODEL_KEY}" \
  "${KEEP_ALIVE_DEFAULT}" \
  "${NUM_BATCH_DEFAULT}" \
  "${REQUEST_TIMEOUT_DEFAULT}" \
  "${MAX_RETRIES_DEFAULT}" \
  "${RETRY_BACKOFF_DEFAULT}" \
  "${POLLY_MODEL_KEY}" \
  "${POLLY_KEEP_ALIVE_DEFAULT}" \
  "${POLLY_REQUEST_TIMEOUT_DEFAULT}" \
  "${POLLY_MAX_RETRIES_DEFAULT}" \
  "${POLLY_RETRY_BACKOFF_DEFAULT}" \
  "${LIGHT_MODEL_KEY}" \
  "${LIGHT_KEEP_ALIVE_DEFAULT}" \
  "${LIGHT_NUM_BATCH_DEFAULT}" \
  "${LIGHT_REQUEST_TIMEOUT_DEFAULT}" \
  "${LIGHT_MAX_RETRIES_DEFAULT}" \
  "${LIGHT_RETRY_BACKOFF_DEFAULT}" <<'PY'
import json
import pathlib
import sys

config_path = pathlib.Path(sys.argv[1])
shadow_plugin_path = sys.argv[2]
legacy_shadow_plugin_path = sys.argv[3]
model_key = sys.argv[4]
keep_alive = sys.argv[5]
num_batch = int(sys.argv[6])
request_timeout_ms = int(sys.argv[7])
max_retries = int(sys.argv[8])
retry_backoff_ms = int(sys.argv[9])
polly_model_key = sys.argv[10]
polly_keep_alive = sys.argv[11]
polly_request_timeout_ms = int(sys.argv[12])
polly_max_retries = int(sys.argv[13])
polly_retry_backoff_ms = int(sys.argv[14])
light_model_key = sys.argv[15]
light_keep_alive = sys.argv[16]
light_num_batch = int(sys.argv[17])
light_request_timeout_ms = int(sys.argv[18])
light_max_retries = int(sys.argv[19])
light_retry_backoff_ms = int(sys.argv[20])

cfg = json.loads(config_path.read_text())
changed = False

plugins = cfg.setdefault("plugins", {})
load = plugins.setdefault("load", {})
paths = load.get("paths")
if not isinstance(paths, list):
    paths = []
shadow_plugin_exists = pathlib.Path(shadow_plugin_path).is_dir()
legacy_shadow_plugin_exists = pathlib.Path(legacy_shadow_plugin_path).is_dir()
preferred_shadow_path = None
if shadow_plugin_exists:
    preferred_shadow_path = shadow_plugin_path
elif legacy_shadow_plugin_exists:
    preferred_shadow_path = legacy_shadow_plugin_path

filtered_paths = [p for p in paths if p not in {shadow_plugin_path, legacy_shadow_plugin_path}]
if filtered_paths != paths:
    changed = True
paths = filtered_paths

if preferred_shadow_path:
    if preferred_shadow_path not in paths:
        paths.insert(0, preferred_shadow_path)
        changed = True
    if paths and paths[0] != preferred_shadow_path:
        paths = [preferred_shadow_path, *[p for p in paths if p != preferred_shadow_path]]
        changed = True
load["paths"] = paths

entries = plugins.get("entries")
if isinstance(entries, dict) and "ollama" in entries:
    # Non-obvious invariant: when a config path shadows bundled `ollama`,
    # keeping plugins.entries.ollama creates noisy duplicate-id diagnostics.
    entries.pop("ollama", None)
    changed = True

agents = cfg.setdefault("agents", {})
defaults = agents.setdefault("defaults", {})
models = defaults.setdefault("models", {})
model_cfg = models.setdefault(model_key, {})
params = model_cfg.setdefault("params", {})
ollama = params.setdefault("ollama", {})

if ollama.get("keepAlive") != keep_alive:
    ollama["keepAlive"] = keep_alive
    changed = True

options = ollama.get("options")
if not isinstance(options, dict):
    options = {}
if options.get("num_batch") != num_batch:
    options["num_batch"] = num_batch
    changed = True
ollama["options"] = options

reliability = ollama.get("reliability")
if not isinstance(reliability, dict):
    reliability = {}
if reliability.get("requestTimeoutMs") != request_timeout_ms:
    reliability["requestTimeoutMs"] = request_timeout_ms
    changed = True
if reliability.get("maxRetries") != max_retries:
    reliability["maxRetries"] = max_retries
    changed = True
if reliability.get("retryBackoffMs") != retry_backoff_ms:
    reliability["retryBackoffMs"] = retry_backoff_ms
    changed = True
ollama["reliability"] = reliability

polly_cfg = models.setdefault(polly_model_key, {})
polly_params = polly_cfg.setdefault("params", {})
polly_ollama = polly_params.setdefault("ollama", {})

if polly_ollama.get("keepAlive") != polly_keep_alive:
    polly_ollama["keepAlive"] = polly_keep_alive
    changed = True

polly_reliability = polly_ollama.get("reliability")
if not isinstance(polly_reliability, dict):
    polly_reliability = {}
if polly_reliability.get("requestTimeoutMs") != polly_request_timeout_ms:
    polly_reliability["requestTimeoutMs"] = polly_request_timeout_ms
    changed = True
if polly_reliability.get("maxRetries") != polly_max_retries:
    polly_reliability["maxRetries"] = polly_max_retries
    changed = True
if polly_reliability.get("retryBackoffMs") != polly_retry_backoff_ms:
    polly_reliability["retryBackoffMs"] = polly_retry_backoff_ms
    changed = True
polly_ollama["reliability"] = polly_reliability

# Light lane model params (ollama-light/qwen2.5:7b on port 11436)
light_cfg = models.setdefault(light_model_key, {})
light_params = light_cfg.setdefault("params", {})
light_ollama = light_params.setdefault("ollama", {})

if light_ollama.get("keepAlive") != light_keep_alive:
    light_ollama["keepAlive"] = light_keep_alive
    changed = True

light_options = light_ollama.get("options")
if not isinstance(light_options, dict):
    light_options = {}
if light_options.get("num_batch") != light_num_batch:
    light_options["num_batch"] = light_num_batch
    changed = True
light_ollama["options"] = light_options

light_reliability = light_ollama.get("reliability")
if not isinstance(light_reliability, dict):
    light_reliability = {}
if light_reliability.get("requestTimeoutMs") != light_request_timeout_ms:
    light_reliability["requestTimeoutMs"] = light_request_timeout_ms
    changed = True
if light_reliability.get("maxRetries") != light_max_retries:
    light_reliability["maxRetries"] = light_max_retries
    changed = True
if light_reliability.get("retryBackoffMs") != light_retry_backoff_ms:
    light_reliability["retryBackoffMs"] = light_retry_backoff_ms
    changed = True
light_ollama["reliability"] = light_reliability

if changed:
    config_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print("[kv-startup] updated openclaw.json for Ollama shadow plugin")
else:
    print("[kv-startup] openclaw.json already aligned")
PY

ACTIVE_SHADOW_PLUGIN_PATH=""
if [[ -d "${SHADOW_PLUGIN_PATH}" ]]; then
  ACTIVE_SHADOW_PLUGIN_PATH="${SHADOW_PLUGIN_PATH}"
elif [[ -d "${LEGACY_SHADOW_PLUGIN_PATH}" ]]; then
  ACTIVE_SHADOW_PLUGIN_PATH="${LEGACY_SHADOW_PLUGIN_PATH}"
fi

# Function purpose: Fail-open startup validation; logs warnings without blocking gateway boot.
if inspect_json="$(openclaw plugins inspect ollama --json 2>/dev/null)"; then
  source_path="$(python3 - "${inspect_json}" <<'PY'
import json
import sys
raw = sys.argv[1]
start = raw.find("{")
end = raw.rfind("}")
if start < 0 or end < 0 or end <= start:
    print("")
    raise SystemExit(0)
snippet = raw[start : end + 1]
try:
    payload = json.loads(snippet)
except Exception:
    print("")
    raise SystemExit(0)
print(payload.get("plugin", {}).get("source", ""))
PY
)"
  if [[ -n "${ACTIVE_SHADOW_PLUGIN_PATH}" ]]; then
    expected_source="${ACTIVE_SHADOW_PLUGIN_PATH}/index.js"
    if [[ "${source_path}" != "${expected_source}" ]]; then
      echo "[kv-startup] warning: ollama plugin source is '${source_path}', expected '${expected_source}'" >&2
    else
      echo "[kv-startup] ollama shadow plugin active: ${source_path}"
    fi
  else
    echo "[kv-startup] no local shadow plugin path found; using default ollama plugin source '${source_path}'"
  fi
else
  echo "[kv-startup] warning: unable to inspect ollama plugin before gateway boot" >&2
fi

# Function purpose: Inject a one-shot startup-notification cron job so Polly
# announces on Telegram that OpenClaw has restarted. The fireAt is set 90 seconds
# in the future to allow the gateway and Polly session to fully initialize first.
JOBS_JSON="${HOME}/.openclaw/cron/jobs.json"
if [[ -f "${JOBS_JSON}" ]]; then
  FIRE_AT_MS=$(( $(date +%s) * 1000 + 90000 ))
  python3 - "${JOBS_JSON}" "${FIRE_AT_MS}" <<'PY'
import json, sys, pathlib, uuid

jobs_path = pathlib.Path(sys.argv[1])
fire_at_ms = int(sys.argv[2])

data = json.loads(jobs_path.read_text())
jobs = data.get("jobs", []) if isinstance(data, dict) else data

# Remove any previous stale startup-notify jobs
jobs = [j for j in jobs if j.get("name") != "polly-startup-notify"]

# Inject fresh one-shot job
jobs.append({
    "id": str(uuid.uuid4()),
    "agentId": "polly",
    "name": "polly-startup-notify",
    "enabled": True,
    "createdAtMs": fire_at_ms - 90000,
    "updatedAtMs": fire_at_ms - 90000,
    "schedule": {"kind": "once"},
    "sessionTarget": "isolated",
    "wakeMode": "now",
    "payload": {
        "kind": "agentTurn",
        "message": "OpenClaw has just restarted. Send a Telegram message to Louis saying exactly: \"OpenClaw is back online.\" Nothing else.",
        "model": "ollama-polly/qwen2.5:7b-instruct",
        "toolsAllow": ["exec", "read", "write"],
        "thinking": "off",
        "timeoutSeconds": 60,
        "lightContext": True
    },
    "delivery": {"mode": "announce", "channel": "last"},
    "state": {"nextRunAtMs": fire_at_ms, "consecutiveErrors": 0}
})

if isinstance(data, dict):
    data["jobs"] = jobs
    jobs_path.write_text(json.dumps(data, indent=2))
else:
    jobs_path.write_text(json.dumps(jobs, indent=2))

print("[startup-notify] injected polly-startup-notify cron job (fireAt +90s)")
PY
fi

exec openclaw gateway run "$@"
