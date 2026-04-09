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
