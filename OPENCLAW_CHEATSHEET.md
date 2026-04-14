# OpenClaw Cheatsheet

> Quick reference for Louis. Commands run from any terminal on the Mac.

---

## Gateway

```bash
# Status
launchctl list | grep openclaw

# ✅ SAFE restart — graceful, allows gateway to flush SQLite writes before exit
launchctl stop  gui/$(id -u)/ai.openclaw.gateway
launchctl start gui/$(id -u)/ai.openclaw.gateway

# ⚠️  FORCE restart — only when gateway is stuck/unresponsive
# Sends SIGKILL (uncatchable). Can corrupt runs.sqlite if mid-write.
launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway
```

> **After restart:** Polly sends a Telegram message "OpenClaw is back online." within ~90s.
> If nothing comes after 3 minutes, check logs (see below).

### If runs.sqlite gets corrupted after a force restart

```bash
# 1. Stop the gateway first
launchctl stop gui/$(id -u)/ai.openclaw.gateway

# 2. Find the most recent clean backup (OpenClaw creates these automatically)
ls -lt ~/.openclaw/tasks/runs.sqlite.bak-runtime-reconcile-* | head -5

# 3. Restore it
cp ~/.openclaw/tasks/runs.sqlite.bak-runtime-reconcile-<LATEST> ~/.openclaw/tasks/runs.sqlite

# 4. Restart
launchctl start gui/$(id -u)/ai.openclaw.gateway
```

### If polly.db gets corrupted

```bash
# Restore from nightly backup (kept 7 days)
launchctl stop gui/$(id -u)/ai.openclaw.gateway
ls ~/.openclaw/backups/polly.db.*
cp ~/.openclaw/backups/polly.db.YYYY-MM-DD ~/.openclaw/workspaces/polly-workspace/polly.db
launchctl start gui/$(id -u)/ai.openclaw.gateway
```

---

## Logs

```bash
# Gateway live log
tail -f ~/.openclaw/logs/gateway.log

# Last 50 lines
tail -50 ~/.openclaw/logs/gateway.log

# Errors only
grep -i "error\|fail\|timeout" ~/.openclaw/logs/gateway.log | tail -30

# Specific agent log
grep '"agentId":"polly"' ~/.openclaw/logs/gateway.log | tail -20
grep '"agentId":"maxwell"' ~/.openclaw/logs/gateway.log | tail -20

# Backer health/audit logs
ls ~/.openclaw/workspaces/backer-workspace/logs/
tail -20 ~/.openclaw/workspaces/backer-workspace/logs/$(date +%Y-%m-%d)-daily-audit.log
```

---

## Ollama

```bash
# Which models are loaded (may be blank on cold start — that's normal)
ollama ps

# List all installed models
ollama list

# Three lanes:
#   Port 11434 — primary  (gemma4:26b  — Polly digest, Otto, general)
#   Port 11435 — polly    (qwen2.5:7b-instruct — Polly main, Maxwell, Rex)
#   Port 11436 — light    (qwen2.5:7b  — Backer, ingestion crons)

# Test each lane
curl -s http://localhost:11434/api/tags | python3 -m json.tool | grep name
curl -s http://localhost:11435/api/tags | python3 -m json.tool | grep name
curl -s http://localhost:11436/api/tags | python3 -m json.tool | grep name

# Restart a specific Ollama instance (if using launchctl services)
# launchctl kickstart -k gui/$(id -u)/ai.ollama.11434
```

---

## Cron Jobs

```bash
# List all jobs with status
openclaw cron list

# Trigger a job immediately by name
openclaw cron trigger polly-morning-digest
openclaw cron trigger maxwell-gmail-sweep
openclaw cron trigger ingestion-watch-20m
openclaw cron trigger maxwell-ingest-30m

# Disable / enable a job
openclaw cron disable polly-morning-digest
openclaw cron enable  polly-morning-digest

# Edit jobs directly (restart gateway after)
nano ~/.openclaw/cron/jobs.json
```

### Key cron schedule at a glance

| Time (ET)   | Job                       | What it does                           |
|-------------|---------------------------|----------------------------------------|
| Every 5m    | backer-health-5m          | sqlite3 integrity + WAL checkpoint     |
| Every 15m   | maxwell-gmail-sweep       | Fetch Gmail → gmail-intake-latest.json |
| Every 20m   | ingestion-watch-20m       | polly_ingest.py → polly.db + draft     |
| Every 30m   | maxwell-ingest-30m        | maxwell_ingest.py → email_threads      |
| Every 30m   | otto-outlook-sweep        | Outlook sweep → sweep-log.yaml         |
| 6:00 AM     | otto-calendar-6am         | Outlook calendar → calendar-today.yaml |
| 6:05 AM     | maxwell-gcal-6am          | Google Calendar → gcal-today.json      |
| 6:50 AM     | polly-pre-digest-health   | Pre-digest system check                |
| 6:55 AM     | polly-digest-prep         | Refresh polly.db + write digest draft  |
| 7:00 AM     | polly-morning-digest      | Read draft → send Telegram             |
| 9:00 AM M–F | otto-slack-digest         | Slack scan → Polly escalation          |
| 10:00 PM    | backer-nightly-backup     | WAL checkpoint                         |
| 11:00 PM    | backer-daily-audit        | Full integrity check + log             |

---

## Database Queries

```bash
# ── polly.db ──────────────────────────────────────────────────────────────────

DB=~/.openclaw/workspaces/polly-workspace/polly.db

# Pending escalations
sqlite3 $DB "SELECT created_at, agent_id, title, severity FROM escalations WHERE status='pending' ORDER BY created_at DESC LIMIT 10;"

# Open tasks due today
sqlite3 $DB "SELECT due, title, agent_id FROM tasks WHERE status='open' AND due <= date('now') ORDER BY due ASC LIMIT 20;"

# Today's events
sqlite3 $DB "SELECT datetime, title, participants FROM events WHERE datetime >= date('now') AND datetime < date('now','+1 day') ORDER BY datetime ASC;"

# Agent health (any errors?)
sqlite3 $DB "SELECT agent_id, last_status, last_error, updated_at FROM agent_health ORDER BY updated_at DESC;"

# Email threads needing reply
sqlite3 $DB "SELECT from_name, subject, received_at FROM email_threads WHERE reply_needed=1 ORDER BY received_at DESC LIMIT 15;"

# Recent direct emails
sqlite3 $DB "SELECT from_name, subject, received_at, topic_tags FROM email_threads WHERE is_direct=1 ORDER BY received_at DESC LIMIT 20;"

# Contact signals (who emails you most)
sqlite3 $DB "SELECT from_name, total_threads, open_reply_threads, last_email_at FROM contact_signals ORDER BY total_threads DESC LIMIT 20;"

# Commitments you owe
sqlite3 $DB "SELECT to_whom, description, due, status FROM commitments WHERE status='open' ORDER BY due ASC LIMIT 10;"

# Waiting on others
sqlite3 $DB "SELECT from_whom, description, due FROM waiting_on WHERE status='open' ORDER BY due ASC LIMIT 10;"

# ── Rex connections.db ────────────────────────────────────────────────────────

RDB=~/.openclaw/workspaces/rex-workspace/connections.db

# Find a contact
sqlite3 $RDB "SELECT name, org, role, last_contact, notes FROM connections WHERE name_lower LIKE '%smith%' LIMIT 5;"

# Contacts not heard from in 90+ days
sqlite3 $RDB "SELECT name, org, last_contact FROM connections WHERE last_contact < date('now','-90 days') ORDER BY last_contact ASC LIMIT 20;"

# Full-text search across relationship documents
sqlite3 $RDB "SELECT snippet(documents, -1, '>>','<<','...',20) FROM documents WHERE documents MATCH 'funding' LIMIT 5;"
```

---

## Scripts (run manually when needed)

```bash
# Force a full polly.db refresh + write new digest draft
python3 ~/openclaw/scripts/polly_ingest.py --verbose

# Force email memory refresh (maxwell → email_threads / contact_signals)
python3 ~/openclaw/scripts/maxwell_ingest.py --verbose

# Rebuild contact_signals from scratch
python3 ~/openclaw/scripts/maxwell_ingest.py --refresh-signals

# Run Gmail sweep manually
python3 ~/openclaw/scripts/maxwell_ingest.py --verbose  # reads existing intake
# (to re-sweep Gmail, trigger maxwell-gmail-sweep cron or use gog directly)

# Backer health check
bash ~/openclaw/scripts/backer_health_tick.sh

# Otto Outlook sweep (manual)
bash ~/openclaw/scripts/otto_outlook_sweep.sh
```

---

## Config Files

| File                                  | What's in it                              |
|---------------------------------------|-------------------------------------------|
| `~/.openclaw/openclaw.json`           | Providers, channels, agent routes, global settings |
| `~/.openclaw/cron/jobs.json`          | All scheduled jobs                        |
| `~/.openclaw/workspaces/*/SOUL.md`    | Each agent's personality + responsibilities |
| `~/.openclaw/workspaces/*/TOOLS.md`   | Each agent's exec rules + allowed scripts |
| `~/.openclaw/workspaces/*/auth-profiles.json` | Per-agent API key mappings      |

> **After editing openclaw.json or jobs.json:** restart the gateway to pick up changes.

---

## Telegram — Talk to Agents Directly

| Bot              | Agent   | What to ask                                  |
|-----------------|---------|----------------------------------------------|
| @Polly_314_bot   | Polly   | Morning digest, escalations, "what's going on with X?" |
| @Maxwell_314_bot | Maxwell | Gmail questions, draft review                |
| @Otto_314_bot    | Otto    | Outlook/Slack/calendar questions             |
| @Rex_314_bot     | Rex     | "What's the status with [person/org]?"       |
| @Worf_314_bot    | Worf    | General questions, research, one-off tasks   |
| @Forge_314_bot   | Forge   | Code tasks, file edits, scripts              |

---

## Health Check Sequence

If something seems off, run through this in order:

```bash
# 1. Is the gateway up?
launchctl list | grep openclaw

# 2. Any recent errors?
grep -i "error\|timeout\|fail" ~/.openclaw/logs/gateway.log | tail -20

# 3. Is polly.db populated?
sqlite3 ~/.openclaw/workspaces/polly-workspace/polly.db "SELECT COUNT(*) FROM agent_health; SELECT COUNT(*) FROM email_threads;"

# 4. Is the digest draft fresh?
ls -la ~/.openclaw/workspaces/polly-workspace/state/morning-digest-draft.txt
cat ~/.openclaw/workspaces/polly-workspace/state/morning-digest-draft.txt

# 5. Are Ollama models responding?
curl -s http://localhost:11435/api/generate -d '{"model":"qwen2.5:7b-instruct","prompt":"ping","stream":false}' | python3 -m json.tool | grep response

# 6. Restart if needed
launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway
```
