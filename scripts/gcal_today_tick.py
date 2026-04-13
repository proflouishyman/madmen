#!/usr/bin/env python3
"""Fetch today + tomorrow's Google Calendar events via gog and write to JSON.

Uses the same `gog` CLI tool that maxwell/rex use for Gmail. Calendar scope
requires that gog has been authorized with the calendar.readonly scope.

If gog does not support calendar yet, this script prints a clear error with
instructions to grant the scope, rather than failing silently.

Output: ~/.openclaw/workspaces/maxwell-workspace/memory/gcal-today.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCOUNT = os.environ.get("GCAL_ACCOUNT", "lhyman@gmail.com")
WORKSPACE = Path.home() / ".openclaw" / "workspaces" / "maxwell-workspace" / "memory"
OUTPUT = WORKSPACE / "gcal-today.json"
WORKSPACE.mkdir(parents=True, exist_ok=True)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def rfc3339(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def write_result(status: str, events: list, error: str = "") -> None:
    today = now_utc().strftime("%Y-%m-%d")
    result = {
        "status": status,
        "source": "google_calendar",
        "account": ACCOUNT,
        "generated_at": rfc3339(now_utc()),
        "date_range": {
            "from": today,
            "to": (now_utc() + timedelta(days=1)).strftime("%Y-%m-%d"),
        },
        "events": events,
        "error": error,
    }
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": status, "events": len(events), "error": error}))


def fetch_via_gog() -> list:
    """Call gog calendar events for today + tomorrow.

    gog calendar interface:
        gog calendar events <calendarId> --from <iso> --to <iso> --account <email> --json

    calendarId is "primary" for the default Google Calendar.
    Requires that gog has been authorized with calendar scope:
        gog auth add lhyman@gmail.com --services gmail,calendar,drive,contacts,docs,sheets
    """
    start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=2)

    cmd = [
        "gog", "calendar", "events", "primary",
        "--from", rfc3339(start),
        "--to",   rfc3339(end),
        "--account", ACCOUNT,
        "--json",
    ]

    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError("gog not found on PATH — must run inside an OpenClaw agent turn")
    except subprocess.TimeoutExpired:
        raise RuntimeError("gog calendar timed out after 60s")

    if cp.returncode != 0:
        stderr = (cp.stderr or cp.stdout or "").strip()
        # Detect missing scope clearly
        if any(phrase in stderr.lower() for phrase in [
            "insufficient authentication scopes",
            "calendar",
            "scope",
            "unauthorized",
            "forbidden",
            "permission",
        ]):
            raise RuntimeError(
                "Google Calendar scope not yet granted. "
                "Re-authorize gog with calendar scope:\n"
                "  gog auth add lhyman@gmail.com --services gmail,calendar,drive,contacts,docs,sheets\n"
                "(This will open a browser OAuth prompt to re-grant permissions.)"
            )
        raise RuntimeError(f"gog calendar exited {cp.returncode}: {stderr[:300]}")

    # gog may output nothing if no events (empty day) — treat as empty list
    if not cp.stdout.strip():
        return []

    try:
        raw = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gog returned invalid JSON: {exc}") from exc

    # gog returns a list of event objects directly, or { items: [...] }
    items = raw.get("items", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise RuntimeError(f"Unexpected gog calendar shape: {type(items)}")

    events = []
    for item in items:
        start_raw = item.get("start", {})
        end_raw   = item.get("end", {})
        # All-day events use 'date'; timed events use 'dateTime'
        start_str = start_raw.get("dateTime", start_raw.get("date", ""))
        end_str   = end_raw.get("dateTime",   end_raw.get("date", ""))
        events.append({
            "id":         item.get("id", ""),
            "summary":    item.get("summary", "(no title)"),
            "start":      start_str,
            "end":        end_str,
            "location":   item.get("location", ""),
            "organizer":  item.get("organizer", {}).get("email", ""),
            "attendees":  len(item.get("attendees", [])),
            "all_day":    "date" in start_raw and "dateTime" not in start_raw,
            "status":     item.get("status", "confirmed"),
            "html_link":  item.get("htmlLink", ""),
        })
    return events


def main() -> None:
    try:
        events = fetch_via_gog()
        write_result("ok", events)
    except RuntimeError as exc:
        msg = str(exc)
        # Write a no_data result so polly_ingest doesn't break — it just skips calendar
        write_result("error", [], error=msg)
        # Print to stderr so cron logs capture it
        print(f"gcal_today_tick: {msg}", file=sys.stderr)
        # Exit 0 so the cron job doesn't mark itself as errored — calendar is best-effort
        sys.exit(0)


if __name__ == "__main__":
    main()
