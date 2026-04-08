# JHU Outlook Access Test (Playwright Fallback)

Use this when app registration/client ID is blocked.

Script: `scripts/test_jhu_email_access_playwright.sh`

## What it does

1. Opens Outlook Web in a headed browser via Playwright CLI.
2. You manually sign in with JHU credentials and complete MFA.
3. The script reads page state and samples top inbox row accessibility labels.

This verifies practical mailbox UI access without Microsoft Graph credentials.

## Prerequisite

`npx` must be installed:

```bash
npx --version
```

## Run

```bash
/Users/louishyman/openclaw/scripts/test_jhu_email_access_playwright.sh
```

Optional flags:

- `--expected-domain jh.edu`
- `--session jhu-outlook-check`
- `--keep-open`

## Expected result

- Browser opens to Outlook sign-in.
- After you authenticate, press Enter in terminal.
- Script prints observed URL/title and inbox row label sample.
- Final line is either:
  - `Outlook UI check: PASS`
  - `Outlook UI check: WARN ...`

## Notes

- This is a UI-level check; it is less stable than Graph API checks.
- If Outlook UI selectors change, row-label sampling may return an empty list even when access works.
