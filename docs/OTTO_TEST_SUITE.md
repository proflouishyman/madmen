# Otto Test Suite

Script: `scripts/test_otto_suite.sh`

## Purpose

Validate that Otto is ready to process JHU Outlook mail on the intended path:

- Otto agent exists and channel is healthy
- Otto identity contract is Outlook-specific (not generic template)
- Local Outlook is reachable via AppleScript
- Optional CES Slack token path is valid (`SLACK_TOKEN_OTTO`)
- Otto cron coverage exists (or warns if missing)
- Optional live Otto turn stays Outlook-only (detects Gmail misrouting)

## Run

Quick checks (no live Otto turn):

```bash
/Users/louishyman/openclaw/scripts/test_otto_suite.sh --quick
```

Full checks (includes live Otto turn):

```bash
/Users/louishyman/openclaw/scripts/test_otto_suite.sh
```

## Exit codes

- `0`: critical checks passed (warnings may still exist)
- `1`: one or more critical checks failed

## Interpreting current common failures

### `FAIL: Otto identity contract`

Otto's `SOUL.md` is still generic and does not enforce Outlook boundaries.

Expected file to configure:

- `~/.openclaw/workspaces/otto-workspace/SOUL.md`
- `~/.openclaw/workspaces/otto-workspace/TOOLS.md`

These should include Outlook ownership and AppleScript tool policy.

### `WARN: Outlook inbox count ... 0 messages`

Outlook is reachable but mailbox data is not visible yet (unsynced or account not fully connected).

### `WARN: Otto cron coverage ...`

Otto has no scheduled sweeps; processing only happens when manually invoked.

### `FAIL: Otto live turn ... STATUS:error ... Exchange accounts=none`

Outlook is open, but Otto cannot see a connected Exchange mailbox yet.
Do not start long-range ingestion until this is resolved.

### `FAIL: Otto live turn - TIMEOUT ...`

The gateway could not complete a live Otto turn in time. In this environment,
the most common cause is upstream model rate limiting or provider cooldown.
Wait for cooldown and rerun before starting ingestion.

## Recommended command sequence after Outlook setup

```bash
# 1) Validate local Outlook path
/Users/louishyman/openclaw/scripts/test_otto_suite.sh --quick

# 2) Force a live Otto Outlook-only read
openclaw agent --agent otto --message "Read JHU Outlook inbox via AppleScript only. Return top 3 subjects and dates."

# 3) Re-run full suite
/Users/louishyman/openclaw/scripts/test_otto_suite.sh
```
