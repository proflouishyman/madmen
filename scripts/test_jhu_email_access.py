#!/usr/bin/env python3
"""Minimal Microsoft Graph mailbox access probe for JHU email accounts.

This script uses OAuth 2.0 device code flow and then calls:
- GET /v1.0/me
- GET /v1.0/me/messages?$top=5

A successful run confirms sign-in and mailbox read access.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    """POST URL-encoded form data and return a JSON object response."""
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"error": f"HTTP {err.code}", "error_description": body}
        raise RuntimeError(
            f"POST {url} failed: {payload.get('error')} - {payload.get('error_description')}"
        ) from err


def get_json(url: str, access_token: str) -> dict[str, Any]:
    """GET a Graph endpoint with bearer auth and return parsed JSON."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {access_token}")
    request.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"error": f"HTTP {err.code}", "message": body}
        raise RuntimeError(f"GET {url} failed: {payload}") from err


def acquire_access_token(client_id: str, tenant: str, scopes: str) -> str:
    """Run device code flow and return an OAuth access token.

    Assumption: The Azure app is configured as a public client so device code
    flow is allowed.
    """
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    device_code_payload = post_form(
        f"{base}/devicecode",
        {
            "client_id": client_id,
            "scope": scopes,
        },
    )

    message = device_code_payload.get("message")
    if message:
        print(message)
    else:
        # Fallback formatting when message is omitted in unexpected responses.
        print(
            "Open",
            device_code_payload.get("verification_uri", "https://microsoft.com/devicelogin"),
            "and enter code",
            device_code_payload.get("user_code", "<unknown>"),
        )

    interval = int(device_code_payload.get("interval", 5))
    expires_at = time.time() + int(device_code_payload.get("expires_in", 900))

    while time.time() < expires_at:
        token_payload = post_form(
            f"{base}/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code_payload["device_code"],
            },
        )

        access_token = token_payload.get("access_token")
        if access_token:
            return access_token

        error = token_payload.get("error")
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        if error == "expired_token":
            break

        raise RuntimeError(
            f"Token request failed: {error} - {token_payload.get('error_description')}"
        )

    raise RuntimeError("Device code expired before sign-in completed.")


def summarize_messages(messages: list[dict[str, Any]]) -> None:
    """Print a short mailbox summary for human verification."""
    if not messages:
        print("Mailbox call succeeded, but no messages were returned.")
        return

    print("Recent messages:")
    for index, message in enumerate(messages, start=1):
        sender = (
            message.get("from", {})
            .get("emailAddress", {})
            .get("address", "<unknown sender>")
        )
        received = message.get("receivedDateTime", "<unknown time>")
        subject = message.get("subject") or "(no subject)"
        read_flag = "read" if message.get("isRead") else "unread"
        print(f"  {index}. [{read_flag}] {received} | {sender} | {subject}")


def main() -> int:
    """CLI entrypoint that authenticates and probes mailbox read access."""
    parser = argparse.ArgumentParser(
        description="Test Microsoft Graph access to a JHU mailbox via device code flow."
    )
    parser.add_argument(
        "--client-id",
        required=True,
        help="Azure app (public client) Application ID",
    )
    parser.add_argument(
        "--tenant",
        default="organizations",
        help="Tenant ID or organizations/common (default: organizations)",
    )
    parser.add_argument(
        "--expected-domain",
        default="jh.edu",
        help="Warn if signed-in mailbox does not match this domain (default: jh.edu)",
    )
    args = parser.parse_args()

    scopes = "User.Read Mail.Read"

    try:
        token = acquire_access_token(args.client_id, args.tenant, scopes)

        me = get_json(
            "https://graph.microsoft.com/v1.0/me?$select=displayName,mail,userPrincipalName,id",
            token,
        )
        upn = me.get("userPrincipalName") or "<unknown UPN>"
        mail = me.get("mail") or "<no primary mail field>"
        display_name = me.get("displayName") or "<unknown name>"

        print("\nAuthenticated successfully")
        print(f"  Name: {display_name}")
        print(f"  UPN:  {upn}")
        print(f"  Mail: {mail}")

        domain_target = args.expected_domain.lower()
        observed = f"{upn} {mail}".lower()
        if domain_target and domain_target not in observed:
            print(
                f"WARNING: expected domain '{domain_target}' not found in UPN/mail.",
                file=sys.stderr,
            )

        messages = get_json(
            "https://graph.microsoft.com/v1.0/me/messages"
            "?$top=5"
            "&$select=subject,receivedDateTime,from,isRead"
            "&$orderby=receivedDateTime desc",
            token,
        )
        summarize_messages(messages.get("value", []))

        print("\nMailbox access check: PASS")
        return 0
    except Exception as err:  # noqa: BLE001
        print(f"Mailbox access check: FAIL\n{err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
