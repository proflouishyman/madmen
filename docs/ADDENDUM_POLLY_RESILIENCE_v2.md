# ADDENDUM: Polly Resilience, Inference Architecture, and Operational Memory
## OpenClaw Implementation Guide — Addendum (Consolidated)
## Version 2.0 — April 11, 2026
## Applies on top of: OPENCLAW_IMPLEMENTATION_GUIDE_v2.md and CONTRACTS.md
## Supersedes: Addendum A v1.0, Addendum B v1.0

---

## WHAT THIS ADDENDUM FIXES

On April 11, 2026, two failures occurred simultaneously, observed in production
via Telegram:

**Failure 1 — Maxwell context overflow**
Maxwell's session JSONL bloated beyond the model's context window overnight.
The 7:01 AM morning digest fired anyway. Polly had no Maxwell data and sent a
digest containing a literal unfilled placeholder. No guard existed to catch this.

**Failure 2 — Polly went dark on a direct user query**
At 7:48 AM Louis asked: "What did Usman email me about?" Polly was silent for
31 minutes. Root cause: Polly shared the main Ollama inference queue (qwen3.5:27b)
with all other agents. Maxwell's bloated KV cache occupied the pool. Polly queued
for an inference slot and the gateway timed out before she got one.

**What was missing:**
1. No rule requiring Polly to acknowledge direct user messages
2. No rule preventing digest send with unfilled placeholders
3. No rule for graceful degradation when a mailman is down
4. No dedicated inference lane for Polly — she competed with every other agent
5. No Codex fallback when local model was unavailable
6. No queryable operational state — YAML files too slow at scale
7. Polly reading agent files directly instead of asking via ACP
8. Backer did not exist to catch Maxwell before the digest ran

This addendum closes all eight gaps.

---

## PART 1: POLLY RESILIENCE RULES

Five behavioral rules added to Polly's SOUL.md. These apply regardless of
which model Polly is running on.

---

### Rule 1 — Acknowledge direct messages within 60 seconds

When Louis sends a direct message, Polly responds within 60 seconds — even
if the answer requires querying other agents.

Acceptable: "On it — checking with Maxwell now."
Unacceptable: silence. Silence is indistinguishable from a crash.

The acknowledgment does not require the full answer. The full answer follows
when available. The acknowledgment uses Polly's fast inference path (see Part 2).

---

### Rule 2 — Never send a digest with unfilled placeholders

Before any Telegram send, Polly scans outbound text for:
```
[Insert   [Pending   [TBD   [TODO   [MISSING   [N/A — awaiting
```
If found: do NOT send. Send a degraded digest instead (Rule 3).
No exceptions. A broken digest is worse than a partial one.

---

### Rule 3 — Degrade gracefully when a mailman is down

If Maxwell or Otto does not respond to ACP within 90 seconds:
1. Do not wait. Retry once only.
2. Read last known timestamp from agent_health in polly.db.
3. Send the digest with what is available, flagging the missing section:

```
📬 Gmail summary unavailable — Maxwell did not respond.
   Last successful sweep: [timestamp from polly.db agent_health]
   Backer has been notified. This will resolve automatically.
```

Write an alert to `backer-workspace/alerts/urgent.yaml`.

---

### Rule 4 — Pre-digest health check is mandatory

The 6:50 AM health check must complete before the 7:00 AM digest fires.
The 10-minute gap enforces this. If health check reveals a failure:
1. Alert Backer via `backer-workspace/alerts/urgent.yaml`
2. Proceed with degraded digest — do not skip it
3. Include system status line at the top of the digest:
   `⚠️ System status: Maxwell DEGRADED — Gmail section unavailable`

---

### Rule 5 — Model routing for direct messages

Direct messages from Louis always go to the fast inference path first.
Heavy synthesis (Briefing Book, digest assembly, learning review) uses
the shared pool or Codex. Direct messages never wait for the shared pool.

---

### SOUL.md additions — paste at end of existing Polly SOUL.md

```markdown
## Resilience Rules

### Rule 1 — Acknowledge direct messages within 60 seconds
When Louis sends a direct message, I respond within 60 seconds.
If I need to query other agents, I acknowledge first.
Silence is never acceptable. Silence looks like a crash.
I use my fast inference path (port 11435 → Codex fallback) for acknowledgments.
I never make Louis wait while I compete for the shared pool.

### Rule 2 — Never send a digest with unfilled placeholders
Before any Telegram send, I scan output for:
  [Insert / [Pending / [TBD / [TODO / [MISSING / [N/A — awaiting
If found: I do NOT send. I send a degraded digest instead.

### Rule 3 — Degrade gracefully when a mailman is down
If Maxwell or Otto does not respond within 90 seconds:
- Retry once. Then proceed without them.
- Read last known timestamp from polly.db agent_health table
- Flag the missing section explicitly — never use a placeholder
- Write alert to backer-workspace/alerts/urgent.yaml

### Rule 4 — Pre-digest health check is mandatory
I do not send the morning digest until the 6:50 AM health check completes.
If health check reveals a failure, I degrade gracefully — I do not skip.

### Rule 5 — Model routing for direct messages
Direct messages from Louis → fast inference path (port 11435, 3s timeout → Codex)
Heavy synthesis → shared pool or Codex as normal
I never make Louis wait for an acknowledgment because the shared pool is busy.

## Acknowledgment Pattern

Every message from Louis gets an acknowledgment before I do anything.

Format (single Telegram message, within 60 seconds):
  ✅ Got it. [One sentence restatement in my own words.]

  I'm going to:
  1. [First step]
  2. [Second step]

Rules:
- Use fast inference path — never wait for shared pool
- Restate in my own words, not Louis's exact words
  If I misunderstood, Louis corrects me before I waste work
- List every planned step in order
- If plan changes mid-execution: send an update immediately
  "Update: Maxwell isn't responding. Pulling from polly.db instead."
- Skip only when trivially immediate: no agent queries, from polly.db, under 5s

Examples:

Louis: "What did Usman email me about this week?"
Polly: ✅ Got it. You want to know what Usman sent you this week.
       I'm going to:
       1. Query Maxwell for emails from Usman in the last 7 days
       2. Summarize what he said and any action he's waiting on

Louis: "Draft a reply to Ken saying I'll have chapter 3 by Friday"
Polly: ✅ Got it. You want a draft committing chapter 3 to Ken by Friday.
       I'm going to:
       1. Pull the last Ken thread from Maxwell for context
       2. Ask John to draft the reply in the right tone
       3. Present the draft for your approval — nothing sends without sign-off

Louis: "Dashboard"
Polly: ✅ Got it. Full system dashboard.
       I'm going to:
       1. Query polly.db for commitments, approvals, and waiting-ons
       2. Query agents for current escalations
       3. Compile and send the dashboard

Skip acknowledgment only when: no agent queries needed, answerable from
polly.db directly, response time under 5 seconds.
Example where skip is correct: "What time is it in Tokyo?" → "3:22 AM Saturday."
Example where skip is wrong: "Is Maxwell running?" → requires ACP ping → acknowledge.

## ACP-First Query Rule

I ask agents for information. I do not read their workspace files directly.

For any information I need from an agent: send ACP query, wait for response.
Exception — degraded mode only: if ACP times out after 90 seconds, I may read
their sweep-log.yaml directly. I flag this as degraded in any output.

My own operational state lives in polly.db. I query it directly — no ACP.
Rex owns relationships. I query Rex via ACP, not connections.db directly.

## Operational Database

My operational state lives in polly.db (SQLite, WAL mode).
Tables: tasks, commitments, waiting_on, escalations, events,
        projects, captures (FTS5), drafts, agent_health

I write to polly.db when:
- Louis gives me a task, commitment, or capture
- An agent sends me an ACP escalation
- I receive an ACP health response (updates agent_health table)
- A draft changes status

I query polly.db when:
- Assembling morning digest (Step 1 — before any ACP calls)
- Louis asks dashboard, status, or "what am I forgetting?"
- Running nag sweep
- Checking if a commitment is already tracked

I never load full YAML files into context when polly.db can answer.

Governing distinction:
  Filter / sort / count / join → polly.db
  Narrative / qualitative / reflective → MEMORY.md
  People and relationships → Rex (query via ACP)
```

---

## PART 2: INFERENCE ARCHITECTURE

### The problem

Polly's 31-minute silence was an inference queue contention failure.
Maxwell's bloated KV cache occupied the shared Ollama instance.
Polly queued for a slot and the gateway timed out.

The fix is two-layer:
1. Polly gets a dedicated small model — prewarm, always in memory, never shared
2. If that model is unavailable, Polly falls back to Codex immediately — no waiting

Louis should never experience lag on a direct message. He should never know
which model answered. He should just get a response.

---

### Inference priority chain

```
Every direct Polly message:

1. Ping port 11435 (qwen2.5:7b-instruct) — 3-second timeout
   ↓ responds within 3s → use it
   ↓ no response within 3s →

2. Switch to Codex (openai-codex/gpt-5.3-codex) — silently, this turn only
   Write alert to backer-workspace/alerts/urgent.yaml
   Backer restarts port 11435 in background
   ↓ next message: try port 11435 again

Louis sees: zero lag, zero status reports about model switching.
Backer sees: alert in urgent.yaml, takes corrective action.
```

**POLLY_FALLBACK_SILENT=true** — Louis never sees "switching to Codex."
Infrastructure problems are Backer's problem, not Louis's problem.

---

### Model selection: qwen2.5:7b-instruct for port 11435

- Full tool-calling support — required for ACP queries
- ~4GB at Q4_K_M — fits alongside qwen3.5:27b (~16GB) within 24GB RAM
- Sub-5s prefill for acknowledgment-class responses
- KEEP_ALIVE=-1 — always prewarm, never unloads

**RAM budget:**

| Component | RAM |
|---|---|
| qwen3.5:27b Q4_K_M (shared pool, port 11434) | ~16GB |
| qwen2.5:7b-instruct Q4_K_M (Polly dedicated, port 11435) | ~4GB |
| macOS + OpenClaw overhead | ~3GB |
| Headroom | ~1GB |

Backer enforces the 22GB ceiling. If exceeded, stuck primary inference is
terminated first. Polly's instance is never touched without Louis's authorization.

---

### Setup

#### Step 1 — Pull model

```bash
ollama pull qwen2.5:7b-instruct
ollama show qwen2.5:7b-instruct --modelfile | grep template
```

#### Step 2 — Create Polly Ollama config

```bash
mkdir -p ~/.ollama-polly
```

#### Step 3 — Create launchd plist

```bash
cat > ~/Library/LaunchAgents/com.ollama.polly.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
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
    <string>/Users/[username]/.ollama/models</string>
    <key>OLLAMA_KEEP_ALIVE</key>
    <string>-1</string>
    <key>OLLAMA_NUM_PARALLEL</key>
    <string>1</string>
    <key>OLLAMA_MAX_LOADED_MODELS</key>
    <string>1</string>
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

sed -i '' "s/\[username\]/$(whoami)/" \
  ~/Library/LaunchAgents/com.ollama.polly.plist
```

#### Step 4 — Load and prewarm

```bash
launchctl load ~/Library/LaunchAgents/com.ollama.polly.plist
launchctl start com.ollama.polly

# Verify
curl -s http://localhost:11435/api/tags | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print('ok:', [m['name'] for m in d['models']])"

# Prewarm — load model into memory immediately
curl -s http://localhost:11435/api/generate \
  -d '{"model":"qwen2.5:7b-instruct","prompt":"ping","stream":false}' | \
  python3 -c "import sys,json; print('prewarm ok')"
```

#### Step 5 — Add to openclaw-params.env

```bash
# ── POLLY INFERENCE ARCHITECTURE ─────────────────────────────────
POLLY_OLLAMA_URL=http://127.0.0.1:11435
POLLY_OLLAMA_MODEL=qwen2.5:7b-instruct
POLLY_LOCAL_TIMEOUT_SECONDS=3          # Ping timeout before Codex fallback
POLLY_FALLBACK_MODEL=openai-codex/gpt-5.3-codex
POLLY_FALLBACK_SILENT=true             # Never tell Louis about the switch
POLLY_DIRECT_RESPONSE_CTX=4096
POLLY_SYNTHESIS_CTX=32768
POLLY_ACK_TIMEOUT_SECONDS=60          # Max before acknowledgment required
POLLY_MAILMAN_TIMEOUT_SECONDS=90      # Max ACP wait for Maxwell/Otto
POLLY_DEGRADED_DIGEST_ALLOWED=true
```

#### Step 6 — Update openclaw.json

```json
{
  "agents": {
    "defaults": {
      "model": {
        "primary": "openai-codex/gpt-5.3-codex",
        "fallbacks": ["ollama/qwen3.5:27b"]
      }
    },
    "overrides": {
      "polly": {
        "model": {
          "primary": "ollama-polly/qwen2.5:7b-instruct",
          "fallbacks": ["openai-codex/gpt-5.3-codex"]
        },
        "timeoutSeconds": 3
      }
    }
  },
  "models": {
    "providers": {
      "ollama": {
        "baseUrl": "http://127.0.0.1:11434",
        "apiKey": "ollama-local",
        "api": "ollama"
      },
      "ollama-polly": {
        "baseUrl": "http://127.0.0.1:11435",
        "apiKey": "ollama-local",
        "api": "ollama"
      },
      "openai-codex": { "auth": "oauth" }
    }
  },
  "gateway": {
    "maxConcurrentTasks": 4
  }
}
```

---

### TOOLS.md additions — append to existing Polly TOOLS.md

```markdown
## Model Routing

### Fast inference path (every direct Louis message)
1. Ping http://127.0.0.1:11435 — 3-second timeout
2. If responds: use qwen2.5:7b-instruct for this turn
3. If timeout: switch to openai-codex/gpt-5.3-codex silently
   Write to backer-workspace/alerts/urgent.yaml
   Do NOT tell Louis. Do NOT explain the switch.

### Direct response tasks (acknowledgment, routing, status, dashboard)
Model: qwen2.5:7b-instruct via http://127.0.0.1:11435 (or Codex fallback)
Context: 4096 tokens
Temperature: 0.1

### Heavy synthesis tasks (digest, Briefing Book, learning review)
Model: openai-codex/gpt-5.3-codex (primary) or qwen3.5:27b (fallback)
Context: 32768 tokens

### Invariant
Louis never waits because of an infrastructure problem.
He gets a response. Backer gets an alert. That is the correct division of labor.

## ACP Query Library

### Health and status

```bash
# Agent health (replaces direct sweep-log.yaml reads)
openclaw acp --from polly --to maxwell --auth "$ACP_TOKEN" \
  --message '{"query_type":"health_status"}'
# Returns: last_success_at, status, session_size_bytes, items_since_last_sweep

openclaw acp --from polly --to otto --auth "$ACP_TOKEN" \
  --message '{"query_type":"health_status"}'

openclaw acp --from polly --to [agent] --auth "$ACP_TOKEN" \
  --message '{"query_type":"today_summary"}'
```

### Email queries (always via mailmen — never direct)

```bash
# Email from a person
openclaw acp --from polly --to maxwell --auth "$ACP_TOKEN" \
  --message '{
    "query_type": "email_search",
    "sender_contains": "[name or domain]",
    "days_back": 7,
    "return_fields": ["date","subject","body_preview","replied"]
  }'

# Overnight summary
openclaw acp --from polly --to maxwell --auth "$ACP_TOKEN" \
  --message '{"query_type":"overnight_summary"}'

# Unanswered threads
openclaw acp --from polly --to maxwell --auth "$ACP_TOKEN" \
  --message '{"query_type":"unanswered_threads","min_urgency":"priority"}'
```

### Domain agent queries

```bash
openclaw acp --from polly --to finn   --auth "$ACP_TOKEN" --message '{"query_type":"today_summary"}'
openclaw acp --from polly --to weber  --auth "$ACP_TOKEN" --message '{"query_type":"upcoming_deadlines","days_ahead":7}'
openclaw acp --from polly --to john   --auth "$ACP_TOKEN" --message '{"query_type":"project_status"}'
openclaw acp --from polly --to emma   --auth "$ACP_TOKEN" --message '{"query_type":"project_status"}'
openclaw acp --from polly --to rex    --auth "$ACP_TOKEN" --message '{"query_type":"contact_lookup","name":"[name]"}'
openclaw acp --from polly --to [agent] --auth "$ACP_TOKEN" --message '{"query_type":"pending_escalations"}'
```

## polly.db — Direct Access

Path: ~/.openclaw/workspaces/polly-workspace/polly.db
Mode: WAL — safe for concurrent reads during Backer checkpoint
Access: sqlite3 CLI or Python sqlite3 module

I query and write polly.db directly. No other agent has write access.

Key queries:
```python
import sqlite3, os
DB = os.path.expanduser("~/.openclaw/workspaces/polly-workspace/polly.db")

# Overdue committed items
sqlite3.connect(DB).execute("""
  SELECT id, description, to_whom, due FROM commitments
  WHERE status='open' AND obligation='committed' AND due < date('now')
  ORDER BY due ASC""").fetchall()

# Pending drafts by tier
sqlite3.connect(DB).execute("""
  SELECT id, created_by, type, to_recipient, subject, approval_tier, created_at
  FROM drafts WHERE status='pending_approval'
  ORDER BY CASE approval_tier WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
           created_at ASC""").fetchall()

# Open urgent escalations
sqlite3.connect(DB).execute("""
  SELECT id, from_agent, type, summary, created_at FROM escalations
  WHERE status='pending'
  ORDER BY CASE type WHEN 'urgent' THEN 1 WHEN 'priority' THEN 2 ELSE 3 END,
           created_at ASC""").fetchall()

# Full-text capture search
sqlite3.connect(DB).execute("""
  SELECT c.id, c.text, c.category, c.captured_at
  FROM captures c JOIN captures_fts fts ON c.id = fts.id
  WHERE captures_fts MATCH ? AND c.status='open'
  ORDER BY rank""", (query_text,)).fetchall()

# Agent health
sqlite3.connect(DB).execute("""
  SELECT last_success_at, last_status, session_size_kb
  FROM agent_health WHERE agent_id=?""", (agent_id,)).fetchone()
```
```

---

## PART 3: POLLY'S OPERATIONAL DATABASE

### Schema

```sql
-- ~/.openclaw/workspaces/polly-workspace/polly.db
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── TASKS ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    owner_agent     TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    due             DATE,
    status          TEXT DEFAULT 'open',        -- open|in_progress|waiting_on|done|cancelled
    obligation      TEXT DEFAULT 'committed',   -- committed|provisional
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

-- ── COMMITMENTS ──────────────────────────────────────────────────
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

-- ── WAITING-ONS ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS waiting_on (
    id              TEXT PRIMARY KEY,
    description     TEXT NOT NULL,
    from_whom       TEXT NOT NULL,
    context         TEXT,
    since           DATETIME NOT NULL,
    due             DATE,
    followup_rule   TEXT,
    obligation      TEXT DEFAULT 'committed',
    status          TEXT DEFAULT 'open',        -- open|received|overdue|cancelled
    tracking_agent  TEXT,
    last_checked    DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_waiting_status    ON waiting_on(status);
CREATE INDEX IF NOT EXISTS idx_waiting_from_whom ON waiting_on(from_whom);
CREATE INDEX IF NOT EXISTS idx_waiting_since     ON waiting_on(since);

-- ── ESCALATIONS ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS escalations (
    id              TEXT PRIMARY KEY,
    from_agent      TEXT NOT NULL,
    type            TEXT NOT NULL,              -- urgent|priority|fyi
    summary         TEXT NOT NULL,
    source_object   TEXT,
    status          TEXT DEFAULT 'pending',     -- pending|acknowledged|resolved
    polly_batched   INTEGER DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_esc_status     ON escalations(status);
CREATE INDEX IF NOT EXISTS idx_esc_type       ON escalations(type);
CREATE INDEX IF NOT EXISTS idx_esc_from_agent ON escalations(from_agent);

-- ── EVENTS ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,              -- meeting|travel|deadline
    title           TEXT NOT NULL,
    datetime        DATETIME NOT NULL,
    participants    TEXT,
    prep_required   INTEGER DEFAULT 0,
    prep_notes      TEXT,
    linked_task     TEXT,
    obligation      TEXT DEFAULT 'committed',
    status          TEXT DEFAULT 'upcoming',    -- upcoming|in-progress|completed|cancelled
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_datetime ON events(datetime);
CREATE INDEX IF NOT EXISTS idx_events_status   ON events(status);

-- ── PROJECTS ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    owner_agent     TEXT NOT NULL,
    status          TEXT DEFAULT 'active',      -- active|paused|completed|dropped
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

-- ── CAPTURES ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS captures (
    id              TEXT PRIMARY KEY,
    captured_at     DATETIME NOT NULL,
    text            TEXT NOT NULL,
    category        TEXT,                       -- idea|task|worry|reference|person|project
    status          TEXT DEFAULT 'open',        -- open|acted_on|archived
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

-- ── DRAFTS ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS drafts (
    id              TEXT PRIMARY KEY,
    created_by      TEXT NOT NULL,
    type            TEXT NOT NULL,              -- email|slack|social|reddit|hn|canvas
    to_recipient    TEXT,
    subject         TEXT,
    body_file       TEXT,
    status          TEXT DEFAULT 'pending_approval',
    approval_tier   TEXT,                       -- low|medium|high
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

-- ── AGENT HEALTH ─────────────────────────────────────────────────
-- Polly's view of each agent's last known run state
-- Written by Polly from ACP responses; read by digest, dashboard, degraded mode
CREATE TABLE IF NOT EXISTS agent_health (
    agent_id        TEXT PRIMARY KEY,
    last_success_at DATETIME,
    last_status     TEXT,                       -- ok|failed|timeout|unknown
    last_error      TEXT,
    session_size_kb INTEGER,
    items_processed INTEGER,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### Initialization

```bash
# Save schema
cat > ~/.openclaw/workspaces/polly-workspace/polly-schema.sql << 'SCHEMA'
[paste schema above]
SCHEMA

# Initialize
sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db \
  < ~/.openclaw/workspaces/polly-workspace/polly-schema.sql

# Verify WAL
sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db "PRAGMA journal_mode;"
# Expected: wal
```

### Migration — run once after initialization

```bash
openclaw agent --agent polly \
  --model-params '{"model":"openai-codex/gpt-5.3-codex","num_ctx":16384}' \
  --message "Migrate existing YAML state files into polly.db.

Read each file:
  polly-workspace/state/tasks.yaml
  polly-workspace/state/commitments.yaml
  polly-workspace/state/waiting_on.yaml
  polly-workspace/state/escalations.yaml
  polly-workspace/state/capture.yaml

For each record, INSERT OR IGNORE into the corresponding polly.db table.
Report counts after migration:
  SELECT COUNT(*) FROM tasks;
  SELECT COUNT(*) FROM commitments;
  SELECT COUNT(*) FROM waiting_on;
  SELECT COUNT(*) FROM escalations;
  SELECT COUNT(*) FROM captures;

Rename each YAML file to [filename].yaml.archive. Do not delete."
```

### What lives where

| Data | Location | Rationale |
|---|---|---|
| Tasks, commitments, waiting-ons, escalations, events, projects | polly.db | Operational — needs querying |
| Captures | polly.db | Needs full-text search |
| Drafts log | polly.db | Cross-agent query by status/tier |
| Agent health | polly.db | Degraded mode decisions |
| Contact/relationship data | Rex's connections.db | Rex owns people |
| Reflective notes, preferences | MEMORY.md | Qualitative — not row-shaped |
| Agent config files | Workspace files | Config |

---

## PART 4: BACKER — DUAL INSTANCE MANAGEMENT

Backer owns both Ollama instances. No other agent may restart either.

### SOUL.md — Ollama section (replace in backer-workspace/SOUL.md)

```markdown
## Ollama Lifecycle Management

I own both Ollama instances. No other agent may restart either.

### Primary instance — port 11434
Serves all agents except Polly.
Health check: every 5 minutes.
Restart trigger: no response in 10 seconds on two consecutive checks.

### Polly instance — port 11435
Serves Polly only. Treated as higher priority than primary.
Health check: every 5 minutes, staggered 2.5 minutes from primary.
Restart trigger: no response in 10 seconds on two consecutive checks.
After any restart: send prewarm ping, verify model loaded within 30 seconds.
Prewarm failure = P1 alert to Louis immediately.
KEEP_ALIVE=-1 — model must always be in memory.

### Restart sequences

```bash
# Primary
launchctl stop homebrew.mxcl.ollama && sleep 5 && launchctl start homebrew.mxcl.ollama

# Polly
launchctl stop com.ollama.polly && sleep 5 && launchctl start com.ollama.polly
sleep 30
# Prewarm verify
curl -s --max-time 30 http://localhost:11435/api/generate \
  -d '{"model":"qwen2.5:7b-instruct","prompt":"ping","stream":false}'
```

### RAM pressure
Budget: 20GB for Ollama (16GB primary + 4GB Polly)
If system RAM > 22GB:
1. Terminate stuck inference in primary pool first
2. If still > 22GB: alert Louis — do not touch Polly instance without authorization

### Alert inbox
I check backer-workspace/alerts/urgent.yaml on every health check.
I process uncleared alerts, attempt the fix, mark cleared=true with outcome.
```

### TOOLS.md — Ollama section (replace in backer-workspace/TOOLS.md)

```markdown
## Ollama Tools

```bash
# Health checks
curl -s --max-time 10 http://localhost:11434/api/tags   # primary
curl -s --max-time 10 http://localhost:11435/api/tags   # polly

# Prewarm verify (after Polly restart)
curl -s --max-time 30 http://localhost:11435/api/generate \
  -d '{"model":"qwen2.5:7b-instruct","prompt":"ping","stream":false}'

# Restart primary
launchctl stop homebrew.mxcl.ollama && sleep 5 && launchctl start homebrew.mxcl.ollama

# Restart Polly
launchctl stop com.ollama.polly && sleep 5 && launchctl start com.ollama.polly

# Log paths
# Primary: /tmp/ollama.log
# Polly: /tmp/ollama-polly.log and /tmp/ollama-polly-error.log
```
```

### Updated Backer health cron

```bash
openclaw cron add --agent backer \
  --name "backer-health-5m" \
  --at "*/5 * * * *" \
  --message "Run infrastructure health check:
1. Ping primary Ollama (port 11434) — restart if unresponsive twice
2. Ping Polly Ollama (port 11435) — restart if unresponsive twice;
   after restart, send prewarm ping within 30s;
   if prewarm fails: alert Louis immediately (P1)
3. Check backer-workspace/alerts/urgent.yaml — process any uncleared alerts
4. Read sweep-log.yaml for all agents — check last successful run timestamps
5. Check session JSONL size for all agents — trim/reset per CONTRACTS.md
6. Check Mac Mini RAM — if over 22GB, terminate stuck primary inference;
   never touch Polly instance without Louis authorization
7. Log all actions to backer-workspace/logs/$(date +%Y-%m-%d).log
8. Alert Louis only if: fix failed, auth expiry, 3+ consecutive failures,
   or Polly prewarm failed"
```

### Backer daily audit additions

```bash
# Add to backer-daily-audit cron:
# polly.db integrity
sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db "PRAGMA integrity_check;"
# WAL checkpoint
sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

### Nightly backup addition

```bash
# Add to existing nightly backup cron alongside Rex checkpoint:
sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

---

## PART 5: NEW AND UPDATED FILES — COMPLETE LIST

### New files

| File | Created by | Purpose |
|---|---|---|
| `~/Library/LaunchAgents/com.ollama.polly.plist` | Setup (Part 2) | Polly dedicated Ollama instance |
| `~/.openclaw/workspaces/polly-workspace/polly-schema.sql` | Setup (Part 3) | Database schema |
| `~/.openclaw/workspaces/polly-workspace/polly.db` | sqlite3 init | Operational state database |
| `~/.openclaw/workspaces/backer-workspace/alerts/urgent.yaml` | Setup | Polly-to-Backer alert inbox |

Initialize urgent.yaml as empty list:
```yaml
# Written by Polly and agents when self-detected failures occur
# Read and cleared by Backer on every health check
[]
```

### Updated files

| File | Change |
|---|---|
| `~/.openclaw/workspaces/polly-workspace/SOUL.md` | Append all sections from Part 1 |
| `~/.openclaw/workspaces/polly-workspace/TOOLS.md` | Append Model Routing + ACP Query Library + polly.db from Part 2 |
| `~/.openclaw/workspaces/backer-workspace/SOUL.md` | Replace Ollama section with Part 4 version |
| `~/.openclaw/workspaces/backer-workspace/TOOLS.md` | Replace Ollama section with Part 4 version |
| `~/.openclaw/openclaw-params.env` | Add Polly inference block from Part 2 Step 5 |
| `~/.openclaw/openclaw.json` | Replace with version from Part 2 Step 6 |

### Updated crons

| Agent | Cron | Change |
|---|---|---|
| Backer | backer-health-5m | Updated to check both Ollama instances + process alerts |
| Backer | backer-daily-audit | Add polly.db integrity check and WAL checkpoint |
| Polly | polly-morning-digest | Step 1 queries polly.db before ACP; Step 3 uses agent_health table |

### Updated morning digest cron

```bash
openclaw cron add --agent polly \
  --name "polly-morning-digest" \
  --at "0 7 * * *" \
  --model-params '{"num_ctx":32768,"temperature":0.1}' \
  --message "Assemble morning digest.

Step 1 — Query polly.db (no ACP):
  Open urgent escalations: SELECT FROM escalations WHERE type='urgent' AND status='pending'
  Overdue commitments: SELECT FROM commitments WHERE due < today AND status='open'
  Pending drafts: SELECT FROM drafts WHERE status='pending_approval'
  Overdue waiting-ons: SELECT FROM waiting_on WHERE due < today AND status='open'
  Agent health: SELECT FROM agent_health for all 16 agents

Step 2 — ACP queries (90-second timeout each):
  Maxwell: overnight_summary
  Otto: overnight_summary
  Finn: today_summary
  Weber: upcoming_deadlines (48h)
  John: project_status
  Emma: project_status

Step 3 — For any agent that timed out: read last_success_at from polly.db
  agent_health. Use that timestamp in degraded section. No [Insert] placeholders.

Step 4 — Assemble. Max 20 lines. Include system status line if any agent degraded.
  Scan output for placeholder strings. If found: send degraded version instead.

Step 5 — Send via Telegram.
  Update polly.db: set escalations polly_batched=1 for all included escalations.
  Update agent_health table with ACP response timestamps."
```

---

## PART 6: VERIFICATION CHECKLIST

```
POLLY INFERENCE ARCHITECTURE
[ ] com.ollama.polly plist loaded: launchctl list | grep com.ollama.polly
[ ] qwen2.5:7b-instruct in model list: curl http://localhost:11435/api/tags
[ ] Prewarm confirmed: ping response under 5s
[ ] KEEP_ALIVE=-1 in plist EnvironmentVariables
[ ] openclaw.json has ollama-polly provider at port 11435 with timeoutSeconds: 3
[ ] openclaw-params.env has all POLLY_ variables including POLLY_LOCAL_TIMEOUT_SECONDS=3
[ ] Test fallback: stop com.ollama.polly, send Polly a message, verify Codex responds
[ ] Test silent fallback: verify Louis sees no "switching to Codex" message
[ ] Test recovery: Backer restarts port 11435, next message uses local model again

POLLY RESILIENCE RULES
[ ] Polly SOUL.md has ## Resilience Rules (all 5 rules)
[ ] Polly SOUL.md has ## Acknowledgment Pattern with examples
[ ] Polly SOUL.md has ## ACP-First Query Rule
[ ] Polly SOUL.md has ## Operational Database
[ ] Test Rule 1: send direct message, verify acknowledgment under 60s
[ ] Test Rule 2: inject [Insert placeholder into digest draft, verify Polly blocks send
[ ] Test Rule 3: simulate Maxwell ACP timeout, verify degraded digest with timestamp
[ ] Test Rule 4: verify 6:50 AM health check fires before 7:00 AM digest
[ ] Test acknowledgment: verify 2-part response (restatement + intent list)
[ ] Test acknowledgment: verify restatement is in Polly's own words
[ ] Test skip rule: "What time is it in Tokyo?" → no acknowledgment, just answer

POLLY DATABASE
[ ] polly-schema.sql exists in polly-workspace/
[ ] polly.db WAL mode: sqlite3 polly.db "PRAGMA journal_mode;" → wal
[ ] All tables: sqlite3 polly.db ".tables"
    Expected: agent_health captures captures_fts commitments drafts
              escalations events project_commitments projects tasks waiting_on
[ ] Migration complete: .yaml.archive files in polly-workspace/state/
[ ] Migration counts correct (all tables populated)
[ ] Backer daily audit includes polly.db integrity_check
[ ] Nightly backup includes polly.db WAL checkpoint
[ ] Test: INSERT test row, SELECT it, DELETE it

ACP-FIRST QUERY RULE
[ ] Polly TOOLS.md has ## ACP Query Library
[ ] Test: "Is Maxwell running?" → ACP query sent, not file read
[ ] Test: Maxwell ACP timeout → sweep-log.yaml fallback + degraded flag in output

BACKER DUAL INSTANCE
[ ] backer-workspace/alerts/urgent.yaml initialized as empty list
[ ] Backer SOUL.md has updated dual-instance Ollama section
[ ] Backer TOOLS.md has both port 11434 and 11435 commands
[ ] Backer health cron checks both instances and processes alerts/urgent.yaml
[ ] Test: stop com.ollama.polly, verify Backer restarts within 10 minutes
[ ] Test: verify prewarm ping sent after Polly restart
[ ] Test: prewarm failure → verify P1 alert to Louis

SMOKE TEST ADDITIONS
[ ] Add to smoke-test.sh:
    check "Polly Ollama running" \
      "curl -s --max-time 5 http://localhost:11435/api/tags | \
       python3 -c 'import sys,json; d=json.load(sys.stdin); print(\"ok\")'" "ok"
    check "Polly prewarm" \
      "curl -s --max-time 15 http://localhost:11435/api/generate \
       -d '{\"model\":\"qwen2.5:7b-instruct\",\"prompt\":\"ping\",\"stream\":false}' | \
       python3 -c 'import sys,json; print(\"ok\")'" "ok"
    check "polly.db healthy" \
      "sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db \
       'PRAGMA integrity_check;'" "ok"
    check "Backer alerts inbox exists" \
      "test -f ~/.openclaw/workspaces/backer-workspace/alerts/urgent.yaml && echo ok" "ok"
```

---

## PART 7: SOLUTIONS.MD ENTRY

```
[2026-04-11] - Polly Resilience Architecture (Consolidated)

Problem
1. Maxwell context overflow → Polly sent digest with unfilled placeholder at 7:01 AM
2. Polly silent for 31 minutes on direct query at 7:48 AM (inference queue contention)
3. No acknowledgment pattern — Louis couldn't distinguish crash from lag
4. YAML state files inadequate for scale — no queryable operational memory
5. Polly reading agent workspace files directly — no audit trail, no agent awareness

Root Cause
Maxwell's session JSONL bloated beyond model context window. Polly shared the main
Ollama inference queue with all agents. Maxwell's bloated KV cache occupied the pool.
Polly had no fast path, no fallback, no guard against sending broken output, and no
behavioral rule requiring it to acknowledge receipt of messages.

Solution — Consolidated Addendum
1. Five resilience rules: acknowledge within 60s, no placeholder sends, graceful
   degradation, mandatory pre-digest health check, model routing for direct messages
2. Acknowledgment pattern: 2-part response (restatement + intent list) within 60s;
   restatement in Polly's own words catches misunderstandings before work begins
3. Dedicated Ollama instance for Polly (port 11435, qwen2.5:7b-instruct, KEEP_ALIVE=-1)
4. 3-second fallback to Codex when port 11435 unavailable — silent, no Louis notification
5. polly.db: SQLite operational database replacing YAML; 9 tables including agent_health;
   WAL mode; Backer owns integrity check and checkpoint
6. ACP-first query rule: Polly asks agents via ACP, never reads their files directly;
   90-second degraded-mode fallback to sweep-log.yaml flagged in output
7. Backer extended to own and monitor both Ollama instances; urgent.yaml alert inbox

Notes
qwen2.5:7b-instruct confirmed to support tool calling.
RAM: 16GB + 4GB + 3GB overhead = 23GB — within 24GB limit.
Polly instance prewarm failure = P1 — Backer alerts Louis immediately.
YAML files archived not deleted — 30-day migration window.
Morning digest now queries polly.db in Step 1 before any ACP calls.
```

---

*OpenClaw v2.8 — Consolidated Addendum v2.0*
*Supersedes Addendum A v1.0 and Addendum B v1.0*
*Applies on top of: OPENCLAW_IMPLEMENTATION_GUIDE_v2.md and CONTRACTS.md*
*Owner: Louis Hyman, JHU*
*Last updated: April 11, 2026*
