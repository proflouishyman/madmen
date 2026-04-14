#!/usr/bin/env python3
"""Maxwell email memory ingestion pipeline.

Reads gmail-intake-latest.json (Gmail) and otto sweep-log.yaml (Outlook),
enriches each thread with direct/list classification and reply intelligence,
fetches body snippets for direct emails via gog, and upserts results into
polly.db (email_threads, thread_items, contact_signals tables).

Rex uses these tables to answer relationship queries from Polly.
Polly uses them to build the morning digest without live tool calls.

Run: python3 maxwell_ingest.py [--dry-run] [--verbose] [--skip-body-fetch]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("maxwell_ingest")

# ── Paths ──────────────────────────────────────────────────────────────────────
OPENCLAW_HOME = Path.home() / ".openclaw"
WORKSPACES    = OPENCLAW_HOME / "workspaces"
POLLY_DB      = WORKSPACES / "polly-workspace" / "polly.db"
REX_DB        = WORKSPACES / "rex-workspace" / "connections.db"
GMAIL_INTAKE  = WORKSPACES / "maxwell-workspace" / "memory" / "gmail-intake-latest.json"
OTTO_SWEEP    = WORKSPACES / "otto-workspace" / "state" / "sweep-log.yaml"
GMAIL_ACCOUNT = "lhyman@gmail.com"
BODY_FETCH_TIMEOUT = 12   # seconds per gog call
MAX_DIRECT_BODY_FETCH = 8  # max direct threads to fetch body for per run

# ── Known list/newsletter sender patterns ─────────────────────────────────────
_LIST_LOCAL_PREFIXES = {
    "no-reply", "noreply", "donotreply", "do-not-reply",
    "notifications", "notification", "newsletter", "newsletters",
    "info", "updates", "update", "mailer", "bounce",
    "support", "hello", "team", "marketing", "news",
    "reply", "alerts", "alert", "subscribe", "unsubscribe",
    "digest", "weekly", "daily", "monthly", "announcements",
    "postmaster", "webmaster", "admin", "contact",
    "automated", "automailer", "list", "lists",
    "offers", "deals", "promo", "promotions", "coupons",
    "billing", "invoice", "receipt", "order", "shipment",
    "tracking", "delivery", "shipping",
    "follow", "invitations", "invitation",
    "accounts", "account", "security",
}
_LIST_DOMAIN_FRAGMENTS = {
    "substack.com", "mailchimp.com", "sendgrid.net", "mailgun.org",
    "constantcontact.com", "campaignmonitor.com", "klaviyo.com",
    "em.example.com", "email.", "mail.", "newsletter.",
    "mailing.", "blast.", "campaign.",
}
# Gmail labels that indicate a direct human email
_DIRECT_GMAIL_LABELS = {"CATEGORY_PERSONAL"}
# Gmail labels that confirm non-direct
_NONDIRECT_GMAIL_LABELS = {
    "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS",
    "CATEGORY_UPDATES",
}
# Gmail labels that are definitively commercial/marketing — never index these
_COMMERCIAL_GMAIL_LABELS = {"CATEGORY_PROMOTIONS"}

# Subject/body patterns that signal commercial email (opt-out / unsubscribe / tracking)
_COMMERCIAL_SUBJECT_PATTERNS = [
    re.compile(r'\bunsubscribe\b', re.I),
    re.compile(r'\bopt.?out\b', re.I),
    re.compile(r'\bopt.?in\b', re.I),
    re.compile(r'\bsale\b.{0,30}\b(ends|today|now|off)\b', re.I),
    re.compile(r'\b\d+%\s*off\b', re.I),
    re.compile(r'\b(promo|coupon|deal|offer|discount|savings|clearance)\b', re.I),
    re.compile(r'\b(free shipping|limited time|act now|exclusive offer)\b', re.I),
]
# Commercial sender domain fragments (retail, marketing platforms)
_COMMERCIAL_DOMAIN_FRAGMENTS = {
    "mailchimp.com", "sendgrid.net", "klaviyo.com", "constantcontact.com",
    "campaignmonitor.com", "exacttarget.com", "sailthru.com", "braze.com",
    "responsys.net", "marketo.com", "hubspot.com", "pardot.com",
    "mg.substack.com",  # Substack marketing (not newsletter)
}

# ── Deadline / reply keywords ──────────────────────────────────────────────────
_DEADLINE_PATTERNS = [
    re.compile(r'\bby\s+(monday|tuesday|wednesday|thursday|friday|'
               r'saturday|sunday|tomorrow|tonight|eod|eow|end of day|'
               r'end of week|end of month)\b', re.I),
    re.compile(r'\b(due|deadline)\s+(on|by|before)?\s*\w+', re.I),
    re.compile(r'\bby\s+\w+\s+\d{1,2}(st|nd|rd|th)?\b', re.I),
    re.compile(r'\bresponse (needed|required|requested)\b', re.I),
]
_COMMITMENT_PATTERNS = [
    re.compile(r"\bi('ll|'m going to|will)\s+\w+", re.I),
    re.compile(r'\bplease (review|confirm|approve|sign|send|respond)\b', re.I),
    re.compile(r'\bcan you\b', re.I),
    re.compile(r'\bwaiting (to hear|for your|on you)\b', re.I),
    re.compile(r'\bfollowing up\b', re.I),
    re.compile(r'\blet me know\b', re.I),
]
_QUESTION_PATTERN = re.compile(r'\?')


# ── Utilities ──────────────────────────────────────────────────────────────────

def stable_id(*parts: str) -> str:
    """Deterministic SHA-256-based ID from parts."""
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:32]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_from(from_field: str) -> tuple[str, str]:
    """Return (name, email) from a 'Name <email>' or bare email string."""
    m = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', from_field.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    bare = re.search(r'[\w.+-]+@[\w.-]+', from_field)
    if bare:
        email = bare.group(0).lower()
        return email.split("@")[0], email
    return from_field.strip(), from_field.strip().lower()


def is_direct_sender(from_email: str, from_name: str,
                     labels: list[str] | None = None) -> bool:
    """Heuristic: return True if this email is likely person-to-person.

    Gmail labels (CATEGORY_PERSONAL) are the most reliable signal.
    Falls back to FROM address pattern analysis.
    """
    if labels:
        if any(l in _DIRECT_GMAIL_LABELS for l in labels):
            return True
        if any(l in _NONDIRECT_GMAIL_LABELS for l in labels):
            return False

    local = from_email.split("@")[0].lower().replace(".", "").replace("-", "").replace("_", "")
    domain = from_email.split("@")[1].lower() if "@" in from_email else ""

    # Non-direct: local part matches known list prefixes
    if any(pfx in local for pfx in _LIST_LOCAL_PREFIXES):
        return False
    # Non-direct: domain fragment matches known bulk mail providers
    if any(frag in domain for frag in _LIST_DOMAIN_FRAGMENTS):
        return False
    # Non-direct: subject has typical newsletter markers (passed via from_name abuse)
    # Domain that looks like a business marketing domain (email.company.com)
    parts = domain.split(".")
    if len(parts) > 2 and parts[0] in {"email", "mail", "newsletter", "marketing", "news"}:
        return False
    # Treat as direct
    return True


def is_commercial(from_email: str, labels: list[str] | None,
                  subject: str = "") -> bool:
    """Return True if this email is definitively commercial/marketing.

    Commercial emails should NOT be indexed into email_threads or contact_signals.
    They pollute relationship intelligence with retailer and bulk-sender noise.

    Detection signals (any one is sufficient):
      1. Gmail CATEGORY_PROMOTIONS label — the most reliable signal
      2. Known marketing platform domain (e.g. klaviyo.com, sendgrid.net)
      3. Subject line contains opt-out / unsubscribe / sale / discount language
    """
    # 1. Gmail's own promotions category
    if labels and any(l in _COMMERCIAL_GMAIL_LABELS for l in labels):
        return True
    # 2. Marketing platform domain
    domain = from_email.split("@")[1].lower() if "@" in from_email else ""
    if any(frag in domain for frag in _COMMERCIAL_DOMAIN_FRAGMENTS):
        return True
    # 3. Subject-line commercial signals
    if subject and any(p.search(subject) for p in _COMMERCIAL_SUBJECT_PATTERNS):
        return True
    return False


def extract_reply_needed(subject: str, body: str | None,
                         classification: str, is_direct: bool) -> bool:
    """Return True if Louis likely needs to reply to this thread."""
    if not is_direct:
        return False
    if classification in ("urgent", "today"):
        return True
    text = (subject + " " + (body or "")).lower()
    if _QUESTION_PATTERN.search(text):
        return True
    for pat in _COMMITMENT_PATTERNS[:3]:
        if pat.search(text):
            return True
    # "Re:" subject in a direct thread suggests ongoing exchange
    if re.match(r'^re:', subject, re.I):
        return True
    return False


def extract_due_date(subject: str, body: str | None) -> str | None:
    """Return ISO due date string if a deadline is found, else None."""
    text = subject + " " + (body or "")
    for pat in _DEADLINE_PATTERNS:
        m = pat.search(text)
        if m:
            # Return the matched phrase as a note; not full date parsing
            return m.group(0).strip()[:40]
    return None


def extract_topic_tags(subject: str, body: str | None,
                       from_email: str) -> list[str]:
    """Simple keyword-based topic tags from subject + body."""
    text = (subject + " " + (body or "")).lower()
    tags = []
    if any(w in text for w in ("invoice", "payment", "billing", "receipt", "check")):
        tags.append("financial")
    if any(w in text for w in ("meeting", "agenda", "calendar", "schedule", "zoom", "call")):
        tags.append("meeting")
    if any(w in text for w in ("manuscript", "paper", "article", "chapter", "book", "press")):
        tags.append("academic")
    if any(w in text for w in ("student", "course", "grade", "class", "lecture", "syllabus")):
        tags.append("teaching")
    if any(w in text for w in ("deadline", "due", "asap", "urgent", "important")):
        tags.append("deadline")
    if any(w in text for w in ("travel", "flight", "hotel", "conference", "trip")):
        tags.append("travel")
    if re.search(r'@(jhu|jh|columbia|nyu|harvard|mit|stanford|yale)\.edu', from_email):
        tags.append("academic")
    return list(set(tags))


# ── gog body fetching ──────────────────────────────────────────────────────────

def fetch_body_via_gog(thread_id: str, account: str,
                       timeout: int = BODY_FETCH_TIMEOUT) -> str | None:
    """Try to fetch message body for a thread via gog CLI.

    Uses 'gog gmail messages search thread:THREAD_ID' to get per-message data.
    Returns plain-text body (first 1200 chars) or None if unavailable.
    """
    try:
        cmd = [
            "gog", "-a", account,
            "gmail", "messages", "search",
            f"thread:{thread_id}",
            "--max", "3", "-j",
        ]
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if cp.returncode != 0:
            return None
        data = json.loads(cp.stdout)
        messages = data.get("messages", data if isinstance(data, list) else [])
        for msg in messages:
            # Body may be under 'body', 'snippet', 'text', or 'plain'
            for field in ("body", "snippet", "plain", "text"):
                body = msg.get(field, "")
                if body and len(body) > 20:
                    # Strip HTML tags if present
                    body = re.sub(r'<[^>]+>', ' ', body)
                    body = re.sub(r'\s+', ' ', body).strip()
                    return body[:1200]
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


# ── Rex contacts lookup ────────────────────────────────────────────────────────

def lookup_rex_connection(from_email: str, conn_rex: sqlite3.Connection | None) -> str | None:
    """Return Rex connection_id for a given email address, or None."""
    if conn_rex is None:
        return None
    try:
        row = conn_rex.execute(
            "SELECT id FROM connections WHERE lower(notes) LIKE ? LIMIT 1",
            (f"%{from_email.lower()}%",),
        ).fetchone()
        if row:
            return row[0]
    except sqlite3.Error:
        pass
    return None


# ── Gmail intake → email_threads ───────────────────────────────────────────────

def ingest_gmail(conn: sqlite3.Connection, conn_rex: sqlite3.Connection | None,
                 dry_run: bool, skip_body: bool) -> int:
    """Read gmail-intake-latest.json and upsert email_threads + contact_signals."""
    if not GMAIL_INTAKE.exists():
        log.warning("Gmail intake not found: %s", GMAIL_INTAKE)
        return 0

    try:
        data = json.loads(GMAIL_INTAKE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read Gmail intake: %s", exc)
        return 0

    threads = data.get("classifications", data.get("threads", []))
    log.info("Gmail: %d threads in intake", len(threads))
    count = 0
    body_fetches = 0

    for thread in threads:
        thread_id = thread.get("thread_id", thread.get("id", ""))
        if not thread_id:
            continue

        classification = thread.get("classification", thread.get("class", "fyi"))
        subject = thread.get("subject", "(no subject)")
        from_raw = thread.get("from", "unknown")
        labels = thread.get("labels", [])
        received_raw = thread.get("received_at", thread.get("date", now_utc()))

        from_name, from_email = parse_from(from_raw)

        # Skip commercial/marketing emails entirely — do not index into polly.db.
        # This keeps contact_signals and email_threads free of retailer noise.
        # Gmail's CATEGORY_PROMOTIONS label is the primary signal; subject-line
        # patterns (sale, unsubscribe, % off) catch what Gmail misses.
        if is_commercial(from_email, labels, subject):
            log.debug("Skipping commercial thread %s | %s | %s",
                      thread_id[:8], from_email, subject[:50])
            continue

        is_direct = is_direct_sender(from_email, from_name, labels)

        # Fetch body for direct emails (rate-limited per run)
        snippet = None
        if is_direct and not skip_body and body_fetches < MAX_DIRECT_BODY_FETCH:
            snippet = fetch_body_via_gog(thread_id, GMAIL_ACCOUNT)
            if snippet:
                body_fetches += 1
                log.debug("Body fetched for %s (%s)", thread_id[:8], subject[:40])

        reply_needed = extract_reply_needed(subject, snippet, classification, is_direct)
        reply_owed_by = "louis" if reply_needed else None
        due_date = extract_due_date(subject, snippet)
        topic_tags = extract_topic_tags(subject, snippet, from_email)
        connection_id = lookup_rex_connection(from_email, conn_rex)

        row_id = stable_id("gmail", thread_id)

        if not dry_run:
            conn.execute("""
                INSERT INTO email_threads
                    (id, thread_id, source, from_email, from_name, subject,
                     snippet, received_at, is_direct, reply_needed, reply_owed_by,
                     due_date, topic_tags, classification, connection_id, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    snippet         = COALESCE(excluded.snippet, snippet),
                    reply_needed    = excluded.reply_needed,
                    reply_owed_by   = excluded.reply_owed_by,
                    due_date        = excluded.due_date,
                    topic_tags      = excluded.topic_tags,
                    classification  = excluded.classification,
                    connection_id   = COALESCE(excluded.connection_id, connection_id),
                    updated_at      = excluded.updated_at
            """, (
                row_id, thread_id, "gmail", from_email, from_name, subject,
                snippet, received_raw, int(is_direct),
                int(reply_needed), reply_owed_by,
                due_date, json.dumps(topic_tags) if topic_tags else None,
                classification, connection_id, now_utc(),
            ))
            _upsert_contact_signal(conn, from_email, from_name,
                                   connection_id, received_raw, is_direct, reply_needed)
        count += 1
        log.debug("Gmail thread %s | direct=%s | reply=%s | %s",
                  thread_id[:8], is_direct, reply_needed, subject[:50])

    if not dry_run:
        conn.commit()
    log.info("Gmail: %d threads processed, %d bodies fetched", count, body_fetches)
    return count


# ── Otto sweep → email_threads ─────────────────────────────────────────────────

def _parse_otto_sweep(path: Path) -> list[dict[str, Any]]:
    """Parse otto sweep-log.yaml into a list of message dicts.

    The YAML format is line-based — we avoid a full YAML dependency.
    """
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    messages: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    in_messages = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("messages:"):
            in_messages = True
            continue
        if stripped.startswith("sweep_run:"):
            in_messages = False
            if current:
                messages.append(current)
                current = {}
            continue
        if not in_messages:
            continue
        if stripped.startswith("- subject:"):
            if current:
                messages.append(current)
            val = stripped[len("- subject:"):].strip().strip("'\"")
            current = {"subject": val}
        elif stripped.startswith("sender:") and current:
            current["sender"] = stripped[len("sender:"):].strip().strip("'\"")
        elif stripped.startswith("received:") and current:
            current["received"] = stripped[len("received:"):].strip().strip("'\"")
        elif stripped.startswith("class:") and current:
            current["class"] = stripped[len("class:"):].strip()

    if current:
        messages.append(current)
    return messages


def ingest_otto(conn: sqlite3.Connection, conn_rex: sqlite3.Connection | None,
                dry_run: bool) -> int:
    """Read otto sweep-log.yaml and upsert email_threads for Outlook messages."""
    messages = _parse_otto_sweep(OTTO_SWEEP)
    if not messages:
        log.info("Otto sweep: no messages to ingest")
        return 0

    count = 0
    for msg in messages:
        subject = msg.get("subject", "(no subject)")
        from_raw = msg.get("sender", "unknown")
        classification = msg.get("class", "ROUTINE").lower()
        if classification == "routine":
            classification = "fyi"
        elif classification == "urgent":
            classification = "urgent"
        received_raw = msg.get("received", now_utc())

        from_name, from_email = parse_from(from_raw)

        # Skip commercial/marketing emails — Otto sweep has no Gmail labels,
        # so we rely on domain patterns and subject-line signals only.
        if is_commercial(from_email, None, subject):
            log.debug("Skipping commercial Outlook thread | %s | %s", from_email, subject[:50])
            continue

        is_direct = is_direct_sender(from_email, from_name)

        # No body fetch for Outlook (no gog equivalent)
        reply_needed = extract_reply_needed(subject, None, classification, is_direct)
        reply_owed_by = "louis" if reply_needed else None
        due_date = extract_due_date(subject, None)
        topic_tags = extract_topic_tags(subject, None, from_email)
        connection_id = lookup_rex_connection(from_email, conn_rex)

        # Stable ID from subject+sender since Outlook has no thread ID in sweep log
        row_id = stable_id("outlook", subject, from_email, received_raw[:10])

        if not dry_run:
            conn.execute("""
                INSERT INTO email_threads
                    (id, thread_id, source, from_email, from_name, subject,
                     snippet, received_at, is_direct, reply_needed, reply_owed_by,
                     due_date, topic_tags, classification, connection_id, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    reply_needed    = excluded.reply_needed,
                    classification  = excluded.classification,
                    updated_at      = excluded.updated_at
            """, (
                row_id, row_id, "outlook", from_email, from_name, subject,
                None, received_raw, int(is_direct),
                int(reply_needed), reply_owed_by,
                due_date, json.dumps(topic_tags) if topic_tags else None,
                classification, connection_id, now_utc(),
            ))
            _upsert_contact_signal(conn, from_email, from_name,
                                   connection_id, received_raw, is_direct, reply_needed)
        count += 1

    if not dry_run:
        conn.commit()
    log.info("Otto: %d messages processed", count)
    return count


# ── contact_signals upsert ─────────────────────────────────────────────────────

def _upsert_contact_signal(conn: sqlite3.Connection,
                           from_email: str, from_name: str,
                           connection_id: str | None,
                           received_at: str,
                           is_direct: bool, reply_needed: bool) -> None:
    """Update or create a contact_signals row for this sender."""
    conn.execute("""
        INSERT INTO contact_signals
            (from_email, from_name, connection_id,
             last_email_at, first_email_at,
             total_threads, direct_threads, open_reply_threads, updated_at)
        VALUES (?,?,?,?,?,1,?,?,?)
        ON CONFLICT(from_email) DO UPDATE SET
            from_name           = excluded.from_name,
            connection_id       = COALESCE(excluded.connection_id, connection_id),
            last_email_at       = MAX(last_email_at, excluded.last_email_at),
            first_email_at      = MIN(first_email_at, excluded.first_email_at),
            total_threads       = total_threads + 1,
            direct_threads      = direct_threads + excluded.direct_threads,
            open_reply_threads  = open_reply_threads + excluded.open_reply_threads,
            updated_at          = excluded.updated_at
    """, (
        from_email, from_name, connection_id,
        received_at, received_at,
        int(is_direct), int(reply_needed), now_utc(),
    ))


# ── Rebuild contact_signals totals (periodic refresh) ─────────────────────────

def refresh_contact_signals(conn: sqlite3.Connection) -> None:
    """Recompute contact_signals aggregates from email_threads.

    Run periodically to ensure totals don't drift from incremental upserts.
    Runs once per invocation as a cheap full-table aggregate.
    """
    conn.execute("""
        INSERT INTO contact_signals
            (from_email, from_name, connection_id,
             last_email_at, first_email_at,
             total_threads, direct_threads, open_reply_threads, updated_at)
        SELECT
            from_email,
            MAX(from_name),
            MAX(connection_id),
            MAX(received_at),
            MIN(received_at),
            COUNT(*),
            SUM(is_direct),
            SUM(CASE WHEN reply_needed=1 AND is_direct=1 THEN 1 ELSE 0 END),
            ?
        FROM email_threads
        GROUP BY from_email
        ON CONFLICT(from_email) DO UPDATE SET
            from_name           = excluded.from_name,
            connection_id       = COALESCE(excluded.connection_id, connection_id),
            last_email_at       = excluded.last_email_at,
            first_email_at      = excluded.first_email_at,
            total_threads       = excluded.total_threads,
            direct_threads      = excluded.direct_threads,
            open_reply_threads  = excluded.open_reply_threads,
            updated_at          = excluded.updated_at
    """, (now_utc(),))
    conn.commit()
    log.debug("contact_signals refreshed")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Maxwell email memory ingestion")
    parser.add_argument("--dry-run",        action="store_true")
    parser.add_argument("--verbose", "-v",  action="store_true")
    parser.add_argument("--skip-body-fetch", action="store_true",
                        help="Skip gog body fetching (faster, less rich)")
    parser.add_argument("--refresh-signals", action="store_true",
                        help="Force full rebuild of contact_signals aggregates")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s %(levelname)s: %(message)s",
    )

    if not POLLY_DB.exists():
        log.error("polly.db not found at %s", POLLY_DB)
        raise SystemExit(1)

    # Import ensure_db from polly_ingest to create tables if needed
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from polly_ingest import ensure_db

    conn = ensure_db(POLLY_DB)

    # Rex DB for contact lookups — optional, fail-open
    conn_rex: sqlite3.Connection | None = None
    if REX_DB.exists():
        try:
            conn_rex = sqlite3.connect(str(REX_DB), timeout=5)
            conn_rex.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            log.warning("Could not open Rex DB: %s", exc)

    mode = "[DRY RUN] " if args.dry_run else ""
    log.info("%sMaxwell ingest run starting", mode)

    gmail_count = ingest_gmail(conn, conn_rex, args.dry_run, args.skip_body_fetch)
    otto_count  = ingest_otto(conn, conn_rex, args.dry_run)

    if not args.dry_run and args.refresh_signals:
        refresh_contact_signals(conn)

    conn.close()
    if conn_rex:
        conn_rex.close()

    total = gmail_count + otto_count
    print(f"maxwell_ingest: {total} threads processed "
          f"(gmail={gmail_count} outlook={otto_count})")


if __name__ == "__main__":
    main()
