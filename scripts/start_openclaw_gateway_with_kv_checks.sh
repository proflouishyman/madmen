#!/usr/bin/env bash
set -euo pipefail

# Function purpose: Start OpenClaw gateway with an Ollama shadow-plugin preflight.
# This script is intended to be used as the launchd ProgramArguments entrypoint.

CONFIG_PATH="${HOME}/.openclaw/openclaw.json"
SHADOW_PLUGIN_PATH="/Users/louishyman/openclaw/plugins/ollama"
MODEL_KEY="${OPENCLAW_OLLAMA_MODEL_KEY:-ollama/gemma4:26b}"
KEEP_ALIVE_DEFAULT="${OPENCLAW_OLLAMA_KEEPALIVE:-45m}"
NUM_BATCH_DEFAULT="${OPENCLAW_OLLAMA_NUM_BATCH:-16}"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "[kv-startup] missing config: ${CONFIG_PATH}" >&2
  exec openclaw gateway run "$@"
fi

# Function purpose: Idempotently enforce startup config for the shadow Ollama plugin.
python3 - "${CONFIG_PATH}" "${SHADOW_PLUGIN_PATH}" "${MODEL_KEY}" "${KEEP_ALIVE_DEFAULT}" "${NUM_BATCH_DEFAULT}" <<'PY'
import json
import pathlib
import sys

config_path = pathlib.Path(sys.argv[1])
shadow_plugin_path = sys.argv[2]
model_key = sys.argv[3]
keep_alive = sys.argv[4]
num_batch = int(sys.argv[5])

cfg = json.loads(config_path.read_text())
changed = False

plugins = cfg.setdefault("plugins", {})
load = plugins.setdefault("load", {})
paths = load.get("paths")
if not isinstance(paths, list):
    paths = []
if shadow_plugin_path not in paths:
    paths.insert(0, shadow_plugin_path)
    changed = True
load["paths"] = paths

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

if changed:
    config_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print("[kv-startup] updated openclaw.json for Ollama shadow plugin")
else:
    print("[kv-startup] openclaw.json already aligned")
PY

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
  if [[ "${source_path}" != "${SHADOW_PLUGIN_PATH}/index.js" ]]; then
    echo "[kv-startup] warning: ollama plugin source is '${source_path}', expected '${SHADOW_PLUGIN_PATH}/index.js'" >&2
  else
    echo "[kv-startup] ollama shadow plugin active: ${source_path}"
  fi
else
  echo "[kv-startup] warning: unable to inspect ollama plugin before gateway boot" >&2
fi

exec openclaw gateway run "$@"
