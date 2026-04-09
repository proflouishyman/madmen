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
