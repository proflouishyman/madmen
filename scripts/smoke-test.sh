#!/usr/bin/env bash
set -euo pipefail

# Function purpose: quick operational smoke test for Polly resilience addendum.

failures=0

check() {
  local name="$1"
  local cmd="$2"
  local expected="$3"
  local out
  out="$(eval "$cmd" 2>&1 || true)"
  if [[ "$out" == *"$expected"* ]]; then
    printf '[PASS] %s\n' "$name"
  else
    printf '[FAIL] %s\n' "$name"
    printf '  expected contains: %s\n' "$expected"
    printf '  actual: %s\n' "$out"
    failures=$((failures + 1))
  fi
}

check_regex() {
  local name="$1"
  local cmd="$2"
  local regex="$3"
  local out
  out="$(eval "$cmd" 2>&1 || true)"
  if printf '%s\n' "$out" | rg -q "$regex"; then
    printf '[PASS] %s\n' "$name"
  else
    printf '[FAIL] %s\n' "$name"
    printf '  expected regex: %s\n' "$regex"
    printf '  actual: %s\n' "$out"
    failures=$((failures + 1))
  fi
}

check "Polly Ollama launchd loaded" \
  "launchctl list | rg 'com\\.ollama\\.polly' || true" \
  "com.ollama.polly"

check "Polly Ollama running" \
  "curl -s --max-time 5 http://localhost:11435/api/tags | python3 -c 'import sys,json; d=json.load(sys.stdin); print(\"ok\")'" \
  "ok"

check "Polly prewarm" \
  "curl -s --max-time 15 http://localhost:11435/api/generate -d '{\"model\":\"qwen2.5:7b-instruct\",\"prompt\":\"ping\",\"stream\":false}' | python3 -c 'import sys,json; json.load(sys.stdin); print(\"ok\")'" \
  "ok"

check_regex "OpenClaw has Polly provider override" \
  "python3 -c \"import json, pathlib; cfg=json.loads((pathlib.Path.home()/'.openclaw/openclaw.json').read_text()); print('ok' if 'ollama-polly' in cfg.get('models',{}).get('providers',{}) else 'missing')\"" \
  "^ok$"

check_regex "Polly model override set to dedicated lane" \
  "python3 -c \"import json, pathlib; cfg=json.loads((pathlib.Path.home()/'.openclaw/openclaw.json').read_text()); polly=[a for a in cfg.get('agents',{}).get('list',[]) if a.get('id')=='polly'][0]; m=polly.get('model'); print('ok' if isinstance(m, dict) and m.get('primary')=='ollama-polly/qwen2.5:7b-instruct' else 'missing')\"" \
  "^ok$"

check "polly.db healthy" \
  "sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db 'PRAGMA integrity_check;'" \
  "ok"

check "Backer alerts inbox exists" \
  "test -f ~/.openclaw/workspaces/backer-workspace/alerts/urgent.yaml && echo ok" \
  "ok"

check "Backer registered as OpenClaw agent" \
  "openclaw agents list --json | rg '\"id\": \"backer\"' || true" \
  "\"id\": \"backer\""

check "Backer health cron exists" \
  "openclaw cron list --json | rg '\"name\": \"backer-health-5m\"' || true" \
  "\"name\": \"backer-health-5m\""

check "Polly morning digest cron updated" \
  "openclaw cron list --json | rg '\"name\": \"polly-morning-digest\"' || true" \
  "\"name\": \"polly-morning-digest\""

if [[ "$failures" -gt 0 ]]; then
  printf '\nSmoke test failed: %s check(s) failed.\n' "$failures"
  exit 1
fi

printf '\nSmoke test passed: all checks succeeded.\n'
