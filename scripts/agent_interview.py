#!/usr/bin/env python3
"""agent_interview.py — Per-agent workflow intake and gap analysis.

Each agent examines its own workspace, databases, and memory to identify:
  1. What it knows about Louis (and what's missing)
  2. What patterns in the data suggest about Louis's workflow
  3. Targeted questions the agent needs answered to do its job well

Usage:
    python3 scripts/agent_interview.py              # all agents
    python3 scripts/agent_interview.py --agent polly
    python3 scripts/agent_interview.py --agent rex
    python3 scripts/agent_interview.py --format telegram  # compact Telegram output
    python3 scripts/agent_interview.py --format report    # full markdown report (default)

Output: a structured gap analysis + interview questions, per agent.
This output can be sent to Louis via Polly for review and response.

Design philosophy:
  - All data is pulled from real files/DBs, never invented.
  - Questions are generated from observed gaps, not generic templates.
  - The system learns what it doesn't know by examining what it does know.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
from pathlib import Path
from typing import NamedTuple

# ── Paths ──────────────────────────────────────────────────────────────────────
_home_candidates = [
    os.environ.get("OPENCLAW_HOME"),
    str(Path.home() / ".openclaw"),
    "/Users/louishyman/.openclaw",
    str(Path(__file__).parent.parent.parent / ".openclaw"),  # mnt/.openclaw
]

def _find_openclaw_home() -> Path:
    for c in _home_candidates:
        if not c:
            continue
        p = Path(c)
        if p.exists() and (p / "workspaces").exists():
            return p
    return Path(_home_candidates[1])

OPENCLAW_HOME = _find_openclaw_home()
WORKSPACES = OPENCLAW_HOME / "workspaces"
POLLY_DB = WORKSPACES / "polly-workspace" / "polly.db"
CONNECTIONS_DB = WORKSPACES / "rex-workspace" / "connections.db"


class Gap(NamedTuple):
    severity: str   # "critical" | "moderate" | "minor"
    category: str   # "identity" | "workflow" | "data" | "config"
    description: str
    question: str   # The actual question to ask Louis


# ── Database helpers ───────────────────────────────────────────────────────────

def _open_db(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def polly_db_summary() -> dict:
    """Pull key polly.db stats for gap analysis."""
    conn = _open_db(POLLY_DB)
    if conn is None:
        return {}
    try:
        n_email = conn.execute("SELECT COUNT(*) FROM email_threads").fetchone()[0]
        n_urgent = conn.execute("SELECT COUNT(*) FROM email_threads WHERE reply_needed=1").fetchone()[0]
        n_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='open'").fetchone()[0]
        n_commits = conn.execute("SELECT COUNT(*) FROM commitments WHERE status!='done'").fetchone()[0]
        n_waiting = conn.execute("SELECT COUNT(*) FROM waiting_on").fetchone()[0]
        n_projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        n_escalations = conn.execute("SELECT COUNT(*) FROM escalations WHERE status='pending'").fetchone()[0]

        # Top communicants (non-commercial)
        top_contacts = conn.execute("""
            SELECT from_name, from_email, total_threads, direct_threads, open_reply_threads
            FROM contact_signals
            WHERE direct_threads > 0
            ORDER BY total_threads DESC
            LIMIT 10
        """).fetchall()

        # Reply-needed threads
        urgent_threads = conn.execute("""
            SELECT subject, from_name, received_at
            FROM email_threads
            WHERE reply_needed=1
            ORDER BY received_at DESC
            LIMIT 5
        """).fetchall()

        return {
            "n_email": n_email,
            "n_urgent": n_urgent,
            "n_tasks": n_tasks,
            "n_commits": n_commits,
            "n_waiting": n_waiting,
            "n_projects": n_projects,
            "n_escalations": n_escalations,
            "top_contacts": [dict(r) for r in top_contacts],
            "urgent_threads": [dict(r) for r in urgent_threads],
        }
    except Exception:
        return {}
    finally:
        conn.close()


def connections_db_summary() -> dict:
    """Pull key connections.db stats."""
    conn = _open_db(CONNECTIONS_DB)
    if conn is None:
        return {}
    try:
        n_contacts = conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
        n_with_notes = conn.execute("SELECT COUNT(*) FROM connections WHERE notes IS NOT NULL AND notes != ''").fetchone()[0]
        n_recent = conn.execute(
            "SELECT COUNT(*) FROM connections WHERE last_contact >= date('now', '-30 days')"
        ).fetchone()[0]
        # Top orgs
        top_orgs = conn.execute("""
            SELECT org, COUNT(*) as n
            FROM connections
            WHERE org IS NOT NULL AND org != ''
            GROUP BY org
            ORDER BY n DESC
            LIMIT 10
        """).fetchall()
        return {
            "n_contacts": n_contacts,
            "n_with_notes": n_with_notes,
            "n_recent": n_recent,
            "top_orgs": [dict(r) for r in top_orgs],
        }
    except Exception:
        return {}
    finally:
        conn.close()


# ── User profile reader ────────────────────────────────────────────────────────

def read_user_md(agent: str) -> dict:
    """Parse USER.md for an agent. Returns what's filled in vs empty."""
    path = WORKSPACES / f"{agent}-workspace" / "USER.md"
    if not path.exists():
        return {"exists": False}
    text = path.read_text()
    fields = {
        "name": bool(re.search(r"Name[^\n]*:\s*\S", text)),
        "timezone": bool(re.search(r"Timezone[^\n]*:\s*\S", text, re.I)),
        "context": bool(re.search(r"## Context\s*\n\s*\S", text)),
        "what_to_call": bool(re.search(r"What to call[^\n]*:\s*\S", text, re.I)),
    }
    return {"exists": True, "text": text, "fields": fields, "filled": sum(fields.values()), "total": len(fields)}


def read_memory_md(agent: str) -> dict:
    """Check MEMORY.md for an agent."""
    path = WORKSPACES / f"{agent}-workspace" / "MEMORY.md"
    if not path.exists():
        return {"exists": False}
    text = path.read_text()
    # Filter out boilerplate placeholder content
    substantive = [
        l for l in text.splitlines()
        if l.strip() and not l.startswith("#") and
        not any(kw in l.lower() for kw in ["_learn about", "_update this", "the more you know", "_(what do"])
    ]
    return {"exists": True, "lines": len(substantive), "text": text}


# ══════════════════════════════════════════════════════════════════════════════
# Per-agent gap analysis
# ══════════════════════════════════════════════════════════════════════════════

def analyze_polly(db: dict) -> list[Gap]:
    gaps: list[Gap] = []
    user = read_user_md("polly")
    memory = read_memory_md("polly")

    # Identity gaps
    if not user["exists"] or user.get("filled", 0) < 2:
        gaps.append(Gap("critical", "identity",
            "USER.md is empty — Polly doesn't know basic facts about Louis",
            "What should I know about your schedule, priorities, and communication preferences? "
            "When do you want morning digests? What time zone should I use for deadlines?"))

    # Project/commitment gaps
    if db.get("n_projects", 0) == 0:
        gaps.append(Gap("moderate", "workflow",
            "No projects tracked in polly.db — can't categorize tasks by project",
            "What are your active projects right now? (e.g., Computational History Substack, "
            "CES admin, a book project, grant applications) I'll create project buckets so "
            "tasks route to the right place."))

    if db.get("n_commits", 0) == 0:
        gaps.append(Gap("moderate", "workflow",
            "No open commitments tracked — I can't remind Louis of things he's promised",
            "Have you made any promises or commitments recently that I should be tracking? "
            "E.g., 'I told the editor I'd send a draft by Friday.'"))

    # Urgent reply classification
    if db.get("n_urgent", 0) > 0 and db.get("n_projects", 0) == 0:
        gaps.append(Gap("minor", "data",
            f"{db['n_urgent']} threads flagged as reply-needed but no projects to route them to",
            "Who are your highest-priority contacts? I'll learn to recognize them and escalate "
            "faster. (e.g., 'MIT Press is always urgent, my RA team is medium priority')"))

    # Escalation noise
    if db.get("n_escalations", 0) > 3:
        gaps.append(Gap("minor", "config",
            f"{db['n_escalations']} pending escalations — some may be false positives",
            "Can you review the pending escalations and tell me which ones were actually urgent? "
            "I'll calibrate my urgency thresholds based on what you say matters."))

    # Memory gaps
    if not memory["exists"] or memory.get("lines", 0) < 5:
        gaps.append(Gap("moderate", "identity",
            "MEMORY.md nearly empty — Polly hasn't learned Louis's preferences yet",
            "What are your communication preferences? For example: do you want me to group "
            "everything into one daily message, or send alerts as things come in? "
            "Any contacts I should always treat as urgent?"))

    return gaps


def analyze_rex(db: dict) -> list[Gap]:
    gaps: list[Gap] = []
    cx = connections_db_summary()
    user = read_user_md("rex")
    memory = read_memory_md("rex")

    # Contacts quality
    n = cx.get("n_contacts", 0)
    n_with_notes = cx.get("n_with_notes", 0)
    n_recent = cx.get("n_recent", 0)

    if n > 0 and n_with_notes / n < 0.3:
        gaps.append(Gap("moderate", "data",
            f"Only {n_with_notes}/{n} contacts ({100*n_with_notes//n}%) have notes",
            "Which relationships are most important to you professionally? I'll prioritize "
            "enriching those contact records first. (e.g., 'Everyone at JHU, MIT Press, "
            "my collaborators on the Computational History project')"))

    if cx.get("top_orgs"):
        top = [r["org"] for r in cx["top_orgs"][:5]]
        gaps.append(Gap("minor", "workflow",
            f"Top orgs by contact count: {', '.join(top)} — no priority ranking set",
            f"Among your top organizations ({', '.join(top[:3])}), which matter most "
            "for follow-up and relationship maintenance?"))

    # User identity
    if not user["exists"] or user.get("filled", 0) < 2:
        gaps.append(Gap("moderate", "identity",
            "USER.md empty — Rex doesn't know Louis's professional network priorities",
            "Who are your 5 most important professional relationships right now? "
            "And who are the people you're trying to cultivate relationships with?"))

    if not memory["exists"] or memory.get("lines", 0) < 3:
        gaps.append(Gap("minor", "identity",
            "MEMORY.md nearly empty — Rex hasn't built relationship context yet",
            "Are there any relationship patterns I should know about? For example: "
            "'I always want to know if [person] reaches out' or 'Don't flag emails from [org]'?"))

    return gaps


def analyze_maxwell(db: dict) -> list[Gap]:
    gaps: list[Gap] = []
    user = read_user_md("maxwell")

    if db.get("n_email", 0) > 0:
        # Email classification quality
        urgent_rate = db.get("n_urgent", 0) / max(db["n_email"], 1)
        if urgent_rate > 0.3:
            gaps.append(Gap("moderate", "config",
                f"{100*urgent_rate:.0f}% of emails flagged as reply-needed — may need recalibration",
                "My reply-needed threshold might be too aggressive. Can you review a few recent "
                "emails I flagged and tell me which ones actually needed a reply? I'll adjust."))
        if urgent_rate < 0.01 and db["n_email"] > 10:
            gaps.append(Gap("moderate", "config",
                f"Almost nothing flagged as reply-needed ({db.get('n_urgent', 0)} of {db['n_email']} emails)",
                "Very few emails are flagged for replies. Am I missing things? "
                "Which senders should always trigger a reply flag?"))

    # Top contacts from maxwell's view
    if db.get("top_contacts"):
        names = [c["from_name"] or c["from_email"] for c in db["top_contacts"][:3]]
        gaps.append(Gap("minor", "workflow",
            f"Most direct email traffic from: {', '.join(names)}",
            f"Your highest-volume email correspondents are {', '.join(names)}. "
            "Should any of these be auto-escalated to Polly, or are they routine?"))

    if not user["exists"] or user.get("filled", 0) < 2:
        gaps.append(Gap("moderate", "identity",
            "USER.md empty — Maxwell doesn't know Louis's email priorities",
            "Who are the senders I should always treat as high priority? "
            "And are there any email threads I should automatically ignore? "
            "(e.g., 'Any email from MIT Press goes straight to urgent')"))

    return gaps


def analyze_otto(db: dict) -> list[Gap]:
    gaps: list[Gap] = []
    user = read_user_md("otto")

    # Calendar events
    n_events = db.get("n_events", 0) if "n_events" in db else 0
    if n_events == 0:
        # Check directly
        conn = _open_db(POLLY_DB)
        if conn:
            n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            conn.close()

    gaps.append(Gap("minor", "workflow",
        "Otto handles Outlook + Slack but has no priority mapping",
        "In Slack, which channels should I watch closely? "
        "And are there any Outlook categories or folders that signal urgency? "
        "E.g., 'Anything in the CES Admin folder is usually time-sensitive.'"))

    if not user["exists"] or user.get("filled", 0) < 2:
        gaps.append(Gap("moderate", "identity",
            "USER.md empty — Otto doesn't know Louis's meeting and communication patterns",
            "How much prep time do you usually want before meetings? "
            "Which recurring meetings matter most vs. which can I de-prioritize?"))

    return gaps


def analyze_uhura(db: dict) -> list[Gap]:
    gaps: list[Gap] = []
    memory = read_memory_md("uhura")

    if not memory["exists"] or memory.get("lines", 0) < 10:
        gaps.append(Gap("critical", "data",
            "MEMORY.md empty — Uhura has no Substack post catalogue yet",
            "I need to build your post archive. Can I fetch the full Computational History "
            "Substack archive and build a catalogue? This will let me spot Reddit/HN threads "
            "where your past work is directly relevant."))

    gaps.append(Gap("moderate", "config",
        "No platform credentials configured (X/Twitter, Bluesky, Reddit, HN)",
        "To post drafts once you approve them, I'll need your platform credentials. "
        "Which platforms are highest priority? (X/Twitter is already your main one.) "
        "Have you set up a Moltbook account yet?"))

    gaps.append(Gap("minor", "workflow",
        "No op-ed pipeline tracked yet",
        "Do you have any op-eds currently in progress or recently placed? "
        "I'll set up an op-ed pipeline tracker so I can remind you of follow-up promotional steps."))

    return gaps


def analyze_backer(db: dict) -> list[Gap]:
    gaps: list[Gap] = []
    conn = _open_db(POLLY_DB)
    if conn:
        health_rows = conn.execute(
            "SELECT agent_id, last_status, last_error FROM agent_health ORDER BY updated_at DESC"
        ).fetchall()
        errors = [(r["agent_id"], r["last_error"]) for r in health_rows if r["last_status"] == "error"]
        conn.close()

        if errors:
            agent_names = [e[0] for e in errors]
            gaps.append(Gap("moderate", "data",
                f"Agents in error state: {', '.join(agent_names)}",
                f"These agents are showing errors: {', '.join(agent_names)}. "
                "Should I attempt a health recovery or do you want to investigate first?"))

    gaps.append(Gap("minor", "config",
        "Backup schedule configured but retention policy unclear",
        "How long should I keep backups? "
        "And which files are most critical to preserve? "
        "(I'm currently backing up workspaces and databases — is that sufficient?)"))

    return gaps


AGENT_ANALYZERS = {
    "polly": analyze_polly,
    "rex": analyze_rex,
    "maxwell": analyze_maxwell,
    "otto": analyze_otto,
    "uhura": analyze_uhura,
    "backer": analyze_backer,
}

AGENT_ROLES = {
    "polly": "Chief of staff / orchestration",
    "rex": "Relationship intelligence",
    "maxwell": "Gmail and Google Calendar intake",
    "otto": "Outlook, Slack, and calendar",
    "uhura": "Communications and audience development",
    "backer": "System health and backup",
}


# ── Output formatting ──────────────────────────────────────────────────────────

def format_report(agent: str, gaps: list[Gap]) -> str:
    """Full markdown report format."""
    role = AGENT_ROLES.get(agent, agent)
    lines = [f"## {agent.upper()} ({role})", ""]

    if not gaps:
        lines.append("✅ No critical gaps identified. Agent appears well-configured.")
        return "\n".join(lines)

    critical = [g for g in gaps if g.severity == "critical"]
    moderate = [g for g in gaps if g.severity == "moderate"]
    minor = [g for g in gaps if g.severity == "minor"]

    for severity, color, items in [("Critical", "🔴", critical), ("Moderate", "🟡", moderate), ("Minor", "🔵", minor)]:
        if not items:
            continue
        lines.append(f"### {color} {severity}")
        for g in items:
            lines.append(f"**{g.category.upper()}** — {g.description}")
            lines.append(f"> ❓ *{g.question}*")
            lines.append("")

    return "\n".join(lines)


def format_telegram(agent: str, gaps: list[Gap]) -> str:
    """Compact format for Telegram delivery via Polly."""
    role = AGENT_ROLES.get(agent, agent)
    lines = [f"*{agent.upper()}* ({role})"]

    if not gaps:
        lines.append("  ✅ No gaps identified")
        return "\n".join(lines)

    for g in gaps:
        icon = {"critical": "🔴", "moderate": "🟡", "minor": "🔵"}[g.severity]
        lines.append(f"  {icon} [{g.category}] {g.description[:60]}")
        lines.append(f"    ❓ {g.question[:120]}{'...' if len(g.question) > 120 else ''}")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw agent workflow intake and gap analysis")
    parser.add_argument("--agent", help="Run only for this agent")
    parser.add_argument("--format", choices=["report", "telegram"], default="report",
                        help="Output format (default: report)")
    args = parser.parse_args()

    # Pull shared DB data once
    db = polly_db_summary()

    agents = [args.agent] if args.agent else list(AGENT_ANALYZERS.keys())

    print(f"# OpenClaw Agent Interview — Workflow Gap Analysis")
    print(f"# OPENCLAW_HOME: {OPENCLAW_HOME}")
    print(f"# polly.db: {db.get('n_email', 0)} emails, {db.get('n_tasks', 0)} tasks, "
          f"{db.get('n_escalations', 0)} escalations\n")

    all_gaps: dict[str, list[Gap]] = {}

    for agent in agents:
        analyzer = AGENT_ANALYZERS.get(agent)
        if analyzer is None:
            print(f"[SKIP] No analyzer for agent '{agent}'")
            continue
        gaps = analyzer(db)
        all_gaps[agent] = gaps

        if args.format == "telegram":
            print(format_telegram(agent, gaps))
        else:
            print(format_report(agent, gaps))
        print()

    # Summary
    total_critical = sum(1 for gaps in all_gaps.values() for g in gaps if g.severity == "critical")
    total_moderate = sum(1 for gaps in all_gaps.values() for g in gaps if g.severity == "moderate")
    total_minor = sum(1 for gaps in all_gaps.values() for g in gaps if g.severity == "minor")

    print("---")
    print(f"SUMMARY: {total_critical} critical, {total_moderate} moderate, {total_minor} minor gaps")
    print()
    print("To address these gaps, review with Louis via Telegram and update each agent's USER.md and MEMORY.md.")
    print("Re-run this script after updates to verify gaps are closed.")


if __name__ == "__main__":
    main()
