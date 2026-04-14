# OpenClaw: How It Should Work

**Technical Reference — April 2026**

This document describes the intended architecture of OpenClaw and the correct patterns for every layer of the system. When fixing bugs, always verify the fix against this document to ensure you're addressing root causes, not symptoms.

---

## 0. Governing Principle: Use the Right Layer

**The single most important rule in this system:**

> LLM agent turns are for reasoning. Deterministic work runs outside the gateway.

Every time work is routed through an agent turn unnecessarily, you inherit the full cost of LLM inference: model loading latency, exec-parameter generation risk, event loop contention, session lock accumulation, and timeout/retry failure modes. None of that is acceptable for work that doesn't require judgment.

**The test**: before adding or keeping a cron job as an `agentTurn`, ask:

1. Does this work require the model to *reason* about something — synthesize, judge, prioritize, compose?
   → Agent turn is appropriate.

2. Does this work execute a fixed script, write a file, run a SQL query, or call a known API with no decisions?
   → Use launchd directly. The agent should not be involved.

**The two layers:**

| Layer | What it's for | Examples |
|-------|--------------|---------|
| **launchd / cron** | Deterministic pipeline work — always the same script, always the same args | `polly_ingest.py`, `maxwell_ingest.py`, `backer_health_tick.sh`, `rex_sync_contacts.py` |
| **OpenClaw agent turns** | Reasoning work — judgment, synthesis, ACP delegation, Telegram responses | Morning digest assembly, Gmail sweep + classification, Slack summarization, relationship queries |

**Why this was violated historically**: OpenClaw's cron system is convenient — one config file, automatic retry, session isolation. It's tempting to use it for everything. But the gateway is a single-threaded async event loop. Blocking it with mechanical script execution during every 20-minute cycle creates contention that starves all reasoning work. The ingest scripts (`polly_ingest.py`, `maxwell_ingest.py`) were originally added as agent turns because the cron mechanism was already there, and the failure modes only became visible at production load.

---

## 1. Architecture Overview

OpenClaw is a multi-agent orchestration framework running locally on macOS. It has **four** execution contexts:

- **launchd agents** — deterministic scripts (file I/O, SQLite writes, shell tasks) run directly by macOS, no gateway involvement. These are the correct layer for data pipeline work.
- **Telegram turns** — user sends a message to an agent; the agent responds in real time using LLM reasoning.
- **Cron jobs** — the OpenClaw scheduler fires a message to an agent on a schedule, in an isolated session. Use only for work that requires reasoning.
- **ACP delegation** — one agent sends a structured message to another agent (e.g. Polly delegates to Maxwell).

Each agent has a workspace directory under `~/.openclaw/workspaces/<agent>-workspace/` containing:
- `SOUL.md` — identity, mission, hard rules (loaded as system context on bootstrap)
- `TOOLS.md` — tool usage rules (loaded as system context on bootstrap)
- `MEMORY.md` or `state/` — persistent working memory

---

## 2. Bootstrap Context and the Compaction Problem

Every agent session (both Telegram and cron) fires an `openclaw:bootstrap-context:full` event before processing the first message. This event loads `SOUL.md` and `TOOLS.md` into the model's context.

**Critical behavior**: After each turn, OpenClaw compacts the conversation to "No prior history." This means every turn starts completely fresh — the model has no memory of previous tool calls or outputs.

**What this means in practice:**
- For Telegram: the model sees SOUL.md + TOOLS.md + the user's message, nothing else.
- For cron: the model sees SOUL.md + TOOLS.md + the cron payload message, nothing else.

---

## 3. Model Tiers and Their Behavior

OpenClaw routes to three Ollama lanes:

| Lane | Port | Model | Used For |
|------|------|-------|----------|
| `ollama` (primary) | 11434 | gemma4:26b | Rich tasks: morning digest, Slack digest, Gmail sweep |
| `ollama-polly` | 11435 | qwen2.5:7b-instruct | Polly Telegram responses |
| `ollama-light` | 11436 | qwen2.5:7b | Lightweight cron ticks |

**The qwen2.5:7b reliability gap**: Small models (qwen2.5:7b) are heavily influenced by the USER MESSAGE. When system context (SOUL.md/TOOLS.md) instructs them to call exec with specific parameters, they often ignore those instructions if the user message doesn't explicitly command the same thing. Specifically:

- A message saying "Sitrep" will cause qwen2.5:7b to hallucinate a response with zero tool calls.
- A message saying "Execute exactly this command via exec: `...`" will reliably cause a tool call.

gemma4:26b is more instruction-following and handles descriptive cron messages better, but is too slow (>26B params) for high-frequency ticks.

**Rule**: Any cron job using `ollama-light/qwen2.5:7b` MUST use a deterministic exec message (exact command spelled out). Descriptive instructions are only acceptable for `ollama/gemma4:26b`.

---

## 4. Exec Parameter Rules

Every `exec` tool call MUST use these parameters:

```
ask: "off"        — required; bypasses the approval gateway
host: "gateway"   — required for Otto (default for all others)
```

These parameters MUST NOT appear:

```
elevated: true         — not enabled system-wide; will always fail
security: "allowlist"  — validates command against TOOLS.md allowlist, fails if not listed
host: "sandbox"        — sandbox mode is disabled
```

**Why they fail**: `elevated: true` requests permissions that aren't granted at the system level. `security: "allowlist"` causes OpenClaw to check whether the exact command string appears in TOOLS.md under a specific allowlist section — any variation fails. `host: "sandbox"` routes to a disabled sandbox runtime.

**Where these rules live**: Top of every agent's `TOOLS.md`, under `## Exec Rules — READ FIRST`. This is the highest-visibility position for the model.

**For qwen2.5:7b crons specifically**: The rules must ALSO appear in the cron message payload itself, because small models deprioritize system context when the user message is clear.

---

## 5. The Sitrep Fabrication Problem and Its Fix

**Problem**: When a user sends "Sitrep" to Polly via Telegram, Polly receives:
- System context: SOUL.md (with hard rules to query polly.db) + TOOLS.md
- User message: "Sitrep"

The qwen2.5:7b model generates a plausible-sounding status update from its training data rather than calling exec to query polly.db. The output looks real but contains fabricated events, tasks, and agent statuses.

**Root cause**: Small models running on short context windows prioritize the conversational pattern ("Sitrep" → respond with a status summary) over system context instructions to use tools.

**Fix**: `polly_ingest.py` now calls `write_sitrep_cache()` at the end of every 20-minute ingest run. This function queries polly.db and writes the results directly into the `<!-- LIVE_STATUS_START --> ... <!-- LIVE_STATUS_END -->` block at the bottom of `SOUL.md`. When Polly responds to "Sitrep," the live data is already in her system context — no tool call needed.

**Correct behavior after fix**:
1. `com.openclaw.polly-ingest` launchd agent fires every 20 minutes → `polly_ingest.py --verbose` executes → `write_sitrep_cache()` updates SOUL.md with live db data.
2. User sends "Sitrep" → Polly reads SOUL.md system context → Live Status block contains real data → Polly responds with accurate status.

---

## 6. When to Use OpenClaw Cron vs. launchd

**First, ask whether the job belongs in the gateway at all** (see §0). If it executes a fixed script with no reasoning, it should be a launchd agent, not an OpenClaw cron job.

For jobs that legitimately belong in the gateway (reasoning required), use the correct message pattern for the model tier:

### ✅ Correct — reasoning task, descriptive message (gemma4:26b only)

```
Read CES Slack channels for the past 24 hours. Identify any threads needing
Louis's input. Send 3-bullet summary to Polly via acp. Only escalate if
something genuinely needs Louis — do not create noise.
```

### ✅ Correct — gateway cron for a script call, deterministic message (qwen2.5:7b)

Used only when a script invocation genuinely must be coordinated through the agent
(e.g., it needs to report results back via ACP, or depends on agent state). Rare.

```
Execute exactly this command once via exec with ask:"off" ONLY
(do not use elevated, security:allowlist, or any other exec parameters).
Do not call cron tools. Return ONLY the command stdout with no edits or commentary:
/path/to/script.sh
```

### ❌ Wrong — deterministic pipeline work routed through agent turn

```
# DO NOT do this for polly_ingest.py, maxwell_ingest.py, backer_health_tick.sh,
# rex_sync_contacts.py, or any other script that runs the same way every time.
# Use launchd instead.
Execute exactly this command once via exec: python3 polly_ingest.py --verbose
```

### ❌ Wrong — descriptive message to qwen2.5:7b

```
Run daily infrastructure audit.
1. Validate polly.db integrity: PRAGMA integrity_check.
2. Checkpoint polly.db WAL: PRAGMA wal_checkpoint(TRUNCATE).
```
This will cause the model to either hallucinate output or use wrong exec params.

**Decision tree for new scheduled work:**

```
Does the work require LLM reasoning?
  NO  → launchd plist (StartInterval or StartCalendarInterval)
  YES → Does it need to run more than once per hour?
          YES → Consider whether the frequency is appropriate for an agent turn
          NO  → OpenClaw cron job with gemma4:26b (descriptive) or qwen2.5:7b (deterministic)
```

---

## 7. Agent Roles and Correct Behavior

### Polly — chief-of-staff
- **Telegram responses**: reads Live Status block from SOUL.md, no tool calls needed for status
- **Morning digest** (`polly-morning-digest`, 7am, gemma4:26b): queries polly.db via exec, reads maxwell/otto intake files, sends Telegram digest
- **Pre-digest health check** (`polly-pre-digest-healthcheck`, 6:50am, qwen2.5:7b): runs gateway health checks via exec; should use deterministic message
- **Ingestion watch** (`ingestion-watch-20m`): **DISABLED** — replaced by `com.openclaw.polly-ingest` launchd agent (see §22)

### Maxwell — Gmail intake
- **gmail-sweep-5m** (every 30m, gemma4:26b): reads Gmail via `gog` tool, writes `gmail-intake-latest.json`
- **gmail-backfill-12m-20m** (every 20m, qwen2.5:7b): deterministic exec of backfill script
- **maxwell-gcal-6am** (daily 6:05am, qwen2.5:7b): deterministic exec of gcal tick script
- **maxwell-ingest-30m**: **DISABLED** — replaced by `com.openclaw.maxwell-ingest` launchd agent (see §22)

### Otto — Outlook + Slack intake
- **otto-outlook-sweep** (hourly 8am-6pm weekdays, qwen2.5:7b): deterministic exec of `otto_outlook_sweep.sh` — *candidate for launchd migration*
- **otto-calendar-6am** (daily 6am, qwen2.5:7b): deterministic exec of `otto_calendar_tick.sh` — *candidate for launchd migration*
- **otto-slack-digest** (9am weekdays, gemma4:26b): reads Slack via curl, summarizes, sends to Polly via ACP — **reasoning task; correct as agent turn**
- **otto-draft-check** (6pm weekdays, qwen2.5:7b): deterministic exec osascript counting Outlook drafts — *candidate for launchd migration*
- **otto-outcome-sweep** (4:30pm Fridays, gemma4:26b): AppleScript search + reply tracking judgment — **reasoning task; correct as agent turn**

### Rex — Contacts database
- **rex-contacts-sync-6h** (every 6h, qwen2.5:7b): deterministic exec of `rex_sync_contacts.py` — *candidate for launchd migration*
- **rex-backfill-365d-20m** (every 20m, qwen2.5:7b): deterministic exec of same script — *candidate for launchd migration*

### Backer — Infrastructure health
- **backer-health-5m** (every 5m, qwen2.5:7b): deterministic exec of `backer_health_tick.sh` — *candidate for launchd migration*
- **backer-daily-audit** (2am, qwen2.5:7b): deterministic exec of sqlite3 integrity check + WAL checkpoint — *candidate for launchd migration*
- **backer-nightly-backup** (2:40am, qwen2.5:7b): WAL checkpoint + runs `backer_backup_tick.sh` → dated backup copies of polly.db and runs.sqlite — *candidate for launchd migration*

**Note on "candidates for launchd migration"**: These jobs are deterministic script invocations with no reasoning requirement. They follow the same anti-pattern that was fixed for `ingestion-watch-20m` and `maxwell-ingest-30m`. They are currently stable (the exec parameter problem is contained by explicit `ask:"off"` in their cron messages), but each one unnecessarily occupies an event loop slot. Migrating them to launchd is the correct long-term direction. Prioritize by run frequency: `backer-health-5m` (every 5m) has the highest contention cost and is the most valuable to migrate next.

### Forge — Coding operations
- **On-demand only** (no cron)
- Does exec-heavy git/test work; must always use `ask: "off"`, never `elevated: true`

### Lex, Trip, Spark, Balt, Uhura, Finn, Prof
- **On-demand only**, no cron jobs
- Lex: research briefs (no exec)
- Trip: Concur/travel — AppleScript browser flows; approval required before any submission
- Spark: experiment tracking — no production writes without approval
- Balt: reliability monitoring — reads from Polly/agents, no outbound
- Uhura: communications drafts — never posts autonomously
- Finn: scheduling coordination — no outbound without approval
- Prof: teaching/academic — no gradebook writes without approval

---

## 8. polly.db Schema Reference

Located at `~/.openclaw/workspaces/polly-workspace/polly.db`. WAL mode, concurrent reads safe.

| Table | Key Columns | Purpose |
|-------|------------|---------|
| `events` | datetime, title, type, participants, prep_required, status | Calendar events |
| `agent_health` | agent_id, last_status, last_error, updated_at | Agent run status |
| `escalations` | type, summary, status, created_at | Items requiring Louis's attention |
| `tasks` | title, due, status | Open action items |
| `drafts` | status, content, created_at | Pending approval drafts |
| `commitments` | — | Tracked commitments |
| `waiting_on` | — | Items waiting for third-party response |
| `captures` | — | Inbox captures |
| `projects` | — | Project tracking |
| `email_threads` | id, source, from_email, subject, is_direct, reply_needed, topic_tags, connection_id | Per-thread email memory (Gmail + Outlook) |
| `thread_items` | id, thread_id, item_type, text, owner, due_date | Action items / commitments extracted from threads |
| `contact_signals` | from_email, from_name, connection_id, total_threads, direct_threads, open_reply_threads, last_email_at | Per-sender communication aggregates |

### Email memory layer

`email_threads`, `thread_items`, and `contact_signals` are populated by `maxwell_ingest.py` (runs every 30 minutes via the `com.openclaw.maxwell-ingest` launchd agent). These tables are the email signal layer for relationship intelligence.

`is_direct` classification uses Gmail's `CATEGORY_PERSONAL` label (most reliable) + FROM address heuristics. Only direct threads get full body fetched (max 8 per run, 12s timeout, via `gog gmail messages search "thread:ID"`). Newsletters/lists get snippet only.

`contact_signals` provides per-sender aggregates without scanning all threads — use this for "how often do I hear from X" queries.

`connection_id` links back to Rex's `connections.db` where a match is found by email address in the connections notes field.

---

## 9. Rex as Relationship Intelligence Layer

Rex is the canonical interface for "what's going on with [person/org]?" queries. Polly delegates these to Rex via ACP rather than querying email tables directly.

**Rex query pattern** (4 steps, all via `sqlite3` exec):
1. Search `connections.db` for the contact by name
2. Join to `polly.db` `email_threads` on `from_email` or `connection_id`
3. Fetch `contact_signals` row for communication cadence
4. Check `polly.db` `commitments` and `waiting_on` for open items

Rex has both database paths in its TOOLS.md:
- `~/.openclaw/workspaces/rex-workspace/connections.db`
- `~/.openclaw/workspaces/polly-workspace/polly.db`

**When to delegate to Rex**: Any Polly Telegram response involving a specific person, org, or relationship. Rex returns a structured brief; Polly surfaces the summary to Louis.

---

## 10. Morning Digest Architecture

The morning digest runs as a two-stage pipeline to avoid a single long-running turn:

| Time | Job | Model | What it does |
|------|-----|-------|--------------|
| 6:50 AM | `polly-pre-digest-healthcheck` | qwen2.5:7b | Deterministic exec: runs `polly_ingest.py` as a health check |
| 6:55 AM | `polly-digest-prep` | qwen2.5:7b | Deterministic exec: runs `polly_ingest.py` → refreshes polly.db + writes digest draft |
| 7:00 AM | `polly-morning-digest` | qwen2.5:7b | Deterministic exec: `cat morning-digest-draft.txt` → sends to Telegram |

**Why split**: A single gemma4:26b turn making 7 sequential exec calls (5 SQL + 2 file reads + assemble + send) hit the 600s timeout at ~968s. The split reduces the 7am turn to 1 exec call + 1 Telegram send.

**Draft file**: `~/.openclaw/workspaces/polly-workspace/state/morning-digest-draft.txt`
Written by `write_morning_digest(conn)` in `polly_ingest.py`, called at the end of every ingest run. If the 6:55am prep fails, the 7am send still uses the previous run's draft (written at 6:40am).

**Never make the 7am cron do live SQL queries** — that's what caused the timeout. All data assembly happens in Python, not in the model turn.

---

## 11. Approval Gate Configuration

Two approval settings exist in `openclaw.json` and they interact:

```json
// Global (applies to all channels) — correct config
"approvals": {
  "exec": { "enabled": false, "mode": "session" }
}

// Per-channel override (OVERRIDES the global setting for Telegram)
"channels": {
  "telegram": {
    "execApprovals": { "enabled": false }   // must be false to match global
  }
}
```

**The trap**: `channels.telegram.execApprovals.enabled: true` overrides `approvals.exec.mode: "off"` for all Telegram-originated exec calls. Setting the global to `"off"` is not sufficient — the channel-level setting must also be `false`.

**Symptom**: Polly or any agent prompts Louis to "confirm this exec" via Telegram even though global approvals are disabled.

**Fix**: Set `channels.telegram.execApprovals.enabled: false` in `openclaw.json`, then restart the gateway.

---

## 12. Gateway Startup Notification Pattern

On every gateway boot, `start_openclaw_gateway_with_kv_checks.sh` injects a one-shot cron job (`polly-startup-notify`) that fires ~90 seconds after boot. The job tells Polly to send "OpenClaw is back online." to Louis via Telegram.

**Why 90 seconds**: Models need time to lazy-load after a cold start. Firing immediately results in a timeout or no response.

**Ollama cold start is normal**: `ollama ps` shows blank output after a restart. Models lazy-load on first inference and stay resident due to `OLLAMA_KEEP_ALIVE=-1`. Do not treat blank `ollama ps` as an error.

---

## 13. Key File Locations

```
~/.openclaw/
  openclaw.json                          — main config (providers, agents, channels, approvals)
  cron/jobs.json                         — all scheduled cron jobs
  logs/gateway.log                       — persistent gateway log (rotated)
  tasks/runs.sqlite                      — OpenClaw internal cron task ledger
  tasks/runs.sqlite.bak-*               — auto-created backups (used for corruption recovery)
  backups/polly.db.YYYY-MM-DD           — nightly polly.db backups (kept 7 days)
  backups/runs.sqlite.YYYY-MM-DD        — nightly runs.sqlite backups (kept 3 days)
  agents/<agent>/agent/auth-profiles.json   — agentDir auth (TWO locations — see §14)
  workspaces/<agent>-workspace/
    SOUL.md                              — agent identity + hard rules (bootstrap context)
    TOOLS.md                             — exec rules + tool usage (bootstrap context)
    auth-profiles.json                   — workspace auth (TWO locations — see §14)
    state/                               — runtime state files
    memory/                              — intake files (maxwell, otto)

~/openclaw/                              — git repo (code + docs)
  scripts/
    openclaw_safe_restart.sh            — safe gateway restart with pre-flight checks (see §17)
    start_openclaw_gateway_with_kv_checks.sh — launchd entrypoint: config enforcement + lock cleanup + gateway exec
    setup_light_ollama_lane.sh          — provisions light Ollama instance (port 11436) via launchd
    apply_polly_resilience_addendum.sh  — enforces Polly lane model routing invariants
    reconcile_runtime_state.py          — clears stale running/locked task rows in runs.sqlite
    polly_ingest.py                      — populates polly.db from intake files + writes sitrep cache + morning digest draft
    maxwell_ingest.py                    — email memory layer (email_threads, contact_signals)
    maxwell_backfill_tick.py            — quota-aware Gmail historical backfill (one page per tick)
    rex_sync_contacts.py                — syncs Gmail senders → connections.db with quota backoff
    backer_health_tick.sh               — 5-min infrastructure health check (all 3 Ollama lanes + locks + reconcile)
    backer_backup_tick.sh               — nightly db backup (polly.db + runs.sqlite via sqlite3 .backup)
    otto_outlook_sweep.sh               — Outlook inbox sweep via AppleScript → sweep-log.yaml
    otto_calendar_tick.sh               — Outlook calendar → calendar-today.yaml
    gcal_today_tick.py                   — Google Calendar → gcal-today.json
    collect_openclaw_metrics.sh         — runtime metrics snapshot (latency, health, model routing)
    test_reconcile_runtime_state.py     — regression tests for runtime state reconciler
    test_otto_suite.sh                  — Otto cron integration tests
  docs/
    OPENCLAW_HOW_IT_SHOULD_WORK.md      — this file
  SOLUTIONS.md                           — bug history (check before fixing anything)
  CLAUDE.md                              — coding agent rules (read before touching code)
```

**Live log** (most recent, JSON format): `/tmp/openclaw/openclaw-YYYY-MM-DD.log`
**Persistent log** (human-readable): `~/.openclaw/logs/gateway.log`
Always check the live log first for current-session errors — it contains more detail.

---

## 14. Auth Profiles: Two Locations

Every agent has **two** auth-profiles.json files that must both be kept in sync:

| Location | Path |
|----------|------|
| Workspace | `~/.openclaw/workspaces/<agent>-workspace/auth-profiles.json` |
| AgentDir | `~/.openclaw/agents/<agent>/agent/auth-profiles.json` |

OpenClaw reads the **agentDir** file at runtime. The workspace file is the source of truth for editing. If you add a new provider auth entry (e.g. `ollama-light:local`), you MUST patch both files for all affected agents.

**To check which agents are missing an entry:**
```bash
python3 -c "
import json
from pathlib import Path
agents = Path('~/.openclaw/agents').expanduser()
for d in sorted(agents.iterdir()):
    f = d / 'agent' / 'auth-profiles.json'
    if f.exists():
        profiles = json.loads(f.read_text()).get('profiles', {})
        if 'ollama-light:local' not in profiles:
            print('MISSING:', d.name)
"
```

**Correct entry format** (same for both files):
```json
"ollama-light:local": { "type": "api_key", "provider": "ollama-light", "key": "ollama-local" }
"ollama-polly:local": { "type": "api_key", "provider": "ollama-polly", "key": "ollama-local" }
```

---

## 15. openclaw.json Key Paths

When modifying `~/.openclaw/openclaw.json`, these are the paths that matter most:

| Path | Purpose | Gotcha |
|------|---------|--------|
| `agents.defaults.models["ollama-polly/qwen2.5:7b-instruct"].params.ollama.reliability.requestTimeoutMs` | Polly lane inference timeout | Must be ≥120000; 3000 causes silent fallback |
| `agents.defaults.models["ollama-light/qwen2.5:7b"].params.ollama.reliability.requestTimeoutMs` | Light lane inference timeout | 60000 is correct |
| `approvals.exec.enabled` | Global exec approval gate | Set `false` to disable |
| `approvals.exec.mode` | Approval mode | Must be `"session"`, `"targets"`, or `"both"` — never `"off"` |
| `channels.telegram.execApprovals.enabled` | Telegram-level override | Overrides global; must also be `false` |
| `agents.list[id=polly].model.primary` | Polly's primary model | Should be `ollama-polly/qwen2.5:7b-instruct` |
| `agents.list[id=polly].model.fallbacks` | Polly fallback chain | `fallbackSilent:true` means failures are invisible |

---

## 16. Database Backup and Restore

### Backups
`backer_backup_tick.sh` runs nightly at 2:40am via `backer-nightly-backup` cron. Uses `sqlite3 .backup` (safe while live). Writes to `~/.openclaw/backups/`.

- `polly.db.YYYY-MM-DD` — kept 7 days
- `runs.sqlite.YYYY-MM-DD` — kept 3 days

OpenClaw also auto-creates `runs.sqlite.bak-runtime-reconcile-*` files on each startup reconcile.

### Restore polly.db
```bash
launchctl stop gui/$(id -u)/ai.openclaw.gateway
cp ~/.openclaw/backups/polly.db.YYYY-MM-DD ~/.openclaw/workspaces/polly-workspace/polly.db
launchctl start gui/$(id -u)/ai.openclaw.gateway
```

### Restore runs.sqlite (after corruption)
```bash
launchctl stop gui/$(id -u)/ai.openclaw.gateway
# Use OpenClaw's auto-backup (most recent)
ls -t ~/.openclaw/tasks/runs.sqlite.bak-runtime-reconcile-* | head -1
cp <latest-backup> ~/.openclaw/tasks/runs.sqlite
launchctl start gui/$(id -u)/ai.openclaw.gateway
```

### Check integrity before restoring
```bash
sqlite3 ~/.openclaw/tasks/runs.sqlite "PRAGMA integrity_check;"
sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db "PRAGMA integrity_check;"
```

---

## 17. Gateway Restart: Safe vs Force

**Preferred method** — use the safe restart script, which handles all edge cases:
```bash
# ✅ RECOMMENDED — pre-flight checks, stale lock cleanup, Ollama lane health, config enforcement
bash ~/openclaw/scripts/openclaw_safe_restart.sh

# Dry-run diagnostics (reports issues without restarting)
bash ~/openclaw/scripts/openclaw_safe_restart.sh --check

# Force mode (SIGKILL stuck processes if SIGTERM fails)
bash ~/openclaw/scripts/openclaw_safe_restart.sh --force
```

**Manual method** (if the script is unavailable):
```bash
# ✅ SAFE — sends SIGTERM, gateway flushes SQLite writes and exits cleanly
launchctl stop  gui/$(id -u)/ai.openclaw.gateway
launchctl start gui/$(id -u)/ai.openclaw.gateway

# ⚠️  FORCE — sends SIGKILL, process dies instantly, can corrupt runs.sqlite
launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway
```

**What the safe restart script checks**: runs.sqlite integrity (auto-restores from backup if corrupt), orphaned session lock files (removes them), Ollama lane health on all 3 ports (restarts dead lanes via launchd), stuck gateway process (escalates from SIGTERM to SIGKILL with --force), runtime state reconciliation (clears stale running markers), and config enforcement (shadow plugin, model params, timeouts).

Use the manual safe restart for simple config changes. Use `openclaw_safe_restart.sh --force` when the gateway is stuck. Never use `launchctl kickstart -k` directly — it bypasses all pre-flight safety checks.

---

## 18. Polly Lane Timeout and Fallback Behavior

Polly's agent config sets a short `requestTimeoutMs` on the `ollama-polly/qwen2.5:7b-instruct` model. If that timeout is too short (e.g. 3000ms), **every** Telegram request will time out before the model can respond, and OpenClaw silently falls back through the fallback chain:

```
ollama-polly/qwen2.5:7b-instruct  →  openai-codex/gpt-5.3-codex  →  ollama/gemma4:26b
```

`fallbackSilent: true` means this happens with no indication to the user. Louis sees a response but it is coming from gemma4:26b, not Polly's intended model.

**Why this causes hallucination**: gemma4:26b responding as a fallback may not correctly receive or apply Polly's SOUL.md bootstrap context. Even with hard rules at the top of SOUL.md, the fallback model generates a plausible-sounding sitrep from training data rather than reading the Live Status block.

**Symptom**: Polly responds with invented calendar events ("Meeting with Client at 9am", "Team Check-In"), invented agent statuses, and doesn't match any real data in the Live Status block.

**Fix**: Set `requestTimeoutMs` to at least 120000ms (2 minutes) on the `ollama-polly/qwen2.5:7b-instruct` model in `openclaw.json` → `agents.defaults.models`. Restart gateway after. This ensures qwen2.5:7b-instruct actually runs rather than immediately timing out.

**The correct value**: 120000ms. Cold-start model load can take 30–60 seconds on first request. 3000ms is only appropriate if the model is guaranteed to already be warm.

---

## 19. approvals.exec.mode Valid Values

`approvals.exec.mode` only accepts: `"session"`, `"targets"`, `"both"`. The value `"off"` is **invalid** and will cause the gateway to refuse to start with a config validation error visible in `gateway.err.log`.

**To disable exec approvals globally**: set `approvals.exec.enabled: false` (and set `mode` to any valid value — it is ignored when disabled). Also set `channels.telegram.execApprovals.enabled: false` or the channel-level setting will override the global one.

**Symptom of invalid mode**: gateway exits immediately on startup; `gateway.err.log` shows `approvals.exec.mode: Invalid input (allowed: "session", "targets", "both")`.

---

## 20. Known Failure Modes and Their Root Causes

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| Polly fabricates sitrep | qwen2.5:7b ignores SOUL.md exec instructions when message is conversational | `write_sitrep_cache()` pre-embeds live data into SOUL.md |
| Exec fails with "elevated not available" | Model adds `elevated: true` based on SOUL.md context about restart operations | TOOLS.md "NEVER elevated" rule; cron message explicit prohibition |
| Exec fails with "approval required" | Missing `ask: "off"` parameter | TOOLS.md Exec Rules; deterministic cron messages |
| Exec fails with "sandbox runtime disabled" | Otto defaults to `host: "sandbox"` | TOOLS.md `host: "gateway"` rule |
| Exec fails with "allowlist miss" | Model uses `security: "allowlist"` and command isn't in TOOLS.md allowlist | TOOLS.md "NEVER allowlist" rule |
| Cron times out | Script takes longer than `timeoutSeconds`; or model loops on retries | Increase `timeoutSeconds`; use deterministic exec to prevent retry loops |
| Morning digest times out at ~968s | gemma4:26b making 7 sequential exec calls in one turn | Use write_morning_digest() to pre-assemble; 7am cron does 1 exec + send only |
| Otto AppleScript error -1728 | Invalid Outlook API reference (`account list 1`) | Use `drafts folder of default account` |
| Otto AppleScript hangs indefinitely | `every message of inbox whose time received >= cutoff` has no timeout | Wrap in `with timeout of 45 seconds` / `end timeout` block |
| Exec approval prompts appear in Telegram despite global approvals off | `channels.telegram.execApprovals.enabled: true` overrides global setting | Set `channels.telegram.execApprovals.enabled: false` in openclaw.json |
| Relationship query returns no email context | Rex only searched connections.db, not polly.db email_threads | Use 4-step Rex query pattern (see §9); ensure maxwell_ingest.py is running |
| Polly hallucinating sitrep despite correct SOUL.md | `requestTimeoutMs: 3000` on polly lane causes silent fallback to gemma4:26b which ignores bootstrap context | Set `requestTimeoutMs: 120000` on `ollama-polly/qwen2.5:7b-instruct` in `agents.defaults.models` |
| Gateway refuses to start, config error in gateway.err.log | `approvals.exec.mode: "off"` is invalid | Set `approvals.exec.enabled: false` and `mode: "session"`; restart gateway |
| "No API key found for provider ollama-light" | agentDir auth-profiles.json missing entry (separate from workspace auth-profiles.json) | Patch both locations for all agents (see §14) |
| runs.sqlite malformed on startup | Force kill (SIGKILL) mid-write corrupted WAL | Use safe restart; restore from `.bak-runtime-reconcile-*` backup (see §16) |
| Errors only visible in err.log, not gateway.log | Some startup failures write to gateway.err.log not gateway.log | Always check both; live session errors in `/tmp/openclaw/openclaw-YYYY-MM-DD.log` |

---

## 21. The "Root Cause or Symptom" Test

Before fixing any issue, ask:

0. **Should this work be in an agent turn at all?** → If it's a deterministic script invocation, move it to launchd (see §0 and §22). Patching exec parameters, adding `ask:"off"` to cron messages, or tuning timeouts are all symptoms of the wrong architectural choice.
1. **Is this a model behavior problem?** → Fix the instruction surface (SOUL.md, TOOLS.md, cron message). Don't patch the script.
2. **Is this a script/code problem?** → Fix the script. Don't paper over it with SOUL.md instructions.
3. **Is this a parameter problem?** → Fix TOOLS.md Exec Rules AND the cron message payload. Both layers needed for qwen2.5:7b.
4. **Is the data fabricated?** → The model isn't calling tools. Pre-embed the data (sitrep cache pattern) or make the cron message deterministic.

**Always verify**: after a fix, check the next cron run's `lastRunStatus` in `jobs.json` and the session transcript to confirm the tool was actually called with the right parameters.

---

## 22. Direct Launchd Ingest Agents (Replacing LLM-Mediated Cron)

**Problem solved**: `ingestion-watch-20m` and `maxwell-ingest-30m` ran `polly_ingest.py` and `maxwell_ingest.py` by asking an LLM agent to "execute this command via exec." This added unnecessary fragility:
- The model had to generate correct exec parameters (`ask:"off"`, no `security:allowlist`) on every run
- Each invocation created an isolated session, locked a `.jsonl` file, and consumed a gateway event loop slot for ~10-15s
- A slow Ollama inference or Telegram stall during one of these cycles blocked all other pending callbacks
- Session lock accumulation cascaded into the 459s–955s backer hangs seen in production logs

**Fix**: Both scripts now run as native launchd background agents that invoke Python directly, with zero LLM involvement.

| Agent | Label | Interval | Script | Log |
|-------|-------|----------|--------|-----|
| Polly ingest | `com.openclaw.polly-ingest` | every 20m | `polly_ingest.py --verbose` | `backer-workspace/logs/polly-ingest.log` |
| Maxwell ingest | `com.openclaw.maxwell-ingest` | every 30m | `maxwell_ingest.py --verbose` | `backer-workspace/logs/maxwell-ingest.log` |

**Plist files**: `~/openclaw/launchd/`

**Install** (one-time, run on Mac terminal):
```bash
bash ~/openclaw/scripts/install_ingest_launchd.sh
```

**Check status**:
```bash
bash ~/openclaw/scripts/install_ingest_launchd.sh --status
```

**Uninstall** (reverts to OpenClaw cron jobs — re-enable them in jobs.json manually):
```bash
bash ~/openclaw/scripts/install_ingest_launchd.sh --uninstall
```

**Rule**: Never add LLM agent turns to the ingest pipeline. Data pipeline work (file I/O, SQLite writes) must run as direct scripts via launchd or cron. LLM agent turns are only appropriate for work that requires reasoning — digests, relationship queries, Slack summarization, ACP delegation.

**Overlap protection**: Both scripts use `script_lock.py` (fcntl-based) so if a slow run is still in progress when the next interval fires, the new invocation exits immediately with `{"status":"skipped"}` rather than queueing and hanging. `backer_health_tick.sh` clears any stale lock files from crashes within the next 5-minute health cycle.

---

## 23. Known Improvement Opportunities

This section is the honest backlog: things that are architecturally wrong, actively fragile, or known to cause reliability problems. It is not a wishlist — every item here has caused or will cause a real failure. Ordered by impact.

---

### 23.1 Remaining Deterministic Cron Jobs Should Move to launchd

**Status**: Not yet migrated. Working but suboptimal.

The following jobs are still in `jobs.json` as `agentTurn` entries even though they execute a fixed script with no reasoning:

| Job | Frequency | Script | Event loop cost |
|-----|-----------|--------|----------------|
| `backer-health-5m` | every 5m | `backer_health_tick.sh` | **288 turns/day — highest priority** |
| `rex-backfill-365d-20m` | every 20m | `rex_sync_contacts.py` | 72 turns/day |
| `gmail-backfill-12m-20m` | every 20m | `maxwell_backfill_tick.py` | 72 turns/day |
| `rex-contacts-sync-6h` | every 6h | `rex_sync_contacts.py` | 4 turns/day |
| `otto-outlook-sweep` | hourly (weekdays) | `otto_outlook_sweep.sh` | ~11 turns/day |
| `otto-calendar-6am` | daily | `otto_calendar_tick.sh` | 1 turn/day |
| `maxwell-gcal-6am` | daily | `gcal_today_tick.py` | 1 turn/day |
| `backer-daily-audit` | daily 2am | sqlite3 shell commands | 1 turn/day |
| `backer-nightly-backup` | daily 2:40am | `backer_backup_tick.sh` | 1 turn/day |

`backer-health-5m` is the highest-impact migration: it fires 288 times per day, each consuming a qwen2.5:7b inference + event loop slot + session lock. Migrating it to launchd eliminates the single largest source of routine gateway contention.

`rex-backfill-365d-20m` and `gmail-backfill-12m-20m` are tied for second: 72 turns/day each for scripts that run the same command every time.

The migration pattern is established: copy `launchd/com.openclaw.polly-ingest.plist`, adjust `Label`, `ProgramArguments`, and `StartInterval`, run `install_ingest_launchd.sh` equivalent, disable in `jobs.json`.

---

### 23.2 `polly-pre-digest-healthcheck` Is Now Redundant

**Status**: Runs daily at 6:50am. Now a no-op.

This job was designed to run `polly_ingest.py` as a final refresh before the 7am digest. Since `com.openclaw.polly-ingest` now runs every 20 minutes via launchd, polly.db is at most 20 minutes stale at any time. The 6:50am job runs `polly_ingest.py` exactly as the launchd agent would — it adds nothing.

**Recommendation**: Disable `polly-pre-digest-healthcheck` and `polly-digest-prep` (6:55am), and simplify the morning pipeline to:

| Time | Job | Action |
|------|-----|--------|
| 7:00am | `polly-morning-digest` | `cat` draft file + send Telegram |

The 7am job already reads from `morning-digest-draft.txt`, which `polly_ingest.py` writes at the end of every launchd run. The 6:50am and 6:55am jobs are belt-and-suspenders from before the launchd agent existed.

**Risk**: If `polly_ingest.py` is broken, the 6:50am agent turn would catch it (it runs the same script). Removing it means a broken ingest script is not discovered until the digest is wrong. Acceptable trade-off — the launchd agent logs to `backer-workspace/logs/polly-ingest.log`, and `backer-health-5m` (once migrated to launchd) will still run every 5 minutes.

---

### 23.3 `otto-slack-digest` Is Timing Out

**Status**: `lastRunStatus: "error"`, `lastDurationMs: 420009` (exactly at timeout), `consecutiveErrors: 1`.

This is a live failure. The job hit its 420s ceiling at the last run. Root causes are likely one or both of:

1. **gemma4:26b cold-start**: If the primary Ollama lane (port 11434) is cold, the model can take 60-90s to load before the first token. A complex multi-tool turn (read Slack channels + summarize + send ACP) then runs into the timeout.
2. **Slack API rate limiting or slow curl**: The job reads multiple Slack channels via curl. If a channel has high message volume or the Slack API is slow, the turn stalls.

**Recommended fix**: Increase `timeoutSeconds` from 420 to 600. Add a warm-up check: either ensure `backer-health-5m` prewarms port 11434 before 9am, or add a 5-minute pre-Slack-digest job that prewarms the primary lane with a dummy ping.

The deeper fix is to make the Slack reading deterministic (a shell script that curls Slack channels into a file) and have the agent turn only do summarization on pre-fetched data — separating I/O from reasoning, same principle as §0.

---

### 23.4 `otto-outcome-sweep` Ran for 33 Minutes

**Status**: `lastDurationMs: 1995906` (~33 min). `lastRunStatus: "error"`.

The job searches Outlook inbox via AppleScript for reply tracking. The known failure mode (§20) is `every message of inbox whose time received >= cutoff` with no timeout block, causing an indefinite hang on large inboxes. The 33-minute duration confirms this happened.

The fix is already documented in §20 (wrap in `with timeout of 45 seconds`), but it has not been applied to `otto_outlook_sweep.sh` or the AppleScript in this job's message. **This fix is overdue.**

---

### 23.5 No Schema Validation Between Producers and Consumers

**Status**: Unaddressed. Root cause of several past bugs.

Maxwell writes `gmail-intake-latest.json`. Both `polly_ingest.py` and `maxwell_ingest.py` read it. There is no contract between them: the writer can produce a bare array, add unexpected fields, omit required fields, or embed invalid JSON characters — all of which have happened. The fixes applied (`_sanitize_json_control_chars`, `isinstance(data, list)` guard, validated-copy pattern) are defensive reader patches, not enforcement at the source.

**Recommended fix**: Add a `_validate_and_write_intake(data, path)` function to the gmail-sweep agent's output path that enforces a minimal schema before writing:
- Must be a list or `{"threads": list, "timestamp": str}`
- Each thread must have at minimum `id`, `from`, `subject`
- No bare control characters (sanitize before writing, not after reading)

A 20-line JSON Schema check written once eliminates an entire class of reader bugs permanently.

---

### 23.6 Morning Digest Has No Fallback When Draft Is Stale

**Status**: Works today, but fragile if launchd agent stops.

`polly-morning-digest` at 7am reads `morning-digest-draft.txt` and sends it. If the launchd agent stops running (e.g., after a macOS update that resets LaunchAgents), the draft file goes stale and Louis gets yesterday's digest with no indication it's outdated.

**Recommended fix**: `write_morning_digest()` in `polly_ingest.py` already writes a timestamp header. The 7am job should check the file's mtime and, if it's more than 2 hours old, prepend `⚠️ Digest may be stale (last refreshed: <time>)` before sending. A single `stat` + comparison in the cron message is sufficient.

---

### 23.7 Log Rotation Not Configured for launchd Agent Logs

**Status**: New issue introduced with §22 migration.

`polly-ingest.log` and `maxwell-ingest.log` are written to unbounded. With `polly_ingest.py --verbose` writing ~50 lines per run, 20 minutes per run, that's ~3,600 lines/day → ~1.3M lines/year. These files will grow without bound.

**Recommended fix**: Add a `newsyslog` config entry or a weekly truncation in `backer_backup_tick.sh`:
```bash
# Truncate ingest logs older than 7 days of lines
tail -n 10000 ~/.openclaw/workspaces/backer-workspace/logs/polly-ingest.log \
  > /tmp/polly-ingest.log.tmp && mv /tmp/polly-ingest.log.tmp \
  ~/.openclaw/workspaces/backer-workspace/logs/polly-ingest.log
```

---

### 23.8 The Gateway Event Loop Is a Single Point of Contention

**Status**: Structural. Will not be fully resolved without upstream changes to OpenClaw.

All of the above improvements reduce contention but do not eliminate the architectural constraint: the gateway runs one async event loop, and all agent turns — Telegram, cron, ACP — share it. A single long-running gemma4:26b turn (Slack digest, Gmail sweep, outcome sweep) blocks every other callback for its duration.

The mitigations available within the current architecture:
- Migrate all deterministic work to launchd (§23.1) — eliminates routine contention
- Separate I/O from reasoning in remaining agent turns (§23.3) — reduces per-turn duration  
- Ensure heavy models are pre-warmed before long turns fire (§23.3) — reduces cold-start latency
- Keep `timeoutSeconds` calibrated to actual observed durations, not aspirational ones

The root fix would be multi-worker gateway support (parallel event loops per agent), which is an OpenClaw platform change. File the issue upstream if the single-loop constraint continues causing cascading failures after the above mitigations are applied.
