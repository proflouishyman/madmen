[2026-04-13] - polly_ingest agent_health Idempotency Bug
Problem
polly_ingest.py total_new counted 5 new items on every run because agent_health rows were processed as new even when they were upserts of existing rows.
Root Cause
The INSERT ON CONFLICT DO UPDATE always runs for all 5 agents every cycle. count += 1 fired unconditionally, including for rows that were updated rather than inserted for the first time.
Solution
Added a pre-check (SELECT 1 FROM agent_health WHERE agent_id = ?) before the upsert. count is only incremented when the row did not already exist. Health rows now upsert silently every run without inflating total_new.
Notes
idempotency test now passes: total_new=0 on second run.

[2026-04-13] - polly_ingest.py gmail=0 Due to Wrong Field Name + exec Allowlist Miss
Problem
polly_ingest.py always returned gmail=0 even though gmail-intake-latest.json had 20 threads with real classifications. Separately, backer-health-5m cron returned "exec denied: allowlist miss" on every run.
Root Cause
Two independent bugs: (1) Maxwell writes the classification field as "class" in gmail-intake-latest.json, but polly_ingest.py read "classification" — a field that never existed, causing all threads to default to "fyi" and zero new records. (2) OpenClaw's exec tool with security="allowlist" checks a configured allowlist in openclaw.json, not TOOLS.md. The "exec-allowlisted" heading in TOOLS.md caused the LLM to use security="allowlist" mode, but no allowlist was configured. Also, backer/otto/rex lacked tools.allow: ["exec"] in openclaw.json (unlike maxwell which had it).
Solution
(1) Fixed field lookup to `thread.get("class", thread.get("classification", "fyi"))` to handle both. (2) Added tools.allow: ["exec", "read", "write"] to backer, otto, rex in openclaw.json. Renamed TOOLS.md section heading from "exec-allowlisted" to plain "Scripts" with explicit note to use ask:"off" not security:"allowlist". Updated both workspace and sandbox TOOLS.md for all affected agents.
Notes
The sandbox TOOLS.md at .openclaw/sandboxes/agent-*/TOOLS.md is the file read during actual agent runs — not the workspace TOOLS.md. Both must be kept in sync.

[2026-04-13] - gcal_today_tick.py Used Wrong gog Calendar Flags
Problem
gcal_today_tick.py called gog with Google Calendar API raw flags (--time-min, --time-max, --max-results, --single-events, --order-by, -j) which are not valid gog CLI flags. The script would fail immediately on any calendar fetch. Additionally, the error message told the user to run `openclaw accounts setup --scope calendar.readonly` which is a non-existent command.
Root Cause
gog wraps the Google Calendar API with its own simplified flag set: `gog calendar events <calendarId> --from <iso> --to <iso> --account <email> --json`. The initial implementation incorrectly used raw API query parameters instead of gog's CLI interface.
Solution
Rewrote the gog invocation in gcal_today_tick.py to: `gog calendar events primary --from <iso> --to <iso> --account <email> --json`. Updated the error message to give the correct re-auth command: `gog auth add lhyman@gmail.com --services gmail,calendar,drive,contacts,docs,sheets`. Also added empty-output handling (gog returns nothing on empty days rather than `[]`).
Notes
The calendarId "primary" refers to the user's default Google Calendar. To add calendar scope to an existing gog auth, re-run `gog auth add` with all services including calendar — this triggers a browser OAuth re-grant without removing existing tokens.

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

[2026-04-11] - Runtime Reconciler Missed Impossible Running Task Rows
Problem
OpenClaw task rows were observed in an impossible state (`status=running` while `endedAt` and terminal `error` were already set), which left CLI calls hanging and kept stale run markers in runtime state.
Root Cause
The reconciler only selected stale runs by age (`last_event_at` cutoff) and did not treat terminal-marked `running` rows as immediately invalid.
Solution
Updated `scripts/reconcile_runtime_state.py` to reconcile `running` rows immediately when `ended_at` or `terminal_outcome` exists, regardless of grace window. Added regression tests in `scripts/test_reconcile_runtime_state.py` to prove immediate cleanup for this edge case and preserve normal recent-running behavior.
Notes
This is a mitigation for upstream task lifecycle inconsistency; OpenClaw can still emit inconsistent timestamp rows under heavy failover churn, but stale queue blockage is now auto-cleared.

[2026-04-11] - Ollama Shadow Wrapper Param Handling Too Narrow
Problem
Ollama shadow-provider controls were brittle when provider/param shapes varied, reducing reliability of cache-control injection across custom Ollama provider aliases.
Root Cause
Wrapper logic matched only `provider.id === "ollama"` and expected params strictly under `params.ollama`, so alternative provider ids and flattened param forms were ignored.
Solution
Expanded shadow wrapper matching to any provider with `api="ollama"` and added flattened-param support for cache and reliability parsing in `plugins/ollama/lib/cache-controls.js`. Added unit coverage in `plugins/ollama/test/cache-controls.test.mjs` for flattened inputs and custom-provider patching.
Notes
Reliability controls still depend on OpenClaw’s stream wrapper compatibility path; this change improves coverage without changing upstream OpenClaw contracts.

[2026-04-11] - Reconciler Missed Superseded Cron Running Rows And Lock-Heal Telemetry
Problem
Cron/task state could remain stuck with a `running` row even after newer runs for the same cron job had already ended, and Polly lane lock incidents needed explicit runtime healing visibility.
Root Cause
`scripts/reconcile_runtime_state.py` only reconciled duplicate concurrent `running` cron rows; it did not reconcile older `running` rows that were superseded by newer terminal runs (`succeeded/failed/timed_out/lost`). Backer health output also did not expose stale-lock detection/heal state.
Solution
Updated `scripts/reconcile_runtime_state.py` to mark stale `running` cron rows as `lost` when a newer terminal run exists for the same `source_id`, and added deterministic tests in `scripts/test_reconcile_runtime_state.py` for both duplicate-running and superseded-running cron scenarios. Extended `scripts/backer_health_tick.sh` with stale lock detection, gateway restart healing for stale OpenClaw lock holders, and telemetry fields (`stale_locks_detected`, `stale_lock_heal_triggered`).
Notes
This is an operational safeguard for upstream scheduler/runtime drift; it prevents stale bookkeeping from persisting and gives explicit lock-heal observability in Backer logs.

[2026-04-11] - Otto Cron Test Broke On JSON Warning Preamble
Problem
`scripts/test_otto_suite.sh --quick` reported `FAIL: Otto cron coverage - Could not parse cron JSON` even when cron jobs existed.
Root Cause
`openclaw cron list --json` can emit config warning lines before the JSON body. The suite attempted to parse full stdout directly as JSON and failed.
Solution
Updated `scripts/test_otto_suite.sh` to strip non-JSON preamble content and parse from the first `{` before counting Otto cron jobs.
Notes
After the fix, `Otto cron coverage` passes reliably while preserving strict JSON parsing of the payload body.

[2026-04-11] - Polly Session Model Drift Caused Slow/Unreliable Replies
Problem
Polly intermittently stopped responding promptly and routed direct turns through slower fallback paths, including lock contention cascades after retries.
Root Cause
OpenClaw persisted an `auto` session-level model override on `agent:polly:main` (`modelOverride=gemma4:26b`), pinning Polly away from the configured fast lane. When retries overlapped, a stale running task could hold the session lock and block new turns.
Solution
Added a guard to `scripts/backer_health_tick.sh` that detects and clears drifted Polly `auto` model overrides from `~/.openclaw/agents/polly/sessions/sessions.json` so Polly returns to configured routing. Added health telemetry field `polly_route_reset_applied` to confirm automatic correction in logs.
Notes
A one-time runtime cleanup was also applied: stale Polly running task reconciled as `lost`, stale session lock healed via gateway restart, and Backer auth profiles were synced with `ollama-polly:local` marker to prevent repeated auth fallback noise.

[2026-04-11] - Backer Health Cron Persisted On Polly Lane
Problem
`backer-health-5m` repeatedly stuck in `running`, reported `already-running`, and starved cron recovery while Maxwell ingestion freshness regressed.
Root Cause
The cron job retained a stale `model` override (`ollama-polly/qwen2.5:7b-instruct`) from prior config state. `openclaw cron edit` does not clear model overrides unless a new model is explicitly set, so Backer continued competing on Polly's dedicated lane and inherited Polly-timeout/fallback churn.
Solution
Updated `scripts/apply_polly_resilience_addendum.sh` to set Backer health model explicitly (`BACKER_HEALTH_MODEL_KEY`, default `ollama/gemma4:26b`) on both cron create and edit paths, ensuring the job never persists on Polly's dedicated provider lane.
Notes
This preserves the addendum invariant: Polly lane is reserved for Polly direct-response reliability; infrastructure health automation runs on the regular local Ollama lane.

[2026-04-13] - Comprehensive Stability Overhaul: 3-Tier Model Routing, Cron Cleanup, Disk Bloat Fix
Problem
Multiple interlocking instability issues accumulated: (1) reconciler backup files grew to 399 copies / 325 MB (actual DB is 2.1 MB); (2) plugin path in openclaw.json pointed to standalone ~/openclaw-ollama-kv-cache-plugin instead of repo subfolder ~/openclaw/openclaw-ollama-kv-cache-plugin; (3) no fallback model configured after Codex credits exhausted (empty fallbacks array); (4) pre-digest healthcheck cron still competing on Polly dedicated lane (ollama-polly/qwen2.5:7b-instruct); (5) three duplicate otto-outlook-sweep cron jobs created by the agent itself; (6) morning digest timing out at 967s with oversized ACP-dependent prompt; (7) multiple cron jobs missing model/timeout/tools constraints; (8) backer had 2 stale running sessions.
Root Cause
Organic growth without centralized model allocation policy. Cron jobs defaulted to the primary gemma4:26b model regardless of task complexity. Simple exec-and-return tasks (run script, return stdout) consumed the same GPU resources and inference queue as complex classification tasks. The reconciler created a full database backup on every 5-minute health tick without pruning, causing unbounded disk growth. Plugin path drifted when the plugin was moved into the repo subfolder but the config was not updated. Codex removal left no fallback path.
Solution
Implemented 3-tier model routing:
- Tier 1 (Polly fast): ollama-polly/qwen2.5:7b-instruct on port 11435 (unchanged, user-facing only)
- Tier 2 (Light): ollama/qwen2.5:7b on port 11434, keepAlive=10m, num_batch=8 — for deterministic exec-and-return crons (backer-health, maxwell-backfill, rex-sync, rex-backfill, backer-nightly-backup, otto-draft-check, backer-daily-audit, pre-digest-healthcheck)
- Tier 3 (Heavy): ollama/gemma4:26b on port 11434, keepAlive=45m, num_batch=16 — for classification/reasoning tasks (gmail-sweep, otto-outlook-sweep, otto-slack-digest, morning-digest, otto-outcome-sweep)

Additional fixes:
- Deleted 398 stale reconciler backups (325 MB → 11 MB)
- Updated _backup_db() to auto-prune, keeping only 5 most recent backups
- Fixed plugin path to repo subfolder
- Added ollama/qwen2.5:7b as fallback for gemma4:26b (no Codex available)
- Registered qwen2.5:7b in ollama provider models list
- Removed 3 duplicate otto-outlook-sweep cron jobs (agent-created)
- Slimmed morning digest prompt to query polly.db and read files directly instead of ACP delegation to 6 agents, raised timeout to 600s
- Added explicit model, timeout, thinking=off, lightContext, and toolsAllow to all unconstrained cron jobs
- Cleaned 2 stale backer running sessions
- Cleared all stale consecutiveErrors counters
Notes
The light tier (qwen2.5:7b) with num_batch=8 and keepAlive=10m yields GPU quickly to the heavy tier. Tasks routed to the light tier barely need LLM reasoning — they parse a simple "run this command and return stdout" instruction. This frees gemma4:26b context/cache for tasks that actually need classification and reasoning. The KV cache plugin's keep_alive injection benefits both tiers independently.

[2026-04-13] - 3-Lane Ollama Architecture: Separate Instances to Prevent GPU Memory Competition
Problem
The 2-tier model routing (both models sharing port 11434) caused GPU memory competition. Ollama swaps models in/out of GPU memory on a single instance, so loading qwen2.5:7b (4.7 GB) would evict gemma4:26b's warm KV cache (17 GB), destroying the 14x speedup from the KV cache plugin. Every time a light cron task ran, the heavy model's cache was cold on the next request.
Root Cause
Single Ollama instance cannot hold multiple models in GPU memory simultaneously without eviction. The keep_alive setting only delays eviction — it cannot prevent it when a different model is loaded on the same instance. The fundamental constraint is that GPU VRAM is shared within a process.
Solution
Implemented 3 dedicated Ollama instances on separate ports, each holding exactly one model permanently:
- Port 11434: Heavy lane (gemma4:26b) — classification, reasoning, digests. keepAlive=45m, num_batch=16.
- Port 11435: Polly lane (qwen2.5:7b-instruct) — user-facing fast path. keepAlive=-1 (permanent), OLLAMA_MAX_LOADED_MODELS=1.
- Port 11436: Light lane (qwen2.5:7b) — deterministic cron exec tasks. keepAlive=10m, num_batch=8, context_length=4096.

Scripts created/updated:
- NEW: scripts/setup_light_ollama_lane.sh — provisions light lane launchd plist, starts instance, prewarms model, registers ollama-light provider in openclaw.json, migrates cron jobs from ollama/qwen2.5:7b to ollama-light/qwen2.5:7b
- UPDATED: scripts/backer_health_tick.sh — monitors all 3 ports, restarts light lane if unresponsive, includes light_ok/light_restarted in JSON health summary
- UPDATED: scripts/start_openclaw_gateway_with_kv_checks.sh — enforces startup config for all 3 lanes (heavy + polly + light model params and reliability controls)

Plugin audit:
- KV cache plugin (openclaw-ollama-kv-cache-plugin/) verified: 10/10 tests pass, patchOllamaProviderDefinition correctly patches all providers with api="ollama" (covers heavy, polly, and light lanes)
- Plugin scripts/ directory documented as historical — canonical versions live in main scripts/
- Generated shim (bundled-ollama-entry.js) verified valid for /opt/homebrew OpenClaw install
Notes
Each Ollama instance must be a separate OS process to hold its own GPU memory region. The OLLAMA_MAX_LOADED_MODELS=1 on Polly and light lanes prevents accidental multi-model loading. The shared OLLAMA_MODELS path means all three instances read from the same model cache on disk — no duplication. The setup_light_ollama_lane.sh must be run on the user's Mac (requires launchctl GUI session). After running, cron jobs referencing ollama/qwen2.5:7b are automatically migrated to ollama-light/qwen2.5:7b.

[2026-04-13] - Light Lane Context Window Too Small (4096 < 16000 Minimum)
Problem
After setting up the light Ollama lane on port 11436, two cron jobs (backer-health-5m and gmail-backfill-12m-20m) started failing with "FailoverError: Model context window too small (4096 tokens). Minimum is 16000."
Root Cause
The light lane launchd plist set OLLAMA_CONTEXT_LENGTH=4096 and the openclaw.json provider registration set contextWindow=4096. OpenClaw requires a minimum 16000-token context window for any agent turn, even simple exec-and-return tasks, because the system prompt and framing consume baseline tokens.
Solution
Updated setup_light_ollama_lane.sh to use OLLAMA_CONTEXT_LENGTH=16384 and contextWindow=16384. Patched the live openclaw.json and launchd plist to match. The 16K context is still small enough to keep GPU memory low (~5 GB) while meeting OpenClaw's minimum.
Notes
Re-run setup_light_ollama_lane.sh to apply the plist change and restart the light instance.

[2026-04-13] - polly.db Empty: Missing Ingestion Pipeline
Problem
polly.db had zero rows in all 16 tables despite Maxwell, Otto, and Rex producing data for days. The morning digest cron (polly-morning-digest) queried empty tables and timed out. The ingestion-watch-15m cron was disabled and had never worked — its prompt was a vague description with no actual SQL/Python implementation.
Root Cause
No script existed to bridge agent output files (gmail-intake-latest.json, sweep-log.yaml, connections.db) with polly.db's structured tables. The ingestion-watch-15m cron relied on the LLM itself to figure out how to read JSON files and write SQL, which consistently failed or timed out because the task requires deterministic parsing, not LLM reasoning.
Solution
Created scripts/polly_ingest.py — a deterministic Python script that:
- Reads gmail-intake-latest.json: urgent emails → escalations table, today emails → tasks table
- Reads otto sweep-log.yaml: URGENT/PRIORITY items → escalations table
- Reads cron/jobs.json: per-agent latest status → agent_health table
- Reads connections.db: contacts with 90+ day inactivity → waiting_on table
- Cleans resolved/dismissed items older than 7 days
- Uses stable SHA-256 IDs for idempotency (safe to re-run)
Re-enabled ingestion-watch cron (renamed to ingestion-watch-20m), now running every 20 minutes on the light lane, calling polly_ingest.py directly. Timeout reduced from 420s to 120s since the script runs in ~2 seconds.
Notes
The morning digest should now find data in polly.db. See also the calendar ingestion entry below.

[2026-04-13] - Calendar Not Wired: Polly Had No Calendar Access
Problem
Polly's morning digest never showed calendar events. No agent was reading either Outlook or Google Calendar into polly.db. Otto had AppleScript calendar code in TOOLS.md but no cron job and no output file. Google Calendar had zero integration.
Root Cause
The Otto year ingestion script (start_otto_year_ingestion.sh) was written but never run. polly.db had an events table but nothing populated it. Polly's morning digest instructions had no calendar section.
Solution
Three scripts created:
- scripts/otto_calendar_tick.sh: AppleScript reads today+tomorrow Outlook calendar events → otto-workspace/state/calendar-today.yaml. Handles Outlook not running gracefully.
- scripts/gcal_today_tick.py: Calls `gog -a lhyman@gmail.com calendar events list` → maxwell-workspace/memory/gcal-today.json. Exits 0 with status=error if gog calendar scope not yet granted (best-effort, does not break digest).
- scripts/polly_ingest.py: Extended with ingest_outlook_calendar() and ingest_google_calendar() that parse both files into polly.db events table. Idempotent, stable IDs prevent duplicates.

Two new cron jobs added:
- otto-calendar-6am: daily 6:00 AM ET, runs otto_calendar_tick.sh via Otto (light lane)
- maxwell-gcal-6am: daily 6:05 AM ET, runs gcal_today_tick.py via Maxwell (light lane)
Both run before polly-pre-digest-healthcheck (6:50 AM) and polly-morning-digest (7:00 AM).

Polly SOUL.md updated: morning digest now starts with calendar section, shows today+tomorrow events sorted by time, flags prep_required events.
Exec allowlists updated: otto_calendar_tick.sh added to Otto TOOLS.md, gcal_today_tick.py added to Maxwell TOOLS.md.
Notes
Google Calendar requires gog to have calendar.readonly scope. If not yet granted, gcal_today_tick.py writes status=error to gcal-today.json and exits 0 — Outlook calendar still works. To grant Google Calendar scope: openclaw accounts setup --account lhyman@gmail.com --scope calendar.readonly (run on Mac). The two calendars are synthesized in polly.db events table — deduplication by title+time happens at query time in Polly's digest. Events, commitments, and drafts tables will populate as Otto writes structured sweep entries and agents create drafts. The stale-contact check (waiting_on) seeds 20 entries initially — these can be resolved by Polly or Louis.

[2026-04-13] - ollama-light Provider Missing Auth in Agent Profiles
Problem
After adding the ollama-light provider, all agents using it (backer, rex, maxwell, otto, polly, and others) failed with "No API key found for provider ollama-light". The context window fix made the error visible — the first failure was context window, and once that was fixed, auth was the next blocker.
Root Cause
OpenClaw's custom provider auth model requires each agent to have an explicit entry in its own auth-profiles.json for any non-bundled provider. The regular `ollama` provider is bundled and needs no per-agent entry. The `ollama-polly` provider had a correct entry (type: api_key, key: ollama-local). The `ollama-light` provider was added to the global config but not to any agent's auth-profiles.json.
Solution
Patched all 18 agent auth-profiles.json files to add "ollama-light:local": { "type": "api_key", "provider": "ollama-light", "key": "ollama-local" }.

[2026-04-13] - Backer Exec Allowlist Missing backer_health_tick.sh
Problem
Backer's health cron was denied with "exec denied: allowlist miss" when trying to run /Users/louishyman/openclaw/scripts/backer_health_tick.sh.
Root Cause
OpenClaw exec security="allowlist" mode checks the command against the agent's TOOLS.md allowed command list. The script path was never listed in backer's TOOLS.md.
Solution
Added the health script and polly_ingest.py to backer's TOOLS.md under "Health Scripts (exec-allowlisted)". Also added the light lane Ollama tools (port 11436 curl check) and launchctl restart commands for the light lane.
