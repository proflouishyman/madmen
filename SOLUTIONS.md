[2026-04-08] - Polly Could Not Route To Forge
Problem
Polly reported that ACP routing to `forge` was unavailable, first with "ACP runtime backend is not configured" and then with `acpx exited with code 1`.
Root Cause
The ACPX runtime plugin (`acpx`) was disabled by default, so no ACP backend was available. After enabling it, Polly still failed because ACP spawn paths for named local agents remained unreliable in this setup.
Solution
Enabled the `acpx` plugin in OpenClaw config and restarted the gateway. Added explicit routing rules in Polly's `SOUL.md` and `TOOLS.md` to use direct local dispatch (`openclaw agent --agent <id> --message ...`) as the default delegation path, with ACP spawn as optional/fallback only when healthy.
Notes
This keeps delegation functional even when ACP spawn is unstable for named agents.

[2026-04-08] - Polly Reported Only Herself As Active
Problem
Polly answered that only her own main session was active, despite all agents being registered and runnable.
Root Cause
Polly was inferring "active agents" from current chat/session context instead of live gateway/session state.
Solution
Updated Polly policy to check runtime truth using `openclaw health --verbose` and `openclaw agents list` before reporting active agents, and to report both configured and recently active counts.
Notes
After refresh, Polly correctly reported full configured and active agent counts.

[2026-04-08] - OpenClaw CLI Failed With Unsafe Temp Dir
Problem
OpenClaw commands intermittently failed with `Unsafe fallback OpenClaw temp dir`.
Root Cause
The expected trusted temp directory for OpenClaw was not consistently configured across shells.
Solution
Created `~/.openclaw/tmp`, set secure permissions, and exported `OPENCLAW_TMP_DIR="$HOME/.openclaw/tmp"` in shell profile.
Notes
This removes the temp-dir guard failure and stabilizes CLI execution.

[2026-04-08] - Rex Year-Long Gmail Backfill With Quota Backoff
Problem
Rex contact sync was limited to recent windows and needed to keep traversing older Gmail history until a full 365-day context was built.
Root Cause
The staged sync process lacked explicit quota-aware pause logic and did not have a dedicated recurring backfill schedule for the 365-day horizon.
Solution
Updated `rex_sync_contacts.py` to support exponential quota backoff with persisted cooldown state, checkpointed resume tokens, cycle-complete cooldowns, and per-sender document upserts. Added a dedicated OpenClaw cron job `rex-backfill-365d-20m` that runs every 20 minutes with a separate checkpoint file to avoid collisions with the existing 14-day incremental sync.
Notes
Backfill now progresses page-by-page across the last year and throttles itself automatically when Gmail rate limits are hit.

[2026-04-08] - Domain Agents Left On Generic Template Souls
Problem
Multiple OpenClaw domain agents were still running default template `SOUL.md`, `TOOLS.md`, and placeholder `IDENTITY.md` files, causing generic behavior and weak orchestration quality.
Root Cause
The rollout created all agents, but many workspaces never received role-specific post-bootstrap files from the implementation guide.
Solution
Replaced template files with concrete role-specific identity, soul, and tools definitions for Finn, Prof, Uhura, Emma, John, Balt, Lex, Spark, Trip, and updated Polly/Maxwell/Weber local policy files. Added explicit untrusted-content clauses and approval boundaries so external content cannot override local policy.
Notes
This fixes the "generic soul" regression and improves delegation reliability for Polly.

[2026-04-08] - Ingestion Loop Needed Persistent Historical Maxwell Backfill
Problem
Maxwell was sweeping recent inbox updates but did not have a recurring historical backfill loop to keep progressing across older mailbox history while Louis is away.
Root Cause
Only near-real-time intake cron existed for Maxwell (`gmail-sweep-5m`); no scheduled long-horizon continuation job was configured.
Solution
Ran an immediate Maxwell 12-month historical ingestion pass and added cron `gmail-backfill-12m-20m` for recurring quota-safe continuation. Added Polly cron `ingestion-watch-15m` so orchestration snapshots and urgent queue refresh continue unattended.
Notes
Backfill and orchestration now continue automatically in read-only mode with explicit no-send constraints.

[2026-04-09] - Model Routing Drifted To Local Primary
Problem
The active default model had drifted to `ollama/qwen3.5:27b` with Codex no longer primary, conflicting with desired rollout policy.
Root Cause
Model defaults in OpenClaw config were changed earlier during experimentation, and fallback settings were inconsistent across update attempts.
Solution
Reset model routing to `openai-codex/gpt-5.3-codex` as primary and `ollama/gemma4:26b` as fallback. Verified via `openclaw models status --json` and updated operational metrics collection to continuously report model routing state.
Notes
`gemma4:27b` is not available in local Ollama inventory on this machine; `gemma4:26b` is the closest installed Gemma 4 model.

[2026-04-09] - Polly Turn Latency/Timeout Under Session Lock Contention
Problem
Polly feature-latency check intermittently timed out and fell back after gateway timeout.
Root Cause
Polly session file lock contention (`*.jsonl.lock`) blocked timely session writes, causing repeated lane timeout errors during turn execution.
Solution
Added automated metrics capture to detect this regression (`scripts/collect_openclaw_metrics.sh`) and recorded lock-based failure evidence in runtime metrics reports for operational follow-up.
Notes
This is currently observable as high Polly latency and occasional non-zero return code in metrics snapshots.

[2026-04-09] - Cron Stability Regression In Ingestion/Outlook/Rex Loops
Problem
Three core unattended cron flows became unreliable: Polly ingestion-watch timed out, Otto sweep intermittently failed to write sweep logs, and Rex backfill showed delivery-path errors despite successful sync work.
Root Cause
Cron payloads were too verbose for isolated runs under model/lock pressure, Otto used a relative log-write path that failed under some runtime contexts, and Rex cron delivery mode drifted to `announce` (Telegram target required) instead of `none`.
Solution
Retuned cron payloads to compact deterministic prompts, reduced thinking level to `low`, tightened timeouts where appropriate, changed Otto sweep logging to an absolute workspace path, and reset Rex delivery to `none`. Verified with manual cron runs: Polly `ok` in ~39s, Otto `ok` in ~56s, and active cron board returned to `ok` across enabled jobs.
Notes
Post-fix metrics snapshot (`runtime_metrics/20260409T190110Z`) shows improved latency (`polly 6.251s`, `rex 6.24s`, `maxwell 17.038s`) with `security critical=0`.

[2026-04-10] - Rex Backfill Cron Recurred Into Itself
Problem
`rex-backfill-365d-20m` repeatedly reported "already running" and did not advance work during cron-triggered runs.
Root Cause
The Rex cron turn had broad tool access and called the `cron` tool on its own job id (`cron.run` recursion), which returned `reason=already-running` and short-circuited real ingestion work.
Solution
Edited Rex cron to enforce deterministic execution: restricted tools to `exec,read,write`, switched to an explicit `python3 rex_sync_contacts.py` command with the dedicated `rex_sync_checkpoint_365d.json`, and required raw stdout JSON return. Also restored schedule to every 20 minutes.
Notes
After the fix, Rex cron returned real JSON stats (`pages_read/messages_scanned/total_connections/next_page_token`) and resumed checkpoint progression.

[2026-04-10] - Maxwell Cron Timeout Loop From Subagent TaskFlow Drift
Problem
`gmail-sweep-5m` accumulated timeouts and stale running TaskFlows, causing repeated cron instability and queue contention.
Root Cause
Maxwell cron used spawn-style subagent execution; stale subagent run records remained active in `~/.openclaw/subagents/runs.json`, reviving running TaskFlows even after process loss/restarts.
Solution
Cleaned stale task/subagent state (with backups), marked orphaned subagent runs terminal in `runs.json`, restarted gateway, and hardened Maxwell cron to run in-session only with restricted tools (`exec,read,write`), `thinking=off`, and a deterministic read-only sweep prompt that writes directly to Maxwell memory artifacts.
Notes
Post-fix Maxwell cron returned to `ok` with `consecutiveErrors=0` and a successful latest duration (`~82.9s`) under Ollama primary routing.

[2026-04-10] - Metrics Collector Could Stall On Hanging Agent Probes
Problem
`scripts/collect_openclaw_metrics.sh` could run indefinitely when an agent CLI call hung, preventing reproducible metrics collection.
Root Cause
The Python latency probe used `subprocess.run(...)` without a hard process timeout, so tool hangs could block the script forever.
Solution
Added explicit per-agent hard timeouts to latency probes, surfaced `timed_out` in output JSON, and kept structured excerpts for timeout diagnostics.
Notes
With runtime stabilized, a fresh metrics snapshot (`runtime_metrics/20260410T065202Z`) completed successfully with non-timeout latencies (`polly 25.124s`, `rex 43.745s`, `maxwell 48.543s`).

[2026-04-10] - Maxwell 12-Month Backfill Was Disabled And Context-Heavy
Problem
Historical Maxwell ingestion over the past year was no longer advancing because the `gmail-backfill-12m-20m` cron had been disabled, and status prompts were reading a very large state file.
Root Cause
During earlier stabilization work, the backfill cron remained disabled. The backfill state format also accumulated full message listings, which made readback prompts expensive and increased timeout risk under Ollama.
Solution
Added deterministic script `scripts/maxwell_backfill_tick.py` that performs one quota-aware Gmail page tick via `gog -j`, persists compact checkpoint state, writes per-run summaries, and applies exponential backoff markers. Reconfigured and enabled cron `292d2a4f-fd28-4b06-bd94-29283a902753` to run this script with `toolsAllow=exec,read,write`, `thinking=off`, `lightContext=true`, and `timeoutSeconds=300`.
Notes
Checkpoint now advances again (`gmail-backfill-12m-checkpoint.json`), and cron status returned to `ok` without large-prompt parsing.

[2026-04-10] - Metrics Collector Timeout Handler Failed On Python 3.14 Bytes
Problem
Metrics collection failed after an agent timeout with `TypeError: can only concatenate str (not "bytes") to str`.
Root Cause
`subprocess.TimeoutExpired` payloads (`stdout`/`stderr`) can be bytes in this runtime, but the collector timeout path assumed strings.
Solution
Updated `scripts/collect_openclaw_metrics.sh` with a `to_text` helper to safely decode bytes before building the timeout excerpt.
Notes
Post-fix metrics run succeeded: `runtime_metrics/20260410T071325Z`.
