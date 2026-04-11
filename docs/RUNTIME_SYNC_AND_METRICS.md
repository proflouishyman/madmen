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

- `runtime_metrics/20260410T072040Z`
- Latency: `polly 20.843s`, `rex 46.4s`, `maxwell 46.089s`

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

## 3) Runtime stale-state reconciler

Script:

`/Users/louishyman/openclaw/scripts/reconcile_runtime_state.py`

Purpose:

- reconcile stale `running` task rows as `lost`
- immediately reconcile impossible rows where `status=running` but terminal markers already exist (`ended_at` / `terminal_outcome`)
- clear stale per-agent session-store `status=running` markers
- remove orphaned `*.jsonl.lock` files
- mark duplicate concurrent `running` cron rows for the same `source_id` as `lost` (keep newest)

Operational integration:

- startup wrapper runs reconciler before boot:
  - `/Users/louishyman/openclaw/scripts/start_openclaw_gateway_with_kv_checks.sh`
- Backer health tick runs reconciler every cycle and reports:
  - `stale_tasks_marked`
  - `stale_sessions_cleared`

Validation:

- `python3 -m unittest /Users/louishyman/openclaw/scripts/test_reconcile_runtime_state.py -v`

## 4) Ollama static-prompt cache benchmark

Script:

`/Users/louishyman/openclaw/scripts/benchmark_ollama_soul_cache.py`

Purpose:

- benchmark repeated turns for a fixed SOUL-heavy prompt on one session
- compare cold vs warm latency after enabling Ollama cache controls
- report `p50` / `p95` and timeout rate

Usage:

```bash
python3 /Users/louishyman/openclaw/scripts/benchmark_ollama_soul_cache.py \
  --agent polly \
  --session-id ollama-kv-bench \
  --warmup 1 \
  --turns 8 \
  --message "Summarize yesterday's top 3 priorities in 3 bullets."
```
