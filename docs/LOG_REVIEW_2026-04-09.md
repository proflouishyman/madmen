# OpenClaw Log Review (2026-04-09)

Review timestamp:
- Local: `2026-04-09 14:54:49 EDT`
- UTC: `2026-04-09 18:54:49Z`

Log files reviewed:
- `~/.openclaw/logs/gateway.log` (`2324` lines)
- `~/.openclaw/logs/gateway.err.log` (`390724` lines)
- `/tmp/openclaw/openclaw-2026-04-09.log` (`1268` lines)

## High-Signal Findings

1. Persistent model timeout pressure
- `gateway.err.log`: `LLM request timed out` = `152`
- `/tmp/openclaw/openclaw-2026-04-09.log`: `LLM request timed out` = `245`
- `/tmp/openclaw/openclaw-2026-04-09.log`: `Request was aborted` = `59`
- Impact: long-running cron tasks frequently exceed lane limits, especially on `maxwell`, `otto`, `rex`, and `polly` cron runs.

2. Session lock overruns
- `gateway.err.log`: `session-write-lock` releases = `17`
- Many locks exceeded configured max lock hold (`720000ms`) before release.
- Impact: elevated latency and cascading timeout/failover behavior.

3. Telegram polling churn
- `gateway.err.log`: `Polling runner stop timed out after 15s` = `33`
- Impact: periodic Telegram channel restart cycles and possible delayed inbound handling.

4. ACP handoff instability (historical but present)
- `gateway.log`: `acpx exited with code 1` = `3`
- `/tmp/openclaw/openclaw-2026-04-09.log`: `acpx exited with code 1` = `2`
- Impact: direct ACP handoff reliability remains a risk path; direct local dispatch fallback remains necessary.

5. Sandbox/Docker dependency failures (time-bounded cluster)
- `gateway.err.log`: `Failed to inspect sandbox image` = `12`
- `/tmp/openclaw/openclaw-2026-04-09.log`: same pattern = `32`
- Impact: if agents are forced through container sandbox while Docker daemon is unavailable, cron jobs fail immediately.

6. Operator scope warning remains
- `missing scope: operator.read`
  - `gateway.log`: `46`
  - `/tmp/openclaw/openclaw-2026-04-09.log`: `43`
- Impact: deep operator telemetry/probing remains partially degraded.

7. Heartbeats present but sparse in reviewed slices
- `gateway.log`: `HEARTBEAT_OK` = `3`
- `/tmp/openclaw/openclaw-2026-04-09.log`: `HEARTBEAT_OK` = `2`
- Impact: heartbeat visibility exists, but should be denser for unattended operations.

## What Is Healthy In The Same Window

- Model primary remains Codex in startup lines:
  - `agent model: openai-codex/gpt-5.3-codex`
- ACPX runtime plugin consistently initializes:
  - `acpx runtime backend ready`
- Cron update events continue to apply:
  - repeated `cron: job updated`

## Current Priority Order

1. Reduce timeout/lock pressure on long cron workloads (most operationally expensive issue).
2. Stabilize Telegram polling restarts.
3. Keep ACP direct-dispatch fallback as default while ACPX failures persist.
4. Remove Docker-sandbox dependency from critical cron paths unless daemon uptime is guaranteed.
5. Resolve `operator.read` scope mismatch for full deep-probe observability.

---

## Addendum (2026-04-10 Runtime Stabilization)

Newly confirmed root causes and fixes:

1. Rex cron self-recursion:
- Rex cron turns were calling `cron.run` on their own job id, returning `already-running` instead of executing backfill.
- Fixed by restricting tools to `exec,read,write` and forcing deterministic `rex_sync_contacts.py` execution with 365d checkpoint.

2. Maxwell cron subagent flow lockups:
- Long-lived subagent flows remained active and repeatedly timed out.
- Stale run records in `~/.openclaw/subagents/runs.json` were reconciled, gateway restarted, and Maxwell cron switched to in-session deterministic sweep behavior (no subagent spawn).

3. Metrics collector hangs:
- `scripts/collect_openclaw_metrics.sh` now applies hard subprocess timeouts and emits `timed_out` flags.

4. Maxwell 12m backfill drift and large-state prompt pressure:
- Historical Maxwell backfill cron was disabled and state readbacks were using a large legacy JSON.
- Added deterministic exec tick script (`scripts/maxwell_backfill_tick.py`) with compact checkpoint state and re-enabled `gmail-backfill-12m-20m` every 20 minutes.
- Verified checkpoint progression and healthy cron status (`ok`) after re-enable.

5. Collector timeout path bug on Python 3.14:
- Timeout exceptions returned bytes in `stdout`/`stderr`, which broke summary generation.
- Added safe bytes-to-text normalization in the timeout handler.

Post-fix validation snapshot:
- `runtime_metrics/20260410T072040Z`
- Agent latency probe success: `polly 20.843s`, `rex 46.4s`, `maxwell 46.089s`
