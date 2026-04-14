#!/usr/bin/env python3
"""rex_query.py — deterministic relationship lookup for Polly.

Purpose: Polly execs this script with a name/keyword argument to get a real
relationship brief from Rex's databases. This bypasses the qwen2.5:7b model's
tendency to hallucinate ACP responses instead of calling exec.

Usage:
    python3 rex_query.py <name>
    python3 rex_query.py "lipartito"
    python3 rex_query.py "Katherine Howe"
    python3 rex_query.py "howe"

Output: Plain-text relationship brief suitable for Polly to relay directly to
Louis via Telegram. If nothing is found, says so explicitly. Never invents data.

Design: Follows the 4-step Rex query pattern from OPENCLAW_HOW_IT_SHOULD_WORK.md §9.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw"))
WORKSPACES = OPENCLAW_HOME / "workspaces"

CONNECTIONS_DB = WORKSPACES / "rex-workspace" / "connections.db"
POLLY_DB = WORKSPACES / "polly-workspace" / "polly.db"


def _open(path: Path) -> sqlite3.Connection | None:
    """Open a SQLite database in read-only mode. Returns None if file missing."""
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def search_contacts(conn: sqlite3.Connection, term: str) -> list[sqlite3.Row]:
    """Step 1: find matching contacts in connections.db."""
    like = f"%{term.lower()}%"
    return conn.execute(
        """
        SELECT id, name, org, role, last_contact, notes
        FROM connections
        WHERE name_lower LIKE ? OR notes LIKE ?
        ORDER BY last_contact DESC
        LIMIT 5
        """,
        (like, like),
    ).fetchall()


def email_threads(conn: sqlite3.Connection, name: str, emails: list[str]) -> list[sqlite3.Row]:
    """Step 2: recent email threads from polly.db email_threads table."""
    # Build OR conditions for name and each email domain
    conditions = [f"from_name LIKE '%{name}%'"]
    for email in emails:
        conditions.append(f"from_email LIKE '%{email}%'")
    where = " OR ".join(conditions)
    return conn.execute(
        f"""
        SELECT subject, from_name, from_email, received_at, reply_needed, snippet
        FROM email_threads
        WHERE {where}
        ORDER BY received_at DESC
        LIMIT 10
        """
    ).fetchall()


def open_reply_threads(conn: sqlite3.Connection, name: str, emails: list[str]) -> list[sqlite3.Row]:
    """Step 3: threads where a reply is owed."""
    conditions = [f"from_name LIKE '%{name}%'"]
    for email in emails:
        conditions.append(f"from_email LIKE '%{email}%'")
    where = " OR ".join(conditions)
    return conn.execute(
        f"""
        SELECT subject, received_at, due_date
        FROM email_threads
        WHERE reply_needed=1 AND ({where})
        ORDER BY received_at DESC
        """
    ).fetchall()


def commitments_and_waiting(conn: sqlite3.Connection, name: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Step 4: open commitments and waiting_on items."""
    like = f"%{name}%"
    commits = conn.execute(
        """
        SELECT description, due, status
        FROM commitments
        WHERE to_whom LIKE ? AND status != 'done'
        ORDER BY due ASC
        LIMIT 5
        """,
        (like,),
    ).fetchall()
    waiting = conn.execute(
        """
        SELECT description, since, due
        FROM waiting_on
        WHERE from_whom LIKE ?
        ORDER BY since DESC
        LIMIT 5
        """,
        (like,),
    ).fetchall()
    return commits, waiting


def contact_signals(conn: sqlite3.Connection, emails: list[str]) -> sqlite3.Row | None:
    """Get communication cadence summary from contact_signals."""
    for email in emails:
        row = conn.execute(
            """
            SELECT from_name, total_threads, direct_threads,
                   open_reply_threads, last_email_at
            FROM contact_signals
            WHERE from_email LIKE ?
            ORDER BY last_email_at DESC
            LIMIT 1
            """,
            (f"%{email}%",),
        ).fetchone()
        if row:
            return row
    return None


def extract_emails_from_notes(notes: str | None) -> list[str]:
    """Pull email address out of the notes field (Rex format: 'Email: x@y.z')."""
    if not notes:
        return []
    emails = []
    for word in notes.split():
        word = word.rstrip(".,;")
        if "@" in word and "." in word:
            emails.append(word.lower())
    return emails


def format_brief(term: str, contacts: list, threads: list, replies: list,
                 commits: list, waiting: list, signals) -> str:
    """Assemble results into a clean Telegram-ready brief."""
    lines: list[str] = []

    # ── Contacts found ────────────────────────────────────────────────────────
    if not contacts:
        return f'Rex found no contacts matching "{term}" in connections.db.'

    lines.append(f"📇 Rex lookup: *{term}*")
    lines.append("")

    for c in contacts:
        name = c["name"] or "?"
        org = c["org"] or ""
        role = c["role"] or ""
        last = c["last_contact"] or "unknown"
        notes = c["notes"] or ""

        header = name
        if org and org.lower() not in name.lower():
            header += f" ({org})"
        if role:
            header += f" — {role}"
        lines.append(f"**{header}**")
        lines.append(f"Last contact: {last}")

        # Extract email from notes for display
        emails_in_notes = extract_emails_from_notes(notes)
        if emails_in_notes:
            lines.append(f"Email: {emails_in_notes[0]}")

        # Notes (trim auto-import boilerplate)
        clean_notes = notes
        for prefix in ["Auto-imported from staged Gmail bootstrap (30d). ",
                        "Auto-synced from Gmail (14d window). ",
                        "Auto-synced from Gmail (365d window). "]:
            clean_notes = clean_notes.replace(prefix, "")
        if clean_notes and len(clean_notes) < 200:
            lines.append(f"Notes: {clean_notes.strip()}")
        lines.append("")

    # ── Email threads ─────────────────────────────────────────────────────────
    if threads:
        lines.append("📬 Recent email threads:")
        for t in threads[:5]:
            subj = t["subject"] or "(no subject)"
            date = (t["received_at"] or "")[:10]
            reply_flag = " ⚠️ reply needed" if t["reply_needed"] else ""
            lines.append(f"  • {date} — {subj}{reply_flag}")
        lines.append("")
    else:
        lines.append("📬 No email threads found in polly.db.")
        lines.append("")

    # ── Replies owed ──────────────────────────────────────────────────────────
    if replies:
        lines.append("⚠️ Replies owed:")
        for r in replies:
            subj = r["subject"] or "(no subject)"
            date = (r["received_at"] or "")[:10]
            due = f" (due {r['due_date']})" if r["due_date"] else ""
            lines.append(f"  • {subj} — received {date}{due}")
        lines.append("")

    # ── Commitments ───────────────────────────────────────────────────────────
    if commits:
        lines.append("📋 Open commitments to them:")
        for c in commits:
            desc = c["description"] or "?"
            due = f" (due {c['due']})" if c["due"] else ""
            lines.append(f"  • {desc}{due}")
        lines.append("")

    # ── Waiting on ────────────────────────────────────────────────────────────
    if waiting:
        lines.append("⏳ Waiting on them:")
        for w in waiting:
            desc = w["description"] or "?"
            since = f" since {w['since']}" if w["since"] else ""
            lines.append(f"  • {desc}{since}")
        lines.append("")

    # ── Communication cadence ────────────────────────────────────────────────
    if signals:
        lines.append(
            f"📊 Signal: {signals['total_threads']} total threads, "
            f"{signals['direct_threads']} direct, "
            f"{signals['open_reply_threads']} open. "
            f"Last: {(signals['last_email_at'] or '')[:10]}"
        )

    return "\n".join(lines).strip()


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: rex_query.py <name>", file=sys.stderr)
        sys.exit(1)

    term = " ".join(sys.argv[1:]).strip()
    if not term:
        print("Error: name argument is empty", file=sys.stderr)
        sys.exit(1)

    # Open databases (read-only; graceful if missing)
    rex_conn = _open(CONNECTIONS_DB)
    polly_conn = _open(POLLY_DB)

    if rex_conn is None:
        print(f"Error: connections.db not found at {CONNECTIONS_DB}", file=sys.stderr)
        sys.exit(1)

    # Step 1 — find contacts
    contacts = search_contacts(rex_conn, term)

    # Collect emails from all matched contacts for polly.db joins
    all_emails: list[str] = []
    all_names: list[str] = []
    for c in contacts:
        all_names.append(c["name"] or "")
        all_emails.extend(extract_emails_from_notes(c["notes"]))

    # Step 2–4 — query polly.db (may be empty if maxwell_ingest hasn't run)
    threads: list = []
    replies: list = []
    commits: list = []
    waiting: list = []
    signals = None

    if polly_conn is not None:
        # Use the search term for name-based queries (broader than exact match)
        threads = email_threads(polly_conn, term, all_emails)
        replies = open_reply_threads(polly_conn, term, all_emails)
        commits, waiting = commitments_and_waiting(polly_conn, term)
        signals = contact_signals(polly_conn, all_emails)

    print(format_brief(term, contacts, threads, replies, commits, waiting, signals))


if __name__ == "__main__":
    main()
