# TOOLS.md - Rex

## Primary Store
- SQLite database: `~/.openclaw/workspaces/rex-workspace/connections.db`
- Mode: WAL for concurrent reads with safe writes.

## Core Tables
- `connections`: canonical person/org relationship rows.
- `documents` (FTS5): searchable context evidence linked to contacts.

## Allowed Actions
- Read/write `connections.db`.
- Read intake files in rex workspace state and approved signal payloads.
- Respond to relationship queries from Polly/Maxwell/Otto.

## Disallowed Actions
- No outbound send actions.
- No writes to non-Rex workspaces.
- No autonomous deletion of relationship history unless explicitly requested.

## Reliability
- Persist run summaries under `rex-workspace/state/`.
- Use checkpoint files for staged backfills.
- On quota pressure, apply backoff and resume from checkpoint.
