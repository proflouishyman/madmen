#!/usr/bin/env bash
set -euo pipefail

# Function purpose: Provision a dedicated Ollama instance for light/deterministic cron tasks.
# This is the "Tier 2" lane — a low-priority qwen2.5:7b instance on port 11436 that handles
# simple exec-and-return crons without competing with gemma4:26b on the main lane.
#
# Architecture:
#   Port 11434 — Heavy lane (gemma4:26b only, classification/reasoning tasks)
#   Port 11435 — Polly lane (qwen2.5:7b-instruct, user-facing fast path)
#   Port 11436 — Light lane (qwen2.5:7b, deterministic cron exec tasks)
#
# Why a separate instance: Ollama swaps models in/out of GPU memory. If the light model
# (4.7 GB) loads on port 11434 it evicts gemma4's warm KV cache (17 GB), destroying the
# 14x speedup from the KV cache plugin. Separate instances hold their own models permanently.

LIGHT_MODEL="qwen2.5:7b"
LIGHT_URL="http://127.0.0.1:11436"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.ollama.light.plist"

log() {
  printf '[light-lane] %s\n' "$*"
}

write_light_launchd_plist() {
  local models_path="${HOME}/.ollama/models"
  mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/.ollama-light"

  cat >"${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.ollama.light</string>
    <key>ProgramArguments</key>
    <array>
      <string>/usr/local/bin/ollama</string>
      <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
      <key>OLLAMA_HOST</key>
      <string>127.0.0.1:11436</string>
      <key>OLLAMA_MODELS</key>
      <string>${models_path}</string>
      <key>OLLAMA_KEEP_ALIVE</key>
      <string>10m</string>
      <key>OLLAMA_NUM_PARALLEL</key>
      <string>1</string>
      <key>OLLAMA_MAX_LOADED_MODELS</key>
      <string>1</string>
      <key>OLLAMA_KV_CACHE_TYPE</key>
      <string>q8_0</string>
      <key>OLLAMA_CONTEXT_LENGTH</key>
      <string>4096</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/ollama-light.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ollama-light-error.log</string>
  </dict>
</plist>
EOF
}

restart_light_ollama() {
  local gui_uid
  gui_uid="$(id -u)"
  launchctl bootout "gui/${gui_uid}/com.ollama.light" >/dev/null 2>&1 || true
  launchctl bootout "gui/${gui_uid}" "${PLIST_PATH}" >/dev/null 2>&1 || true
  if ! launchctl bootstrap "gui/${gui_uid}" "${PLIST_PATH}" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/${gui_uid}/com.ollama.light" >/dev/null 2>&1 || true
  fi
  launchctl kickstart -k "gui/${gui_uid}/com.ollama.light" >/dev/null

  local ok=0
  for _ in $(seq 1 20); do
    if curl -sf --max-time 2 "${LIGHT_URL}/api/tags" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 1
  done
  if [[ "${ok}" -ne 1 ]]; then
    printf 'light ollama endpoint did not come up at %s\n' "${LIGHT_URL}" >&2
    exit 1
  fi
}

prewarm_model() {
  log "prewarming ${LIGHT_MODEL} on ${LIGHT_URL}"
  curl -sf --max-time 30 "${LIGHT_URL}/api/generate" \
    -d "{\"model\": \"${LIGHT_MODEL}\", \"prompt\": \"hello\", \"stream\": false, \"keep_alive\": \"10m\"}" \
    >/dev/null 2>&1 || log "prewarm request sent (may still be loading)"
}

update_openclaw_config() {
  local config_path="${HOME}/.openclaw/openclaw.json"
  if [[ ! -f "${config_path}" ]]; then
    log "warning: ${config_path} not found, skipping config update"
    return
  fi

  python3 - "${config_path}" <<'PY'
import json
import pathlib
import sys

config_path = pathlib.Path(sys.argv[1])
cfg = json.loads(config_path.read_text())
changed = False

# Add ollama-light provider
providers = cfg.setdefault("models", {}).setdefault("providers", {})
if "ollama-light" not in providers:
    providers["ollama-light"] = {
        "baseUrl": "http://127.0.0.1:11436",
        "apiKey": "ollama-local",
        "api": "ollama",
        "models": [
            {
                "id": "qwen2.5:7b",
                "name": "qwen2.5:7b",
                "input": ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 4096,
            }
        ],
    }
    changed = True
    print("[light-lane] added ollama-light provider")

# Register model params under agent defaults
defaults = cfg.setdefault("agents", {}).setdefault("defaults", {})
models = defaults.setdefault("models", {})
light_key = "ollama-light/qwen2.5:7b"
if light_key not in models:
    models[light_key] = {
        "params": {
            "ollama": {
                "keepAlive": "10m",
                "options": {"num_batch": 8},
                "reliability": {
                    "requestTimeoutMs": 60000,
                    "maxRetries": 0,
                    "retryBackoffMs": 250,
                },
            }
        }
    }
    changed = True
    print("[light-lane] registered ollama-light/qwen2.5:7b model params")

# Update fallback chain: gemma4 → light → (nothing)
model_routing = defaults.get("model", {})
fallbacks = model_routing.get("fallbacks", [])
if light_key not in fallbacks:
    # Replace any existing ollama/qwen2.5:7b fallback with the light-lane version
    fallbacks = [light_key if f == "ollama/qwen2.5:7b" else f for f in fallbacks]
    if light_key not in fallbacks:
        fallbacks.append(light_key)
    model_routing["fallbacks"] = fallbacks
    defaults["model"] = model_routing
    changed = True
    print(f"[light-lane] updated fallback chain: {model_routing['primary']} → {fallbacks}")

if changed:
    config_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print("[light-lane] openclaw.json updated")
else:
    print("[light-lane] openclaw.json already aligned")
PY
}

update_cron_jobs() {
  local cron_path="${HOME}/.openclaw/cron/jobs.json"
  if [[ ! -f "${cron_path}" ]]; then
    log "warning: ${cron_path} not found, skipping cron update"
    return
  fi

  python3 - "${cron_path}" <<'PY'
import json
import pathlib
import sys

cron_path = pathlib.Path(sys.argv[1])
cron = json.loads(cron_path.read_text())
changed = False

# Migrate all cron jobs currently using ollama/qwen2.5:7b to ollama-light/qwen2.5:7b
for job in cron.get("jobs", []):
    payload = job.get("payload", {})
    if payload.get("model") == "ollama/qwen2.5:7b":
        payload["model"] = "ollama-light/qwen2.5:7b"
        changed = True
        print(f"[light-lane] cron {job.get('name', job['id'])}: → ollama-light/qwen2.5:7b")

if changed:
    cron_path.write_text(json.dumps(cron, indent=2) + "\n")
    print("[light-lane] cron/jobs.json updated")
else:
    print("[light-lane] cron jobs already aligned")
PY
}

# ─── Main ───
log "setting up light Ollama lane on port 11436"

write_light_launchd_plist
log "wrote launchd plist: ${PLIST_PATH}"

restart_light_ollama
log "light Ollama running at ${LIGHT_URL}"

prewarm_model
log "model prewarmed"

update_openclaw_config
update_cron_jobs

log "done — 3-lane architecture active"
log "  Port 11434: gemma4:26b (heavy — classification, reasoning, digests)"
log "  Port 11435: qwen2.5:7b-instruct (Polly — user-facing fast path)"
log "  Port 11436: qwen2.5:7b (light — deterministic cron exec tasks)"
