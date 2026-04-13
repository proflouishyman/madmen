#!/usr/bin/env bash
set -euo pipefail

# Function purpose: Apply ADDENDUM_POLLY_RESILIENCE_v2 live runtime changes idempotently.
# The script updates OpenClaw runtime config/workspaces, provisions Polly's dedicated
# Ollama instance, initializes polly.db, and aligns cron jobs with resilience behavior.

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
OPENCLAW_CONFIG="${OPENCLAW_HOME}/openclaw.json"
OPENCLAW_PARAMS="${OPENCLAW_HOME}/openclaw-params.env"
POLLY_WS="${OPENCLAW_HOME}/workspaces/polly-workspace"
BACKER_WS="${OPENCLAW_HOME}/workspaces/backer-workspace"
POLLY_DB="${POLLY_WS}/polly.db"
POLLY_SCHEMA="${POLLY_WS}/polly-schema.sql"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.ollama.polly.plist"
POLLY_MODEL="qwen2.5:7b-instruct"
POLLY_URL="http://127.0.0.1:11435"
POLLY_PROVIDER_KEY="ollama-polly/qwen2.5:7b-instruct"
# Non-obvious invariant: Backer health must not consume Polly's dedicated lane.
BACKER_HEALTH_MODEL_KEY="${OPENCLAW_BACKER_HEALTH_MODEL_KEY:-ollama/gemma4:26b}"

log() {
  printf '[polly-addendum] %s\n' "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

ensure_prereqs() {
  require_cmd openclaw
  require_cmd ollama
  require_cmd curl
  require_cmd sqlite3
  require_cmd python3
}

ensure_model() {
  if ! ollama list | awk 'NR>1 {print $1}' | grep -qx "${POLLY_MODEL}"; then
    log "pulling missing Ollama model ${POLLY_MODEL}"
    ollama pull "${POLLY_MODEL}"
  else
    log "Ollama model ${POLLY_MODEL} already present"
  fi
}

write_polly_launchd_plist() {
  local models_path="${HOME}/.ollama/models"
  mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/.ollama-polly"

  cat >"${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.ollama.polly</string>
    <key>ProgramArguments</key>
    <array>
      <string>/usr/local/bin/ollama</string>
      <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
      <key>OLLAMA_HOST</key>
      <string>127.0.0.1:11435</string>
      <key>OLLAMA_MODELS</key>
      <string>${models_path}</string>
      <key>OLLAMA_KEEP_ALIVE</key>
      <string>-1</string>
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
    <string>/tmp/ollama-polly.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ollama-polly-error.log</string>
  </dict>
</plist>
EOF
}

restart_polly_ollama() {
  local gui_uid
  gui_uid="$(id -u)"
  # launchd supports different bootout signatures across macOS revisions.
  launchctl bootout "gui/${gui_uid}/com.ollama.polly" >/dev/null 2>&1 || true
  launchctl bootout "gui/${gui_uid}" "${PLIST_PATH}" >/dev/null 2>&1 || true
  if ! launchctl bootstrap "gui/${gui_uid}" "${PLIST_PATH}" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/${gui_uid}/com.ollama.polly" >/dev/null 2>&1 || true
  fi
  launchctl kickstart -k "gui/${gui_uid}/com.ollama.polly" >/dev/null

  local ok=0
  for _ in $(seq 1 20); do
    if curl -sf --max-time 2 "${POLLY_URL}/api/tags" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 1
  done
  if [[ "${ok}" -ne 1 ]]; then
    printf 'polly ollama endpoint did not come up at %s\n' "${POLLY_URL}" >&2
    exit 1
  fi

  curl -sf --max-time 20 "${POLLY_URL}/api/generate" \
    -d "{\"model\":\"${POLLY_MODEL}\",\"prompt\":\"ping\",\"stream\":false}" \
    >/dev/null
  log "Polly Ollama instance healthy and prewarmed"
}

upsert_openclaw_params_env() {
  mkdir -p "${OPENCLAW_HOME}"
  touch "${OPENCLAW_PARAMS}"
  python3 - "$OPENCLAW_PARAMS" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text() if path.exists() else ""
block = """# >>> POLLY RESILIENCE ADDENDUM >>>
POLLY_OLLAMA_URL=http://127.0.0.1:11435
POLLY_OLLAMA_MODEL=qwen2.5:7b-instruct
POLLY_LOCAL_TIMEOUT_SECONDS=3
POLLY_FALLBACK_MODEL=openai-codex/gpt-5.3-codex
POLLY_FALLBACK_SILENT=true
POLLY_DIRECT_RESPONSE_CTX=4096
POLLY_SYNTHESIS_CTX=32768
POLLY_ACK_TIMEOUT_SECONDS=60
POLLY_MAILMAN_TIMEOUT_SECONDS=90
POLLY_DEGRADED_DIGEST_ALLOWED=true
OLLAMA_KEEP_ALIVE=-1
OLLAMA_KV_CACHE_TYPE=q8_0
OLLAMA_NUM_PARALLEL=1
OLLAMA_CONTEXT_LENGTH=4096
# <<< POLLY RESILIENCE ADDENDUM <<<
"""

pattern = re.compile(
    r"# >>> POLLY RESILIENCE ADDENDUM >>>.*?# <<< POLLY RESILIENCE ADDENDUM <<<\n?",
    re.S,
)
if pattern.search(text):
    text = pattern.sub(block, text)
else:
    if text and not text.endswith("\n"):
        text += "\n"
    text += block
path.write_text(text)
PY
  log "updated ${OPENCLAW_PARAMS}"
}

patch_openclaw_json() {
  python3 - "$OPENCLAW_CONFIG" "$POLLY_PROVIDER_KEY" <<'PY'
import json
import pathlib
import shutil
import sys
from datetime import datetime

config_path = pathlib.Path(sys.argv[1])
polly_model_key = sys.argv[2]

if not config_path.exists():
    raise SystemExit(f"missing OpenClaw config: {config_path}")

backup_path = config_path.with_name(
    f"{config_path.name}.bak-polly-addendum.{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
)
shutil.copy2(config_path, backup_path)

cfg = json.loads(config_path.read_text())

models = cfg.setdefault("models", {})
providers = models.setdefault("providers", {})

providers["ollama-polly"] = {
    "baseUrl": "http://127.0.0.1:11435",
    "apiKey": "ollama-local",
    "api": "ollama",
    "models": [
        {
            "id": "qwen2.5:7b-instruct",
            "name": "qwen2.5:7b-instruct",
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 32768,
        }
    ],
}

agents = cfg.setdefault("agents", {})
defaults = agents.setdefault("defaults", {})
default_model = defaults.get("model")
if isinstance(default_model, str):
    defaults["model"] = {
        "primary": default_model,
        "fallbacks": ["openai-codex/gpt-5.3-codex"],
    }
elif isinstance(default_model, dict):
    primary = default_model.get("primary") or "ollama/gemma4:26b"
    fallbacks = [x for x in default_model.get("fallbacks", []) if isinstance(x, str) and x]
    if "openai-codex/gpt-5.3-codex" not in fallbacks:
        fallbacks.append("openai-codex/gpt-5.3-codex")
    defaults["model"] = {"primary": primary, "fallbacks": fallbacks}
else:
    defaults["model"] = {
        "primary": "ollama/gemma4:26b",
        "fallbacks": ["openai-codex/gpt-5.3-codex"],
    }

default_models = defaults.setdefault("models", {})
polly_model = default_models.setdefault(polly_model_key, {})
params = polly_model.setdefault("params", {})
ollama = params.setdefault("ollama", {})
ollama["keepAlive"] = "-1"
options = ollama.setdefault("options", {})
if "num_batch" not in options:
    options["num_batch"] = 16
reliability = ollama.setdefault("reliability", {})
reliability["requestTimeoutMs"] = 3000
reliability["maxRetries"] = 1
reliability["retryBackoffMs"] = 250

agent_list = agents.setdefault("list", [])
index_by_id = {
    entry.get("id"): idx
    for idx, entry in enumerate(agent_list)
    if isinstance(entry, dict) and entry.get("id")
}

if "polly" not in index_by_id:
    raise SystemExit("polly agent not found in agents.list")

polly_entry = agent_list[index_by_id["polly"]]
polly_entry["model"] = {
    "primary": polly_model_key,
    "fallbacks": ["openai-codex/gpt-5.3-codex", "ollama/gemma4:26b"],
}
polly_params = polly_entry.setdefault("params", {})
polly_meta = polly_params.setdefault("polly", {})
polly_meta["localTimeoutSeconds"] = 3
polly_meta["mailmanTimeoutSeconds"] = 90
polly_meta["ackTimeoutSeconds"] = 60
polly_meta["fallbackSilent"] = True

backer_entry = {
    "id": "backer",
    "name": "backer",
    "workspace": str(pathlib.Path.home() / ".openclaw" / "workspaces" / "backer-workspace"),
    "agentDir": str(pathlib.Path.home() / ".openclaw" / "agents" / "backer" / "agent"),
    "sandbox": {"mode": "off"},
}
if "backer" in index_by_id:
    existing = agent_list[index_by_id["backer"]]
    existing.update(backer_entry)
else:
    agent_list.append(backer_entry)

config_path.write_text(json.dumps(cfg, indent=2) + "\n")
print(backup_path)
PY
  log "patched ${OPENCLAW_CONFIG}"
}

write_polly_workspace_files() {
  mkdir -p "${POLLY_WS}/state" "${POLLY_WS}/memory" "${POLLY_WS}/logs"

  if ! grep -q '^## Resilience Rules$' "${POLLY_WS}/SOUL.md"; then
    cat >>"${POLLY_WS}/SOUL.md" <<'EOF'

## Resilience Rules

### Rule 1 — Acknowledge direct messages within 60 seconds
When Louis sends a direct message, I respond within 60 seconds.
If I need to query other agents, I acknowledge first.
Silence is never acceptable. Silence looks like a crash.
I use my fast inference path (port 11435 -> Codex fallback) for acknowledgments.
I never make Louis wait while I compete for the shared pool.

### Rule 2 — Never send a digest with unfilled placeholders
Before any Telegram send, I scan output for:
  [Insert / [Pending / [TBD / [TODO / [MISSING / [N/A - awaiting
If found: I do NOT send. I send a degraded digest instead.

### Rule 3 — Degrade gracefully when a mailman is down
If Maxwell or Otto does not respond within 90 seconds:
- Retry once. Then proceed without them.
- Read last known timestamp from polly.db agent_health table.
- Flag the missing section explicitly - never use a placeholder.
- Write alert to backer-workspace/alerts/urgent.yaml.

### Rule 4 — Pre-digest health check is mandatory
I do not send the morning digest until the 6:50 AM health check completes.
If health check reveals a failure, I degrade gracefully - I do not skip.

### Rule 5 — Model routing for direct messages
Direct messages from Louis -> fast inference path (port 11435, 3s timeout -> Codex).
Heavy synthesis -> shared pool or Codex as normal.
I never make Louis wait for an acknowledgment because the shared pool is busy.

## Acknowledgment Pattern

Every message from Louis gets an acknowledgment before I do anything.

Format (single Telegram message, within 60 seconds):
  ✅ Got it. [One sentence restatement in my own words.]

  I'm going to:
  1. [First step]
  2. [Second step]

Rules:
- Use fast inference path - never wait for shared pool.
- Restate in my own words, not Louis's exact words.
- List every planned step in order.
- If plan changes mid-execution: send an update immediately.
- Skip only when trivially immediate: no agent queries, from polly.db, under 5s.

## ACP-First Query Rule

I ask agents for information. I do not read their workspace files directly.
For any information I need from an agent: send ACP query and wait for response.
Exception (degraded mode only): if ACP times out after 90 seconds, I may read
their sweep-log.yaml directly, and I must flag degraded mode in output.

My own operational state lives in polly.db. I query it directly.
Rex owns relationships. I query Rex via ACP, not connections.db directly.

## Operational Database

My operational state lives in polly.db (SQLite, WAL mode).
Tables: tasks, commitments, waiting_on, escalations, events, projects,
captures (FTS5), drafts, agent_health.

I write to polly.db when:
- Louis gives me a task, commitment, waiting-on, escalation, or capture.
- I receive an ACP health response (update agent_health).
- A draft changes status.

I query polly.db when:
- Assembling morning digest (before ACP calls).
- Louis asks for dashboard/status.
- Running nag sweeps and stale commitment checks.
EOF
  fi

  if ! grep -q '^## Model Routing$' "${POLLY_WS}/TOOLS.md"; then
    cat >>"${POLLY_WS}/TOOLS.md" <<'EOF'

## Model Routing

### Fast inference path (every direct Louis message)
1. Ping http://127.0.0.1:11435 with a 3-second timeout.
2. If responsive: use qwen2.5:7b-instruct for this turn.
3. If timeout: switch to openai-codex/gpt-5.3-codex silently for this turn.
4. Write an alert to backer-workspace/alerts/urgent.yaml.

### Direct response tasks
Model: qwen2.5:7b-instruct via http://127.0.0.1:11435
Fallback: openai-codex/gpt-5.3-codex
Context: 4096 tokens
Temperature: 0.1

### Heavy synthesis tasks
Model: openai-codex/gpt-5.3-codex or ollama/gemma4:26b fallback
Context: 32768 tokens

## ACP Query Library

### Health checks
- `openclaw acp --from polly --to maxwell --auth "$ACP_TOKEN" --message '{"query_type":"health_status"}'`
- `openclaw acp --from polly --to otto --auth "$ACP_TOKEN" --message '{"query_type":"health_status"}'`

### Email queries
- `openclaw acp --from polly --to maxwell --auth "$ACP_TOKEN" --message '{"query_type":"overnight_summary"}'`
- `openclaw acp --from polly --to maxwell --auth "$ACP_TOKEN" --message '{"query_type":"unanswered_threads","min_urgency":"priority"}'`

### Domain agent summaries
- `openclaw acp --from polly --to finn --auth "$ACP_TOKEN" --message '{"query_type":"today_summary"}'`
- `openclaw acp --from polly --to weber --auth "$ACP_TOKEN" --message '{"query_type":"upcoming_deadlines","days_ahead":7}'`
- `openclaw acp --from polly --to john --auth "$ACP_TOKEN" --message '{"query_type":"project_status"}'`
- `openclaw acp --from polly --to emma --auth "$ACP_TOKEN" --message '{"query_type":"project_status"}'`
- `openclaw acp --from polly --to rex --auth "$ACP_TOKEN" --message '{"query_type":"contact_lookup","name":"[name]"}'`

## polly.db Access

Path: ~/.openclaw/workspaces/polly-workspace/polly.db
Mode: WAL
I query and write this DB directly for operational state.
EOF
  fi
}

write_backer_workspace_files() {
  mkdir -p "${BACKER_WS}/alerts" "${BACKER_WS}/logs" "${BACKER_WS}/memory" "${BACKER_WS}/.learnings" "${BACKER_WS}/.openclaw"
  mkdir -p "${OPENCLAW_HOME}/agents/backer/agent" "${OPENCLAW_HOME}/agents/backer/sessions"
  [[ -f "${BACKER_WS}/alerts/urgent.yaml" ]] || printf '[]\n' >"${BACKER_WS}/alerts/urgent.yaml"

  cat >"${BACKER_WS}/IDENTITY.md" <<'EOF'
- Name: Backer
- Role: Infrastructure reliability steward
- Priority: Keep Polly responsive and unattended loops healthy
EOF

  cat >"${BACKER_WS}/SOUL.md" <<'EOF'
# SOUL.md - Backer

## Mission
I own infrastructure resilience for this OpenClaw deployment.
I keep inference lanes healthy, process urgent runtime alerts, and restore service quickly.

## Ollama Lifecycle Management
I own both Ollama instances. No other agent may restart either.

### Primary instance - port 11434
Serves all agents except Polly.
Health check cadence: every 5 minutes.
Restart trigger: unresponsive twice in a row with 10-second probe timeout.

### Polly instance - port 11435
Serves Polly only and has higher priority.
Health check cadence: every 5 minutes.
Restart trigger: unresponsive twice in a row with 10-second probe timeout.
After restart: prewarm qwen2.5:7b-instruct and verify within 30 seconds.
Prewarm failure is P1 and must be escalated immediately.

### RAM pressure
Budget target for Ollama is 20-22GB aggregate.
If memory pressure is high, I recover by terminating stuck primary inference first.
I do not touch Polly's dedicated lane without explicit Louis authorization.

### Alert inbox
I process backer-workspace/alerts/urgent.yaml every health cycle.
For each uncleared item, I attempt remediation, append outcome, and mark cleared.
EOF

  cat >"${BACKER_WS}/TOOLS.md" <<'EOF'
# TOOLS.md - Backer

## Ollama Tools
- `curl -s --max-time 10 http://localhost:11434/api/tags`
- `curl -s --max-time 10 http://localhost:11435/api/tags`
- `curl -s --max-time 30 http://localhost:11435/api/generate -d '{"model":"qwen2.5:7b-instruct","prompt":"ping","stream":false}'`

## Restart Commands
- `launchctl kickstart -k gui/$(id -u)/com.ollama.ollama`
- `launchctl bootout gui/$(id -u) com.ollama.polly || true`
- `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ollama.polly.plist`
- `launchctl kickstart -k gui/$(id -u)/com.ollama.polly`

## Health Operations
- Read: `~/.openclaw/workspaces/backer-workspace/alerts/urgent.yaml`
- Check: `sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db "PRAGMA integrity_check;"`
- Checkpoint: `sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db "PRAGMA wal_checkpoint(TRUNCATE);"`

## Limits
- Do not send outbound communication unless explicitly asked.
- Do not modify domain-agent workspaces.
EOF

  cat >"${BACKER_WS}/AGENTS.md" <<'EOF'
# AGENTS.md - Backer Workspace

Read SOUL.md, USER.md, and current memory notes before acting.
Prioritize reliability outcomes and deterministic remediation.
EOF

  cat >"${BACKER_WS}/BOOTSTRAP.md" <<'EOF'
Backer handles infrastructure reliability: Ollama health, alert processing, and runtime audits.
EOF

  cat >"${BACKER_WS}/HEARTBEAT.md" <<'EOF'
Check alert inbox and inference lane health. If healthy, reply HEARTBEAT_OK.
EOF

  cat >"${BACKER_WS}/USER.md" <<'EOF'
Louis expects high reliability, concise diagnostics, and no silent failures.
EOF

  [[ -f "${BACKER_WS}/MEMORY.md" ]] || : >"${BACKER_WS}/MEMORY.md"
}

write_polly_schema_and_db() {
  cat >"${POLLY_SCHEMA}" <<'EOF'
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    owner_agent     TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    due             DATE,
    status          TEXT DEFAULT 'open',
    obligation      TEXT DEFAULT 'committed',
    waiting_on      TEXT,
    source          TEXT,
    notes           TEXT,
    last_verified   DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_owner      ON tasks(owner_agent);
CREATE INDEX IF NOT EXISTS idx_tasks_due        ON tasks(due);
CREATE INDEX IF NOT EXISTS idx_tasks_obligation ON tasks(obligation);

CREATE TABLE IF NOT EXISTS commitments (
    id              TEXT PRIMARY KEY,
    description     TEXT NOT NULL,
    to_whom         TEXT NOT NULL,
    context         TEXT,
    due             DATE,
    status          TEXT DEFAULT 'open',
    obligation      TEXT DEFAULT 'committed',
    agent_tracking  TEXT,
    source          TEXT,
    last_verified   DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_commits_status     ON commitments(status);
CREATE INDEX IF NOT EXISTS idx_commits_to_whom    ON commitments(to_whom);
CREATE INDEX IF NOT EXISTS idx_commits_due        ON commitments(due);
CREATE INDEX IF NOT EXISTS idx_commits_obligation ON commitments(obligation);

CREATE TABLE IF NOT EXISTS waiting_on (
    id              TEXT PRIMARY KEY,
    description     TEXT NOT NULL,
    from_whom       TEXT NOT NULL,
    context         TEXT,
    since           DATETIME NOT NULL,
    due             DATE,
    followup_rule   TEXT,
    obligation      TEXT DEFAULT 'committed',
    status          TEXT DEFAULT 'open',
    tracking_agent  TEXT,
    last_checked    DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_waiting_status    ON waiting_on(status);
CREATE INDEX IF NOT EXISTS idx_waiting_from_whom ON waiting_on(from_whom);
CREATE INDEX IF NOT EXISTS idx_waiting_since     ON waiting_on(since);

CREATE TABLE IF NOT EXISTS escalations (
    id              TEXT PRIMARY KEY,
    from_agent      TEXT NOT NULL,
    type            TEXT NOT NULL,
    summary         TEXT NOT NULL,
    source_object   TEXT,
    status          TEXT DEFAULT 'pending',
    polly_batched   INTEGER DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_esc_status     ON escalations(status);
CREATE INDEX IF NOT EXISTS idx_esc_type       ON escalations(type);
CREATE INDEX IF NOT EXISTS idx_esc_from_agent ON escalations(from_agent);

CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    title           TEXT NOT NULL,
    datetime        DATETIME NOT NULL,
    participants    TEXT,
    prep_required   INTEGER DEFAULT 0,
    prep_notes      TEXT,
    linked_task     TEXT,
    obligation      TEXT DEFAULT 'committed',
    status          TEXT DEFAULT 'upcoming',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_datetime ON events(datetime);
CREATE INDEX IF NOT EXISTS idx_events_status   ON events(status);

CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    owner_agent     TEXT NOT NULL,
    status          TEXT DEFAULT 'active',
    linked_agents   TEXT,
    notes           TEXT,
    last_updated    DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS project_commitments (
    project_id      TEXT NOT NULL,
    commitment_id   TEXT NOT NULL,
    PRIMARY KEY (project_id, commitment_id)
);

CREATE TABLE IF NOT EXISTS captures (
    id              TEXT PRIMARY KEY,
    captured_at     DATETIME NOT NULL,
    text            TEXT NOT NULL,
    category        TEXT,
    status          TEXT DEFAULT 'open',
    related_agent   TEXT,
    acted_on        DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts USING fts5(
    id              UNINDEXED,
    text,
    category        UNINDEXED,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS drafts (
    id              TEXT PRIMARY KEY,
    created_by      TEXT NOT NULL,
    type            TEXT NOT NULL,
    to_recipient    TEXT,
    subject         TEXT,
    body_file       TEXT,
    status          TEXT DEFAULT 'pending_approval',
    approval_tier   TEXT,
    tier_reason     TEXT,
    approval_at     DATETIME,
    sent_at         DATETIME,
    archived_at     DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_drafts_status     ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_created_by ON drafts(created_by);
CREATE INDEX IF NOT EXISTS idx_drafts_tier       ON drafts(approval_tier);

CREATE TABLE IF NOT EXISTS agent_health (
    agent_id        TEXT PRIMARY KEY,
    last_success_at DATETIME,
    last_status     TEXT,
    last_error      TEXT,
    session_size_kb INTEGER,
    items_processed INTEGER,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
EOF

  sqlite3 "${POLLY_DB}" <"${POLLY_SCHEMA}" >/dev/null
  sqlite3 "${POLLY_DB}" "PRAGMA journal_mode=WAL;" >/dev/null
  log "initialized ${POLLY_DB}"

  # Non-obvious invariant: if legacy YAML state files exist, archive them so they
  # do not remain a conflicting source of truth after DB initialization.
  local state_dir="${POLLY_WS}/state"
  local f
  for f in tasks commitments waiting_on escalations capture; do
    if [[ -f "${state_dir}/${f}.yaml" ]]; then
      mv "${state_dir}/${f}.yaml" "${state_dir}/${f}.yaml.archive"
    fi
  done
}

patch_cron_jobs() {
  local cron_json
  cron_json="$(openclaw cron list --json)"

  local morning_id precheck_id backer_health_id backer_audit_id backer_backup_id
  morning_id="$(python3 - "${cron_json}" <<'PY'
import json, sys
jobs=json.loads(sys.argv[1]).get("jobs", [])
for j in jobs:
    if j.get("agentId")=="polly" and j.get("name") in {"morning-brief","polly-morning-digest"}:
        print(j["id"])
        break
PY
)"
  precheck_id="$(python3 - "${cron_json}" <<'PY'
import json, sys
jobs=json.loads(sys.argv[1]).get("jobs", [])
for j in jobs:
    if j.get("agentId")=="polly" and j.get("name") in {"pre-digest-healthcheck","polly-pre-digest-healthcheck"}:
        print(j["id"])
        break
PY
)"
  backer_health_id="$(python3 - "${cron_json}" <<'PY'
import json, sys
jobs=json.loads(sys.argv[1]).get("jobs", [])
for j in jobs:
    if j.get("agentId")=="backer" and j.get("name")=="backer-health-5m":
        print(j["id"])
        break
PY
)"
  backer_audit_id="$(python3 - "${cron_json}" <<'PY'
import json, sys
jobs=json.loads(sys.argv[1]).get("jobs", [])
for j in jobs:
    if j.get("agentId")=="backer" and j.get("name")=="backer-daily-audit":
        print(j["id"])
        break
PY
)"
  backer_backup_id="$(python3 - "${cron_json}" <<'PY'
import json, sys
jobs=json.loads(sys.argv[1]).get("jobs", [])
for j in jobs:
    if j.get("agentId")=="backer" and j.get("name")=="backer-nightly-backup":
        print(j["id"])
        break
PY
)"

  local morning_message precheck_message backer_health_message backer_audit_message backer_backup_message

  morning_message="$(cat <<'EOF'
Assemble morning digest.

Step 1 — Query polly.db first (no ACP):
- urgent escalations (pending)
- overdue commitments (open due < today)
- pending drafts (pending_approval)
- overdue waiting-ons (open due < today)
- agent_health for all configured agents

Step 2 — ACP queries (90 second timeout each):
- maxwell overnight_summary
- otto overnight_summary
- finn today_summary
- weber upcoming_deadlines (48h)
- john project_status
- emma project_status

Step 3 — For ACP timeouts, use polly.db agent_health.last_success_at.
Never emit placeholders ([Insert], [Pending], [TODO], [MISSING], [N/A - awaiting).
If placeholders appear, rebuild degraded digest before send.

Step 4 — Assemble max 20 lines.
If any agent is degraded, include:
⚠️ System status: <agent> DEGRADED — section unavailable.

Step 5 — Send via Telegram and update polly.db:
- mark included escalations polly_batched=1
- upsert agent_health for responders
EOF
)"

  precheck_message="$(cat <<'EOF'
Run mandatory pre-digest health check before morning digest.
1. Verify gateway and telegram channel health.
2. Verify maxwell and otto freshness via ACP health_status.
3. If any failure is detected, append an alert item to
   /Users/louishyman/.openclaw/workspaces/backer-workspace/alerts/urgent.yaml
   and mark system degraded for digest.
4. Do not skip digest; only force degraded mode when needed.
EOF
)"

  backer_health_message="$(cat <<'EOF'
Execute exactly this command once via exec (do not call cron tools), then return
ONLY the command stdout with no edits or commentary:
/Users/louishyman/openclaw/scripts/backer_health_tick.sh
EOF
)"

  backer_audit_message="$(cat <<'EOF'
Run daily infrastructure audit.
1. Validate polly.db integrity: PRAGMA integrity_check.
2. Checkpoint polly.db WAL: PRAGMA wal_checkpoint(TRUNCATE).
3. Capture cron health summary and stale-error counts.
4. Summarize into backer-workspace/logs/<date>-daily-audit.log.
5. Escalate only if integrity check fails or repeated cron failures persist.
EOF
)"

  backer_backup_message="$(cat <<'EOF'
Run nightly DB checkpoint maintenance.
1. Execute PRAGMA wal_checkpoint(TRUNCATE) on polly.db.
2. Confirm result is successful.
3. Log output in backer-workspace/logs/<date>-nightly-backup.log.
EOF
)"

  if [[ -n "${morning_id}" ]]; then
    openclaw cron edit "${morning_id}" \
      --name "polly-morning-digest" \
      --cron "0 7 * * *" \
      --tz "America/New_York" \
      --model "openai-codex/gpt-5.3-codex" \
      --thinking low \
      --timeout-seconds 420 \
      --tools "exec,read,write" \
      --light-context \
      --announce \
      --message "${morning_message}" >/dev/null
  else
    openclaw cron add \
      --agent polly \
      --name "polly-morning-digest" \
      --cron "0 7 * * *" \
      --tz "America/New_York" \
      --model "openai-codex/gpt-5.3-codex" \
      --thinking low \
      --timeout-seconds 420 \
      --tools "exec,read,write" \
      --light-context \
      --announce \
      --message "${morning_message}" >/dev/null
  fi

  if [[ -n "${precheck_id}" ]]; then
    openclaw cron edit "${precheck_id}" \
      --name "polly-pre-digest-healthcheck" \
      --cron "50 6 * * *" \
      --tz "America/New_York" \
      --model "${POLLY_PROVIDER_KEY}" \
      --thinking low \
      --timeout-seconds 300 \
      --tools "exec,read,write" \
      --light-context \
      --announce \
      --message "${precheck_message}" >/dev/null
  else
    openclaw cron add \
      --agent polly \
      --name "polly-pre-digest-healthcheck" \
      --cron "50 6 * * *" \
      --tz "America/New_York" \
      --model "${POLLY_PROVIDER_KEY}" \
      --thinking low \
      --timeout-seconds 300 \
      --tools "exec,read,write" \
      --light-context \
      --announce \
      --message "${precheck_message}" >/dev/null
  fi

  if [[ -n "${backer_health_id}" ]]; then
    openclaw cron edit "${backer_health_id}" \
      --every "5m" \
      --model "${BACKER_HEALTH_MODEL_KEY}" \
      --thinking off \
      --timeout-seconds 420 \
      --tools "exec,read,write" \
      --light-context \
      --no-deliver \
      --message "${backer_health_message}" >/dev/null
  else
    openclaw cron add \
      --agent backer \
      --name "backer-health-5m" \
      --every "5m" \
      --model "${BACKER_HEALTH_MODEL_KEY}" \
      --thinking off \
      --timeout-seconds 420 \
      --tools "exec,read,write" \
      --light-context \
      --no-deliver \
      --message "${backer_health_message}" >/dev/null
  fi

  if [[ -n "${backer_audit_id}" ]]; then
    openclaw cron edit "${backer_audit_id}" \
      --cron "20 2 * * *" \
      --tz "America/New_York" \
      --thinking off \
      --timeout-seconds 300 \
      --tools "exec,read,write" \
      --light-context \
      --no-deliver \
      --message "${backer_audit_message}" >/dev/null
  else
    openclaw cron add \
      --agent backer \
      --name "backer-daily-audit" \
      --cron "20 2 * * *" \
      --tz "America/New_York" \
      --thinking off \
      --timeout-seconds 300 \
      --tools "exec,read,write" \
      --light-context \
      --no-deliver \
      --message "${backer_audit_message}" >/dev/null
  fi

  if [[ -n "${backer_backup_id}" ]]; then
    openclaw cron edit "${backer_backup_id}" \
      --cron "40 2 * * *" \
      --tz "America/New_York" \
      --thinking off \
      --timeout-seconds 180 \
      --tools "exec,read,write" \
      --light-context \
      --no-deliver \
      --message "${backer_backup_message}" >/dev/null
  else
    openclaw cron add \
      --agent backer \
      --name "backer-nightly-backup" \
      --cron "40 2 * * *" \
      --tz "America/New_York" \
      --thinking off \
      --timeout-seconds 180 \
      --tools "exec,read,write" \
      --light-context \
      --no-deliver \
      --message "${backer_backup_message}" >/dev/null
  fi

  log "cron jobs patched"
}

main() {
  ensure_prereqs
  ensure_model
  write_polly_launchd_plist
  restart_polly_ollama
  upsert_openclaw_params_env
  patch_openclaw_json
  write_polly_workspace_files
  write_backer_workspace_files
  write_polly_schema_and_db

  openclaw config validate >/dev/null
  openclaw gateway restart >/dev/null

  # Force backer bootstrap so auth/models files are created under agentDir.
  openclaw agent --agent backer --timeout 120 --message "Return HEARTBEAT_OK." >/dev/null || true

  patch_cron_jobs

  log "addendum rollout applied"
}

main "$@"
