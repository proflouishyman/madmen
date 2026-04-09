# OpenClaw Checklist Status (2026-04-09)

## Completed

- OpenClaw installed and gateway running.
- Model routing corrected to:
  - primary: `openai-codex/gpt-5.3-codex`
  - fallback: `ollama/gemma4:26b`
- Ollama local model availability confirmed (`gemma4:26b` present).
- All core agents present in registry (including `polly`, `maxwell`, `otto`, `rex`, `worf`, `forge`, domain agents).
- Telegram channels healthy for active configured accounts:
  - `polly`, `maxwell`, `otto`, `worf`
- Cron coverage active:
  - Maxwell intake sweep + 12m backfill
  - Rex 365d and 14d sync loops
  - Polly ingest watch + digest jobs
  - Otto sweep/digest checks
- Otto operational suite (quick) passes critical checks.
- Local backup created and verified.
- Runtime metrics and snapshot scripts implemented.

## In Progress

- Historical ingestion continues in checkpointed mode:
  - Maxwell 12m backfill (`complete=false`, next token present)
  - Rex 365d backfill (`cycle_complete=false`, next token present)

## Outstanding

- Full 16-bot Telegram rollout is not complete (currently 4 configured/running).
- Full Otto live turn test currently times out in this environment.
- Security audit still reports one critical item:
  - small local models allowed with web-capable tool surface unless sandbox hardening is applied.
- Approvals policy still needs explicit hardening pass.
- Heartbeat settings for non-main agents are still disabled.

## Latest Metrics Snapshot

From `runtime_metrics/latest/summary.md`:

- Telegram channels working: `4`
- Cron jobs enabled/total: `11/11`
- Security audit (critical/warn/info): `1/2/1`
- Otto quick suite (pass/warn/fail): `7/4/0`
- Agent latency:
  - `polly`: `112.454s` (lock/timeout path)
  - `rex`: `9.125s`
  - `maxwell`: `18.827s`
