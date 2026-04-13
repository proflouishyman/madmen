#!/usr/bin/env python3
"""Polly ingestion pipeline — reads Maxwell/Otto/Rex output files and populates polly.db.

Purpose: Bridge the gap between agent data producers (Maxwell→gmail-intake-latest.json,
Otto→sweep-log.yaml, Rex→connections.db) and Polly's SQLite database that powers the
morning digest, escalation queue, and task tracking.

This script is designed to be run by the ingestion-watch cron job every 15–20 minutes.
It is idempotent: re-running with the same source data produces no duplicate rows.

Usage:
    python3 polly_ingest.py [--dry-run] [--verbose]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw"))
WORKSPACES = OPENCLAW_HOME / "workspaces"

POLLY_DB = WORKSPACES / "polly-workspace" / "polly.db"
GMAIL_INTAKE = WORKSPACES / "maxwell-workspace" / "memory" / "gmail-intake-latest.json"
OTTO_SWEEP = WORKSPACES / "otto-workspace" / "state" / "sweep-log.yaml"
OTTO_CALENDAR = WORKSPACES / "otto-workspace" / "state" / "calendar-today.yaml"
GCAL_TODAY = WORKSPACES / "maxwell-workspace" / "memory" / "gcal-today.json"
CONNECTIONS_DB = WORKSPACES / "rex-workspace" / "connections.db"
CRON_JOBS = OPENCLAW_HOME / "cron" / "jobs.json"

log = logging.getLogger("polly-ingest")


def stable_id(*parts: str) -> str:
    """Generate a deterministic ID from input parts to prevent duplicate rows."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def ensure_db(db_path: Path) -> sqlite3.Connection:
    """Open polly.db with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


# ── Gmail Intake → escalations + tasks ─────────────────────────────────────────

def ingest_gmail_intake(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Read gmail-intake-latest.json and populate escalations (urgent) and tasks (today)."""
    if not GMAIL_INTAKE.exists():
        log.warning("Gmail intake file not found: %s", GMAIL_INTAKE)
        return 0

    try:
        data = json.loads(GMAIL_INTAKE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read Gmail intake: %s", exc)
        return 0

    threads = data.get("threads", [])
    ts = data.get("timestamp", datetime.now(timezone.utc).isoformat())
    count = 0

    for thread in threads:
        thread_id = thread.get("id", "")
        # Maxwell writes "class" not "classification"; support both field names
        classification = thread.get("class", thread.get("classification", "fyi"))
        subject = thread.get("subject", "(no subject)")
        sender = thread.get("from", "unknown")
        date_str = thread.get("date", "")

        if classification == "urgent":
            # Create an escalation for urgent emails
            esc_id = stable_id("gmail-urgent", thread_id)
            existing = conn.execute(
                "SELECT id FROM escalations WHERE id = ?", (esc_id,)
            ).fetchone()
            if not existing:
                if not dry_run:
                    conn.execute(
                        """INSERT INTO escalations
                           (id, from_agent, type, summary, source_object, status)
                           VALUES (?, 'maxwell', 'urgent_email', ?, ?, 'pending')""",
                        (esc_id, f"URGENT: {subject} (from {sender})", f"gmail:{thread_id}"),
                    )
                count += 1
                log.info("Escalation: %s — %s", esc_id[:8], subject)

        elif classification == "today":
            # Create a task for today-priority emails
            task_id = stable_id("gmail-today", thread_id)
            existing = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not existing:
                today = datetime.now().strftime("%Y-%m-%d")
                if not dry_run:
                    conn.execute(
                        """INSERT INTO tasks
                           (id, title, owner_agent, created_by, due, status, source, notes)
                           VALUES (?, ?, 'polly', 'maxwell', ?, 'open', ?, ?)""",
                        (
                            task_id,
                            f"Review: {subject}",
                            today,
                            f"gmail:{thread_id}",
                            f"From {sender} at {date_str}",
                        ),
                    )
                count += 1
                log.info("Task (today): %s — %s", task_id[:8], subject)

    if not dry_run:
        conn.commit()
    return count


# ── Otto Sweep → escalations ──────────────────────────────────────────────────

def ingest_otto_sweep(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Read Otto's sweep-log.yaml and populate escalations for URGENT/PRIORITY items."""
    if not OTTO_SWEEP.exists():
        log.warning("Otto sweep log not found: %s", OTTO_SWEEP)
        return 0

    content = OTTO_SWEEP.read_text().strip()
    if not content or content.startswith("#") and len(content.splitlines()) <= 1:
        log.info("Otto sweep log is empty, skipping")
        return 0

    # Simple YAML line parser — each line is "- YYYY-MM-DD HH:MM: CLASSIFICATION: subject"
    count = 0
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Try to parse structured sweep entries
        line_clean = line.lstrip("- ").strip()
        parts = line_clean.split(":", 2)
        if len(parts) < 3:
            continue

        # Expected: "timestamp: CLASSIFICATION: description"
        classification = parts[1].strip().upper()
        description = parts[2].strip()

        if classification in ("URGENT", "PRIORITY"):
            esc_id = stable_id("otto-sweep", line_clean[:80])
            existing = conn.execute(
                "SELECT id FROM escalations WHERE id = ?", (esc_id,)
            ).fetchone()
            if not existing:
                esc_type = "urgent_outlook" if classification == "URGENT" else "priority_outlook"
                if not dry_run:
                    conn.execute(
                        """INSERT INTO escalations
                           (id, from_agent, type, summary, source_object, status)
                           VALUES (?, 'otto', ?, ?, 'outlook', 'pending')""",
                        (esc_id, esc_type, description),
                    )
                count += 1
                log.info("Escalation (Otto): %s — %s", esc_id[:8], description[:60])

    if not dry_run:
        conn.commit()
    return count


# ── Cron Health → agent_health ─────────────────────────────────────────────────

def ingest_agent_health(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Read cron/jobs.json and populate agent_health with latest run status per agent."""
    if not CRON_JOBS.exists():
        log.warning("Cron jobs file not found: %s", CRON_JOBS)
        return 0

    try:
        cron_data = json.loads(CRON_JOBS.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read cron jobs: %s", exc)
        return 0

    # Aggregate per-agent: latest success, latest error, total items
    agent_stats: dict[str, dict] = {}
    for job in cron_data.get("jobs", []):
        agent_id = job.get("agentId", "unknown")
        state = job.get("state", {})
        status = state.get("lastRunStatus", "unknown")
        last_run_ms = state.get("lastRunAtMs")
        duration_ms = state.get("lastDurationMs", 0)
        error = state.get("lastError", "")

        if agent_id not in agent_stats:
            agent_stats[agent_id] = {
                "last_success_at": None,
                "last_status": "unknown",
                "last_error": None,
                "items_processed": 0,
            }

        stats = agent_stats[agent_id]
        stats["items_processed"] += 1

        if last_run_ms:
            run_dt = datetime.fromtimestamp(last_run_ms / 1000, tz=timezone.utc).isoformat()
            if status == "ok":
                if not stats["last_success_at"] or run_dt > stats["last_success_at"]:
                    stats["last_success_at"] = run_dt
                    stats["last_status"] = "ok"
            elif status == "error":
                stats["last_status"] = "error"
                stats["last_error"] = error or f"Duration: {duration_ms}ms"

    count = 0
    now = datetime.now(timezone.utc).isoformat()
    for agent_id, stats in agent_stats.items():
        if not dry_run:
            conn.execute(
                """INSERT INTO agent_health
                   (agent_id, last_success_at, last_status, last_error, items_processed, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(agent_id) DO UPDATE SET
                     last_success_at = excluded.last_success_at,
                     last_status = excluded.last_status,
                     last_error = excluded.last_error,
                     items_processed = excluded.items_processed,
                     updated_at = excluded.updated_at""",
                (
                    agent_id,
                    stats["last_success_at"],
                    stats["last_status"],
                    stats["last_error"],
                    stats["items_processed"],
                    now,
                ),
            )
        count += 1
        log.info("Agent health: %s → %s", agent_id, stats["last_status"])

    if not dry_run:
        conn.commit()
    return count


# ── Contacts (Rex) → waiting_on for stale contacts ────────────────────────────

def ingest_stale_contacts(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Flag contacts with no activity in 90+ days who had 'urgent' or 'today' interactions."""
    if not CONNECTIONS_DB.exists():
        log.info("Connections DB not found, skipping stale contact check")
        return 0

    try:
        rex_conn = sqlite3.connect(str(CONNECTIONS_DB), timeout=5)
        rex_conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        log.error("Failed to open connections.db: %s", exc)
        return 0

    count = 0
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        rows = rex_conn.execute(
            """SELECT c.id, c.name, c.last_contact, c.last_channel
               FROM connections c
               WHERE c.last_contact < ? AND c.status = 'active'
               ORDER BY c.last_contact ASC
               LIMIT 20""",
            (cutoff,),
        ).fetchall()

        for row in rows:
            wo_id = stable_id("rex-stale", row["id"])
            existing = conn.execute(
                "SELECT id FROM waiting_on WHERE id = ?", (wo_id,)
            ).fetchone()
            if not existing:
                if not dry_run:
                    conn.execute(
                        """INSERT INTO waiting_on
                           (id, description, from_whom, context, since, status, tracking_agent)
                           VALUES (?, ?, ?, ?, ?, 'open', 'rex')""",
                        (
                            wo_id,
                            f"No contact in 90+ days with {row['name']}",
                            row["name"],
                            f"Last channel: {row['last_channel'] or 'unknown'}",
                            row["last_contact"],
                        ),
                    )
                count += 1
                if count <= 5:
                    log.info("Stale contact: %s (last: %s)", row["name"], row["last_contact"])

    except sqlite3.Error as exc:
        log.error("Error querying connections.db: %s", exc)
    finally:
        rex_conn.close()

    if not dry_run:
        conn.commit()
    return count


# ── Calendar: Outlook (otto) + Google Calendar (maxwell) → events ──────────────

def _parse_outlook_calendar_yaml(content: str) -> list[dict]:
    """Parse the simple YAML written by otto_calendar_tick.sh into event dicts.

    The YAML is hand-rolled (not a full YAML parser dependency) so we parse it
    line-by-line. Format per event:
        - start: "Mon Apr 14 2026 09:00:00"
          end:   "Mon Apr 14 2026 10:00:00"
          subject: 'Committee Meeting'
          location: 'Room 101'    # optional
          organizer: 'foo@jhu.edu' # optional
          attendees: 5
    """
    events: list[dict] = []
    current: dict = {}
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- start:"):
            if current:
                events.append(current)
            current = {"source": "outlook", "start": stripped.split(":", 1)[1].strip().strip('"\''), "end": "", "title": "", "location": "", "organizer": "", "attendees": 0}
        elif stripped.startswith("end:") and current:
            current["end"] = stripped.split(":", 1)[1].strip().strip('"\'')
        elif stripped.startswith("subject:") and current:
            # subject may be quoted form: 'Meeting title' or unquoted
            val = stripped.split(":", 1)[1].strip()
            if val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            current["title"] = val
        elif stripped.startswith("location:") and current:
            val = stripped.split(":", 1)[1].strip().strip("'\"")
            current["location"] = val
        elif stripped.startswith("organizer:") and current:
            val = stripped.split(":", 1)[1].strip().strip("'\"")
            current["organizer"] = val
        elif stripped.startswith("attendees:") and current:
            try:
                current["attendees"] = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                pass
    if current:
        events.append(current)
    return [e for e in events if e.get("title")]


def _normalize_dt(dt_str: str) -> str | None:
    """Best-effort normalization of various datetime strings to ISO format."""
    if not dt_str:
        return None
    # Already ISO-ish
    if "T" in dt_str or dt_str.count("-") >= 2:
        return dt_str[:19]
    # Outlook AppleScript returns e.g. "Monday, April 14, 2026 at 9:00:00 AM"
    for fmt in (
        "%A, %B %d, %Y at %I:%M:%S %p",
        "%A, %B %d, %Y at %I:%M %p",
        "%a %b %d %Y %H:%M:%S",
        "%b %d, %Y %H:%M",
    ):
        try:
            return datetime.strptime(dt_str, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    return dt_str[:19]  # best effort truncation


def ingest_outlook_calendar(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Read otto's calendar-today.yaml and populate events table."""
    if not OTTO_CALENDAR.exists():
        log.info("Outlook calendar file not found, skipping: %s", OTTO_CALENDAR)
        return 0

    content = OTTO_CALENDAR.read_text()
    if "status: no_data" in content or "events: []" in content:
        log.info("Outlook calendar empty (no_data), skipping")
        return 0

    raw_events = _parse_outlook_calendar_yaml(content)
    if not raw_events:
        log.info("Outlook calendar YAML parsed to 0 events")
        return 0

    count = 0
    for evt in raw_events:
        evt_id = stable_id("outlook-cal", evt.get("title", ""), evt.get("start", ""))
        existing = conn.execute("SELECT id FROM events WHERE id = ?", (evt_id,)).fetchone()
        if not existing:
            start_iso = _normalize_dt(evt.get("start", ""))
            if not start_iso:
                continue
            if not dry_run:
                conn.execute(
                    """INSERT INTO events
                       (id, type, title, datetime, participants, prep_required,
                        prep_notes, status)
                       VALUES (?, 'meeting', ?, ?, ?, 0, ?, 'upcoming')""",
                    (
                        evt_id,
                        evt["title"],
                        start_iso,
                        str(evt.get("attendees", 0)),
                        evt.get("location", "") or evt.get("organizer", ""),
                    ),
                )
            count += 1
            log.info("Outlook event: %s @ %s", evt["title"][:50], start_iso)

    if not dry_run:
        conn.commit()
    return count


def ingest_google_calendar(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Read gcal-today.json and populate events table."""
    if not GCAL_TODAY.exists():
        log.info("Google Calendar file not found, skipping: %s", GCAL_TODAY)
        return 0

    try:
        data = json.loads(GCAL_TODAY.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read gcal-today.json: %s", exc)
        return 0

    if data.get("status") != "ok":
        log.info("Google Calendar status=%s (%s), skipping", data.get("status"), data.get("error", "")[:80])
        return 0

    raw_events = data.get("events", [])
    count = 0
    for evt in raw_events:
        if evt.get("status") == "cancelled":
            continue
        evt_id = stable_id("gcal", evt.get("id", ""), evt.get("start", ""))
        existing = conn.execute("SELECT id FROM events WHERE id = ?", (evt_id,)).fetchone()
        if not existing:
            start_iso = _normalize_dt(evt.get("start", ""))
            if not start_iso:
                continue
            prep = 1 if evt.get("attendees", 0) > 2 else 0
            if not dry_run:
                conn.execute(
                    """INSERT INTO events
                       (id, type, title, datetime, participants, prep_required,
                        prep_notes, status)
                       VALUES (?, 'meeting', ?, ?, ?, ?, ?, 'upcoming')""",
                    (
                        evt_id,
                        evt.get("summary", "(no title)"),
                        start_iso,
                        str(evt.get("attendees", 0)),
                        prep,
                        evt.get("location", ""),
                    ),
                )
            count += 1
            log.info("GCal event: %s @ %s", evt.get("summary", "")[:50], start_iso)

    # Expire stale gcal events (today's data overrides yesterday's)
    if not dry_run and count > 0:
        today = datetime.now().strftime("%Y-%m-%d")
        conn.execute(
            """DELETE FROM events
               WHERE id LIKE 'gcal%' AND status = 'upcoming'
               AND datetime < ?""",
            (today,),
        )
        conn.commit()

    return count


# ── Cleanup: expire old resolved items ─────────────────────────────────────────

def cleanup_resolved(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Remove escalations and tasks that have been resolved for 7+ days."""
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    count = 0

    if not dry_run:
        cur = conn.execute(
            "DELETE FROM escalations WHERE status IN ('resolved', 'dismissed') AND updated_at < ?",
            (cutoff,),
        )
        count += cur.rowcount

        cur = conn.execute(
            "DELETE FROM tasks WHERE status = 'done' AND updated_at < ?",
            (cutoff,),
        )
        count += cur.rowcount

        cur = conn.execute(
            "DELETE FROM waiting_on WHERE status = 'resolved' AND updated_at < ?",
            (cutoff,),
        )
        count += cur.rowcount

        conn.commit()

    if count:
        log.info("Cleaned up %d resolved items older than 7 days", count)
    return count


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Polly ingestion pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be ingested without writing")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
    )

    if not POLLY_DB.exists():
        log.error("polly.db not found at %s", POLLY_DB)
        sys.exit(1)

    conn = ensure_db(POLLY_DB)
    mode = "[DRY RUN] " if args.dry_run else ""

    log.info("%sStarting ingestion run", mode)

    gmail_count = ingest_gmail_intake(conn, args.dry_run)
    otto_count = ingest_otto_sweep(conn, args.dry_run)
    outlook_cal_count = ingest_outlook_calendar(conn, args.dry_run)
    gcal_count = ingest_google_calendar(conn, args.dry_run)
    health_count = ingest_agent_health(conn, args.dry_run)
    stale_count = ingest_stale_contacts(conn, args.dry_run)
    cleanup_count = cleanup_resolved(conn, args.dry_run)

    conn.close()

    total = gmail_count + otto_count + outlook_cal_count + gcal_count + health_count + stale_count
    log.info(
        "%sIngestion complete: gmail=%d, otto=%d, outlook_cal=%d, gcal=%d, health=%d, stale_contacts=%d, cleaned=%d",
        mode,
        gmail_count,
        otto_count,
        outlook_cal_count,
        gcal_count,
        health_count,
        stale_count,
        cleanup_count,
    )
    print(json.dumps({
        "status": "ok",
        "dry_run": args.dry_run,
        "gmail_items": gmail_count,
        "otto_items": otto_count,
        "outlook_calendar_events": outlook_cal_count,
        "gcal_events": gcal_count,
        "agent_health_items": health_count,
        "stale_contacts": stale_count,
        "cleaned_up": cleanup_count,
        "total_new": total,
    }))


if __name__ == "__main__":
    main()
