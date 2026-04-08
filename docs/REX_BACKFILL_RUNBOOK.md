# Rex Gmail Backfill Runbook

## Goal
Continuously backfill Rex contact context from Gmail across the past year while respecting quota limits.

## Script
Path:
`~/.openclaw/workspaces/rex-workspace/state/rex_sync_contacts.py`

Key behavior:
- checkpointed pagination resume (`next_page_token`)
- exponential quota backoff persisted in checkpoint state
- optional cooldown after a full cycle completes
- contact upserts into `connections.db`

## Year Backfill Command
```bash
python3 ~/.openclaw/workspaces/rex-workspace/state/rex_sync_contacts.py \
  --account lhyman@gmail.com \
  --days 365 \
  --max-per-page 100 \
  --max-pages 2 \
  --checkpoint-file ~/.openclaw/workspaces/rex-workspace/state/rex_sync_checkpoint_365d.json \
  --backoff-base-seconds 180 \
  --backoff-max-seconds 7200 \
  --cycle-holdoff-seconds 86400
```

## Cron
Configured cron:
- name: `rex-backfill-365d-20m`
- schedule: every 20 minutes
- agent: `rex`

This job advances year-backfill in small chunks and self-throttles via checkpointed backoff.

## Existing Incremental Sync
The 14-day incremental sync cron remains enabled:
- name: `rex-contacts-sync-6h`
- purpose: keep recent contacts fresh while backfill continues

## Verification
```bash
openclaw cron list
sqlite3 ~/.openclaw/workspaces/rex-workspace/connections.db "SELECT COUNT(*) FROM connections;"
cat ~/.openclaw/workspaces/rex-workspace/state/connections-sync-last.json
```
