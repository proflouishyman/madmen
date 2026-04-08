# JHU Outlook Access Test (Microsoft Graph)

This is a minimal mailbox access probe for a JHU Microsoft 365 mailbox.

Script: `scripts/test_jhu_email_access.py`

## What it validates

The script runs OAuth device code flow and then calls Microsoft Graph:

- `GET /v1.0/me`
- `GET /v1.0/me/messages?$top=5`

If both calls succeed, mailbox access is confirmed.

## One-time Azure app setup

1. In Microsoft Entra ID, create an app registration.
2. Under **Authentication**, enable **Allow public client flows** (required for device code flow).
3. Under **API permissions** (Delegated), add:
   - `User.Read`
   - `Mail.Read`
4. Grant admin consent if your tenant policy requires it.
5. Copy the **Application (client) ID**.

## Run

```bash
python3 scripts/test_jhu_email_access.py --client-id <YOUR_APP_CLIENT_ID>
```

Optional flags:

- `--tenant <tenant-id|organizations|common>` (default: `organizations`)
- `--expected-domain <domain>` (default: `jh.edu`)

Example with explicit tenant:

```bash
python3 scripts/test_jhu_email_access.py \
  --client-id <YOUR_APP_CLIENT_ID> \
  --tenant <YOUR_JHU_TENANT_ID> \
  --expected-domain jh.edu
```

## Expected result

- You get a device login prompt/code.
- After sign-in, the script prints your mailbox identity and recent message headers.
- Final line: `Mailbox access check: PASS`

If sign-in or permissions fail, it exits with `Mailbox access check: FAIL` and prints the API error.
