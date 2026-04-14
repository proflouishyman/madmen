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
    """Open polly.db with WAL mode and create any missing tables including the
    email memory layer (email_threads, thread_items, contact_signals)."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    # ── Email memory tables ───────────────────────────────────────────────────
    # email_threads: one row per email thread, enriched from Maxwell/Otto intake.
    # Written by maxwell_ingest.py; read by polly_ingest.py and Rex.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_threads (
            id              TEXT PRIMARY KEY,
            thread_id       TEXT NOT NULL,
            source          TEXT NOT NULL,
            from_email      TEXT NOT NULL,
            from_name       TEXT,
            subject         TEXT NOT NULL,
            snippet         TEXT,
            received_at     DATETIME NOT NULL,
            last_reply_at   DATETIME,
            is_direct       INTEGER DEFAULT 0,
            reply_needed    INTEGER DEFAULT 0,
            reply_owed_by   TEXT,
            due_date        DATE,
            topic_tags      TEXT,
            classification  TEXT,
            connection_id   TEXT,
            processed_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Index for fast per-contact and recency queries by Rex
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_email_threads_from
        ON email_threads(from_email, received_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_email_threads_reply
        ON email_threads(reply_needed, received_at DESC)
    """)

    # thread_items: extracted facts (commitments, deadlines, questions) from bodies.
    # Promoted items flow into the commitments / waiting_on tables.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thread_items (
            id              TEXT PRIMARY KEY,
            thread_id       TEXT NOT NULL,
            item_type       TEXT NOT NULL,
            text            TEXT NOT NULL,
            owner           TEXT,
            due_date        DATE,
            promoted        INTEGER DEFAULT 0,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # contact_signals: per-contact aggregated email intelligence.
    # Keyed by from_email; joined with Rex connections by email for relationship queries.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_signals (
            from_email          TEXT PRIMARY KEY,
            from_name           TEXT,
            connection_id       TEXT,
            last_email_at       DATETIME,
            first_email_at      DATETIME,
            total_threads       INTEGER DEFAULT 0,
            direct_threads      INTEGER DEFAULT 0,
            open_reply_threads  INTEGER DEFAULT 0,
            topics              TEXT,
            updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
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
    """Read Otto's sweep-log.yaml and populate escalations for URGENT/PRIORITY items.

    Supports the structured format written by otto_outlook_sweep.sh:
        messages:
        - subject: 'Re: Urgent thing'
          sender: '...'
          received: '...'
          class: URGENT
    """
    if not OTTO_SWEEP.exists():
        log.warning("Otto sweep log not found: %s", OTTO_SWEEP)
        return 0

    content = OTTO_SWEEP.read_text().strip()
    non_comment = [l for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not non_comment:
        log.info("Otto sweep log is empty, skipping")
        return 0

    count = 0

    # ── Structured format (otto_outlook_sweep.sh) ────────────────────────────
    # Parse message blocks: look for "- subject:" entries followed by "  class: X"
    current: dict = {}
    in_messages = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("#") or not line:
            continue
        if line == "messages:":
            in_messages = True
            continue
        if not in_messages:
            continue
        # New message entry
        if line.startswith("- subject:"):
            if current:
                count += _maybe_insert_otto_escalation(conn, current, dry_run)
            current = {"subject": _yaml_val(line)}
        elif line.startswith("sender:") and current:
            current["sender"] = _yaml_val(line)
        elif line.startswith("received:") and current:
            current["received"] = _yaml_val(line)
        elif line.startswith("class:") and current:
            current["class"] = _yaml_val(line).upper()
        elif line.startswith("sweep_run:") or line.startswith("- timestamp:"):
            # New sweep_run block — flush current message and reset
            if current:
                count += _maybe_insert_otto_escalation(conn, current, dry_run)
                current = {}
            in_messages = False

    # Flush last message
    if current:
        count += _maybe_insert_otto_escalation(conn, current, dry_run)

    if not dry_run:
        conn.commit()
    return count


def _yaml_val(line: str) -> str:
    """Extract value from a 'key: value' YAML line, stripping quotes."""
    parts = line.split(":", 1)
    val = parts[1].strip() if len(parts) > 1 else ""
    return val.strip("'\"")


def _maybe_insert_otto_escalation(
    conn: sqlite3.Connection, msg: dict, dry_run: bool
) -> int:
    """Insert an escalation for URGENT/PRIORITY Outlook messages. Returns 1 if inserted."""
    cls = msg.get("class", "ROUTINE")
    if cls not in ("URGENT", "PRIORITY"):
        return 0
    subject = msg.get("subject", "(no subject)")
    sender  = msg.get("sender", "unknown")
    esc_id  = stable_id("otto-sweep", f"{subject}:{sender}")
    existing = conn.execute("SELECT id FROM escalations WHERE id = ?", (esc_id,)).fetchone()
    if existing:
        return 0
    esc_type = "urgent_outlook" if cls == "URGENT" else "priority_outlook"
    # Strip redundant leading classification prefix if subject already starts with it
    display_subject = subject
    for prefix in ("URGENT:", "PRIORITY:", "URGENT -", "PRIORITY -"):
        if subject.upper().startswith(prefix):
            display_subject = subject[len(prefix):].strip()
            break
    summary  = f"{cls}: {display_subject} (from {sender})"
    if not dry_run:
        conn.execute(
            """INSERT INTO escalations
               (id, from_agent, type, summary, source_object, status)
               VALUES (?, 'otto', ?, ?, 'outlook', 'pending')""",
            (esc_id, esc_type, summary),
        )
    log.info("Escalation (Otto %s): %s", cls, subject[:60])
    return 1


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
            # Check whether this agent already has a health row (upsert vs new insert)
            existing = conn.execute(
                "SELECT 1 FROM agent_health WHERE agent_id = ?", (agent_id,)
            ).fetchone()
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
            # Only count as "new" on first insert; updates are routine health refreshes
            if not existing:
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


# ── Live Status Cache → SOUL.md ───────────────────────────────────────────────

SOUL_MD = WORKSPACES / "polly-workspace" / "SOUL.md"
# Sandbox path — OpenClaw reads this file at agent runtime, not the workspace copy.
# polly_ingest must keep BOTH in sync so the Live Status block is never stale in production.
_OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw"))
SOUL_MD_SANDBOX = _OPENCLAW_HOME / "sandboxes" / "agent-polly-16c13b58" / "SOUL.md"
_LIVE_STATUS_START = "<!-- LIVE_STATUS_START -->"
_LIVE_STATUS_END   = "<!-- LIVE_STATUS_END -->"


def write_sitrep_cache(conn: sqlite3.Connection) -> None:
    """Query polly.db and write current status into SOUL.md between marker comments.

    This avoids the need for Polly to call exec during conversational turns —
    the data is embedded directly in the system context at bootstrap time.
    """
    if not SOUL_MD.exists():
        log.warning("SOUL.md not found at %s, skipping sitrep cache write", SOUL_MD)
        return

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Calendar: next 48 hours ───────────────────────────────────────────────
    events = conn.execute(
        """SELECT datetime, title, type FROM events
           WHERE date(datetime) >= date('now')
             AND date(datetime) <= date('now', '+1 day')
           ORDER BY datetime"""
    ).fetchall()

    if events:
        cal_lines = []
        for row in events:
            dt = row[0][:16] if row[0] else "?"
            title = row[1] or "(untitled)"
            etype = row[2] or ""
            cal_lines.append(f"  - {dt}  {title}" + (f" [{etype}]" if etype else ""))
        cal_block = "\n".join(cal_lines)
    else:
        cal_block = "  (no events in next 48h)"

    # ── Agent health ──────────────────────────────────────────────────────────
    health_rows = conn.execute(
        """SELECT agent_id, last_status, last_error, updated_at
           FROM agent_health ORDER BY updated_at DESC"""
    ).fetchall()

    if health_rows:
        health_lines = []
        for row in health_rows:
            agent = row[0]
            status = row[1] or "unknown"
            err = f" — {row[2]}" if row[2] and status == "error" else ""
            updated = (row[3] or "")[:16]
            health_lines.append(f"  - {agent}: {status}{err} (updated {updated})")
        health_block = "\n".join(health_lines)
    else:
        health_block = "  (no health data)"

    # ── Escalations ───────────────────────────────────────────────────────────
    esc_rows = conn.execute(
        """SELECT type, summary, created_at FROM escalations
           WHERE status = 'pending' ORDER BY created_at DESC LIMIT 10"""
    ).fetchall()

    if esc_rows:
        esc_lines = [f"  - {r[1]}" for r in esc_rows]
        esc_block = "\n".join(esc_lines)
    else:
        esc_block = "  (none pending)"

    # ── Open tasks ────────────────────────────────────────────────────────────
    task_rows = conn.execute(
        """SELECT title, owner_agent, due, status FROM tasks
           WHERE status = 'open' ORDER BY due ASC LIMIT 10"""
    ).fetchall()

    if task_rows:
        task_lines = [f"  - {r[0]} (due {r[2] or 'TBD'}, owner: {r[1]})" for r in task_rows]
        task_block = "\n".join(task_lines)
    else:
        task_block = "  (no open tasks)"

    # ── Assemble block ────────────────────────────────────────────────────────
    block = f"""{_LIVE_STATUS_START}
*Auto-updated by polly_ingest.py — last refresh: {now_utc}*

### 📅 Calendar (next 48h)
{cal_block}

### 🔧 Agent Health
{health_block}

### 🚨 Escalations (pending)
{esc_block}

### 📋 Open Tasks
{task_block}
{_LIVE_STATUS_END}"""

    # ── Write into SOUL.md between markers ───────────────────────────────────
    # Write to BOTH workspace and sandbox copies. OpenClaw reads the sandbox at
    # agent runtime; the workspace is the source of truth for manual edits.
    # Keeping them in sync here means polly_ingest is the single writer for the
    # Live Status block — no manual sync required after each ingest run.
    for soul_path in [SOUL_MD, SOUL_MD_SANDBOX]:
        if not soul_path.exists():
            log.warning("SOUL.md not found at %s, skipping", soul_path)
            continue
        soul = soul_path.read_text()
        if _LIVE_STATUS_START in soul and _LIVE_STATUS_END in soul:
            before = soul[:soul.index(_LIVE_STATUS_START)]
            after  = soul[soul.index(_LIVE_STATUS_END) + len(_LIVE_STATUS_END):]
            soul_path.write_text(before + block + after)
        else:
            # Markers missing — append block at end
            soul_path.write_text(soul.rstrip() + "\n\n" + block + "\n")

    log.info("Sitrep cache written to workspace + sandbox SOUL.md (%s)", now_utc)


# ── Morning digest pre-assembly ────────────────────────────────────────────────

DIGEST_DRAFT = WORKSPACES / "polly-workspace" / "state" / "morning-digest-draft.txt"

def write_morning_digest(conn: sqlite3.Connection) -> None:
    """Pre-assemble the morning digest text and write it to a file.

    The 7am digest cron just reads this file and sends it — no live SQL queries,
    no multi-step exec chain, no 26B model doing 7 tool calls under time pressure.
    Called at the end of every 20-min ingestion run; only the 7am run matters.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today   = datetime.now().strftime("%A, %B %-d")
    lines   = [f"🌅 *Morning digest — {today}*", ""]

    # ── Calendar ─────────────────────────────────────────────────────────────
    events = conn.execute(
        """SELECT datetime, title, prep_required FROM events
           WHERE status='upcoming'
             AND date(datetime) >= date('now')
             AND date(datetime) < date('now','+2 days')
           ORDER BY datetime ASC LIMIT 15"""
    ).fetchall()
    if events:
        lines.append("📅 *Today's calendar*")
        for row in events:
            dt    = (row[0] or "?")[:16]
            title = row[1] or "(untitled)"
            prep  = " ⚑ prep needed" if row[2] else ""
            lines.append(f"  {dt}  {title}{prep}")
        lines.append("")

    # ── Urgent escalations ────────────────────────────────────────────────────
    escs = conn.execute(
        """SELECT summary FROM escalations
           WHERE status='pending' ORDER BY created_at DESC LIMIT 8"""
    ).fetchall()
    if escs:
        lines.append("🚨 *Urgent*")
        for r in escs:
            lines.append(f"  • {r[0]}")
        lines.append("")

    # ── Tasks due today ───────────────────────────────────────────────────────
    tasks = conn.execute(
        """SELECT title, owner_agent FROM tasks
           WHERE status='open' AND due <= date('now') LIMIT 8"""
    ).fetchall()
    if tasks:
        lines.append("📋 *Due today*")
        for r in tasks:
            lines.append(f"  • {r[0]} ({r[1]})")
        lines.append("")

    # ── Email summary from email_threads ─────────────────────────────────────
    try:
        email_counts = conn.execute(
            """SELECT classification, COUNT(*) as n
               FROM email_threads
               WHERE date(received_at) >= date('now','-1 day')
               GROUP BY classification ORDER BY n DESC"""
        ).fetchall()
        reply_due = conn.execute(
            """SELECT COUNT(*) FROM email_threads
               WHERE reply_needed=1
                 AND date(received_at) >= date('now','-7 days')"""
        ).fetchone()[0]
        if email_counts:
            counts_str = "  " + "  ".join(
                f"{r[0]}: {r[1]}" for r in email_counts
                if r[0] not in ("spam_or_noise", "noise")
            )
            lines.append("✉️ *Email (last 24h)*")
            if counts_str.strip():
                lines.append(counts_str)
            if reply_due:
                lines.append(f"  ↩ {reply_due} thread(s) need a reply")
            lines.append("")
    except Exception:
        pass  # email_threads may not exist yet on first run

    # ── Pending approvals ─────────────────────────────────────────────────────
    drafts = conn.execute(
        """SELECT subject, created_by FROM drafts
           WHERE status='pending_approval' LIMIT 5"""
    ).fetchall()
    if drafts:
        lines.append("✍️ *Pending approvals*")
        for r in drafts:
            lines.append(f"  • {r[0]} (from {r[1]})")
        lines.append("")

    # ── System health ─────────────────────────────────────────────────────────
    errors = conn.execute(
        """SELECT agent_id, last_error FROM agent_health
           WHERE last_status='error' ORDER BY updated_at DESC LIMIT 5"""
    ).fetchall()
    if errors:
        lines.append("🔧 *System health*")
        for r in errors:
            err_note = f" — {r[1]}" if r[1] else ""
            lines.append(f"  ⚠ {r[0]}{err_note}")
        lines.append("")

    if len(lines) <= 2:
        lines.append("  All clear — nothing urgent today.")

    lines.append(f"_Updated {now_utc}_")

    DIGEST_DRAFT.parent.mkdir(parents=True, exist_ok=True)
    DIGEST_DRAFT.write_text("\n".join(lines) + "\n")
    log.info("Morning digest draft written (%d lines)", len(lines))


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

    # Write current db state into SOUL.md so Polly has it in bootstrap context
    if not args.dry_run:
        write_sitrep_cache(conn)
        # Pre-assemble morning digest so the 7am cron only needs to read + send
        write_morning_digest(conn)

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
