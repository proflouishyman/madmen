# Runtime Sync And Metrics

This repo now includes two operational scripts:

## 1) Metrics bundle

Script:

`/Users/louishyman/openclaw/scripts/collect_openclaw_metrics.sh`

What it collects:

- model status
- channel probe
- health verbose
- cron inventory
- doctor output
- security audit (`--deep`)
- Otto suite (`--quick`)
- timed agent latency checks (`polly`, `rex`, `maxwell`)
  - hard process timeouts per probe to prevent collector hangs
  - `timed_out` boolean in `agent-latency.json`
  - timeout-path bytes decoding guard for Python 3.14 `TimeoutExpired`

Outputs:

- timestamped folder under `runtime_metrics/<UTCSTAMP>/`
- machine summary: `summary.json`
- human summary: `summary.md`
- symlink: `runtime_metrics/latest`

Latest validated snapshot:

- `runtime_metrics/20260410T071325Z`
- Latency: `polly 23.45s`, `rex 47.891s`, `maxwell 52.655s`

Maxwell backfill execution mode:

- Cron `gmail-backfill-12m-20m` now runs deterministic exec tick script:
  - `/Users/louishyman/openclaw/scripts/maxwell_backfill_tick.py`
- Compact checkpoint path:
  - `/Users/louishyman/.openclaw/workspaces/maxwell-workspace/memory/gmail-backfill-12m-checkpoint.json`

## 2) Runtime snapshot sync (GitHub-safe default)

Script:

`/Users/louishyman/openclaw/scripts/snapshot_openclaw_runtime.sh`

Default behavior (safe mode):

- captures command outputs (health, cron, channels, models, security audit)
- writes redacted config: `openclaw.redacted.json`
- copies agent contract files (`IDENTITY.md`, `SOUL.md`, `TOOLS.md`, `MEMORY.md`, `USER.md`)
- copies selected operational state JSON files
- writes `manifest.sha256`

Sensitive mode:

```bash
/Users/louishyman/openclaw/scripts/snapshot_openclaw_runtime.sh --include-sensitive
```

This additionally copies:

- Maxwell memory folder
- Polly memory folder
- Rex `connections.db`

`--include-sensitive` outputs are ignored by git via:

- `runtime_snapshots/*/sensitive/`

## GitHub backup recommendation

- Keep repository private.
- Commit scripts/docs and redacted snapshots.
- Avoid committing raw secrets, OAuth files, bot tokens, and full sensitive memory dumps.
