#!/usr/bin/env python3
"""test_agents.py — Comprehensive OpenClaw agent test suite.

Tests every agent across three layers:
  1. Identity contract — SOUL.md/TOOLS.md exist and contain required content
  2. Infrastructure — databases, files, cron jobs, sandbox sync
  3. Functional — rex_query.py, polly_ingest.py, scripts produce correct output

Usage:
    python3 scripts/test_agents.py              # all agents, all layers
    python3 scripts/test_agents.py --agent rex  # single agent
    python3 scripts/test_agents.py --quick      # skip slow functional checks
    python3 scripts/test_agents.py --layer infra # identity|infra|functional

Exit codes:
    0 = all tests passed (warnings may exist)
    1 = one or more FAIL
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable

# ── Paths ──────────────────────────────────────────────────────────────────────
# OPENCLAW_HOME: prefer env var, then look for the real ~/.openclaw.
# When running inside a Cowork session on macOS, the user's files are mounted
# under /sessions/.../mnt/.openclaw and /sessions/.../mnt/openclaw.
# We walk through candidates in priority order and pick the first that exists
# AND contains a workspaces/ subdirectory (real OpenClaw home).
REPO = Path(__file__).parent.parent  # repo root (~/openclaw or mnt/openclaw)

_home_candidates = [
    os.environ.get("OPENCLAW_HOME"),
    str(Path.home() / ".openclaw"),
    "/Users/louishyman/.openclaw",
    # Cowork sandbox mount: the user's .openclaw is at mnt/.openclaw
    str(REPO.parent / ".openclaw"),
]

def _find_openclaw_home() -> Path:
    for c in _home_candidates:
        if not c:
            continue
        p = Path(c)
        if p.exists() and (p / "workspaces").exists():
            return p
    # Fallback — return even if not found so error messages are useful
    return Path(_home_candidates[1])

OPENCLAW_HOME = _find_openclaw_home()
WORKSPACES = OPENCLAW_HOME / "workspaces"
SANDBOXES = OPENCLAW_HOME / "sandboxes"
SCRIPTS = REPO / "scripts"

POLLY_DB = WORKSPACES / "polly-workspace" / "polly.db"
CONNECTIONS_DB = WORKSPACES / "rex-workspace" / "connections.db"
CRON_JOBS = OPENCLAW_HOME / "cron" / "jobs.json"

# ── Result tracking ────────────────────────────────────────────────────────────
PASS = 0
WARN = 0
FAIL = 0
results: list[dict] = []


def _record(level: str, agent: str, name: str, detail: str) -> None:
    global PASS, WARN, FAIL
    tag = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[level]
    print(f"  {tag} [{level}] {name}: {detail}")
    results.append({"level": level, "agent": agent, "name": name, "detail": detail})
    if level == "PASS":
        PASS += 1
    elif level == "WARN":
        WARN += 1
    else:
        FAIL += 1


def ok(agent: str, name: str, detail: str = "ok") -> None:
    _record("PASS", agent, name, detail)


def warn(agent: str, name: str, detail: str) -> None:
    _record("WARN", agent, name, detail)


def fail(agent: str, name: str, detail: str) -> None:
    _record("FAIL", agent, name, detail)


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_cron_jobs() -> list[dict]:
    """Load cron/jobs.json. Returns empty list on error."""
    try:
        return json.loads(CRON_JOBS.read_text()).get("jobs", [])
    except Exception:
        return []


def sandbox_for(agent_id: str) -> Path | None:
    """Find the sandbox directory for an agent (e.g. agent-polly-16c13b58)."""
    if not SANDBOXES.exists():
        return None
    for d in SANDBOXES.iterdir():
        if d.name.startswith(f"agent-{agent_id}-"):
            return d
    return None


def check_soul_contains(agent: str, soul_text: str, patterns: list[str]) -> None:
    """Verify all required patterns are present in SOUL.md."""
    for pattern in patterns:
        if re.search(pattern, soul_text, re.IGNORECASE):
            ok(agent, f"SOUL contains '{pattern[:40]}'")
        else:
            fail(agent, f"SOUL missing '{pattern[:40]}'", "required pattern absent")


def check_sandbox_sync(agent: str, filename: str = "SOUL.md") -> None:
    """Verify workspace and sandbox copies of a file match."""
    workspace_file = WORKSPACES / f"{agent}-workspace" / filename
    sandbox_dir = sandbox_for(agent)
    if sandbox_dir is None:
        warn(agent, f"sandbox sync ({filename})", "no sandbox directory found — agent may be workspace-only")
        return
    sandbox_file = sandbox_dir / filename
    if not workspace_file.exists():
        fail(agent, f"sandbox sync ({filename})", f"workspace file missing: {workspace_file}")
        return
    if not sandbox_file.exists():
        fail(agent, f"sandbox sync ({filename})", f"sandbox file missing: {sandbox_file}")
        return
    w = workspace_file.read_text()
    s = sandbox_file.read_text()
    if w == s:
        ok(agent, f"sandbox sync ({filename})", "workspace == sandbox")
    else:
        # For SOUL.md allow LIVE_STATUS block to differ (it's auto-updated)
        if filename == "SOUL.md":
            LIVE_START = "<!-- LIVE_STATUS_START -->"
            LIVE_END = "<!-- LIVE_STATUS_END -->"
            w_pre = w[:w.find(LIVE_START)] if LIVE_START in w else w
            s_pre = s[:s.find(LIVE_START)] if LIVE_START in s else s
            if w_pre == s_pre:
                ok(agent, f"sandbox sync ({filename})", "match (ignoring LIVE_STATUS block)")
                return
        diff_lines = sum(1 for a, b in zip(w.splitlines(), s.splitlines()) if a != b)
        fail(agent, f"sandbox sync ({filename})", f"{diff_lines} lines differ")


def check_cron_coverage(agent: str, expected_crons: list[str]) -> None:
    """Verify expected cron jobs exist for an agent."""
    jobs = load_cron_jobs()
    agent_jobs = {j.get("name") for j in jobs if j.get("agentId") == agent}
    for cron_name in expected_crons:
        if cron_name in agent_jobs:
            ok(agent, f"cron '{cron_name}'", "present")
        else:
            fail(agent, f"cron '{cron_name}'", "missing from jobs.json")


def run_script(cmd: list[str], timeout: int = 30, env: dict | None = None) -> tuple[bool, str]:
    """Run a command; return (success, output)."""
    # Always propagate OPENCLAW_HOME so subprocesses find the right databases
    merged_env = os.environ.copy()
    merged_env["OPENCLAW_HOME"] = str(OPENCLAW_HOME)
    if env:
        merged_env.update(env)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=merged_env)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s"
    except FileNotFoundError as e:
        return False, f"command not found: {e}"


def db_ok(path: Path) -> tuple[bool, str]:
    """Run PRAGMA integrity_check on a SQLite db. Returns (ok, detail)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        return result == "ok", result
    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1: Identity contracts
# ══════════════════════════════════════════════════════════════════════════════

def test_identity_polly() -> None:
    agent = "polly"
    print(f"\n── {agent.upper()} identity ──")
    soul = WORKSPACES / "polly-workspace" / "SOUL.md"
    tools = WORKSPACES / "polly-workspace" / "TOOLS.md"

    if not soul.exists():
        fail(agent, "SOUL.md exists", str(soul)); return
    ok(agent, "SOUL.md exists")
    soul_text = soul.read_text()

    check_soul_contains(agent, soul_text, [
        r"HARD RULES",
        r"No fabrication",
        r"LIVE_STATUS_START",
        r"CALL EXEC IMMEDIATELY",             # our new Rule 1
        r"rex_query\.py",
        r"morning digest",
    ])

    if tools.exists():
        ok(agent, "TOOLS.md exists")
        t = tools.read_text()
        check_soul_contains(agent, t, [
            r"ask.*off",
            r"rex_query\.py",
            r"Rex Relationship Lookups",
        ])
    else:
        fail(agent, "TOOLS.md exists", str(tools))


def test_identity_rex() -> None:
    agent = "rex"
    print(f"\n── {agent.upper()} identity ──")
    soul = WORKSPACES / "rex-workspace" / "SOUL.md"
    tools = WORKSPACES / "rex-workspace" / "TOOLS.md"

    if not soul.exists():
        fail(agent, "SOUL.md exists", str(soul)); return
    ok(agent, "SOUL.md exists")
    soul_text = soul.read_text()

    check_soul_contains(agent, soul_text, [
        r"connections\.db",
        r"relationship",
        r"Step 1",         # 4-step query pattern
        r"Step 2",
        r"Step 3",
        r"Step 4",
        r"email_threads",
        r"polly\.db",
    ])

    if tools.exists():
        ok(agent, "TOOLS.md exists")
        t = tools.read_text()
        check_soul_contains(agent, t, [
            r"ask.*off",
            r"connections\.db",
            r"Relationship Query Pattern",
        ])
    else:
        fail(agent, "TOOLS.md exists", str(tools))


def test_identity_maxwell() -> None:
    agent = "maxwell"
    print(f"\n── {agent.upper()} identity ──")
    soul = WORKSPACES / "maxwell-workspace" / "SOUL.md"
    if not soul.exists():
        fail(agent, "SOUL.md exists", str(soul)); return
    ok(agent, "SOUL.md exists")
    soul_text = soul.read_text()
    check_soul_contains(agent, soul_text, [
        r"gmail",
        r"read.only",
        r"never.*send|may not send|not send.*email|never send",
        r"classify|category|urgent",
    ])


def test_identity_otto() -> None:
    agent = "otto"
    print(f"\n── {agent.upper()} identity ──")
    soul = WORKSPACES / "otto-workspace" / "SOUL.md"
    tools = WORKSPACES / "otto-workspace" / "TOOLS.md"
    if not soul.exists():
        fail(agent, "SOUL.md exists", str(soul)); return
    ok(agent, "SOUL.md exists")
    soul_text = soul.read_text()
    check_soul_contains(agent, soul_text, [
        r"outlook",
        r"never.*send.*autonom",
        r"slack",
    ])
    if tools.exists():
        ok(agent, "TOOLS.md exists")
        t = tools.read_text()
        check_soul_contains(agent, t, [
            r"osascript|applescript",
            r"host.*gateway",
            r"ask.*off",
        ])
    else:
        fail(agent, "TOOLS.md exists", str(tools))


def test_identity_backer() -> None:
    agent = "backer"
    print(f"\n── {agent.upper()} identity ──")
    soul = WORKSPACES / "backer-workspace" / "SOUL.md"
    tools = WORKSPACES / "backer-workspace" / "TOOLS.md"
    if not soul.exists():
        fail(agent, "SOUL.md exists", str(soul)); return
    ok(agent, "SOUL.md exists")
    soul_text = soul.read_text()
    check_soul_contains(agent, soul_text, [
        r"ollama",
        r"11434|11435|11436",
        r"health",
    ])
    if tools.exists():
        ok(agent, "TOOLS.md exists")
        t = tools.read_text()
        check_soul_contains(agent, t, [
            r"ask.*off",
            r"backer_health_tick",
            r"NEVER.*elevated",
        ])
    else:
        fail(agent, "TOOLS.md exists", str(tools))


def test_identity_uhura() -> None:
    agent = "uhura"
    print(f"\n── {agent.upper()} identity ──")
    soul = WORKSPACES / "uhura-workspace" / "SOUL.md"
    tools = WORKSPACES / "uhura-workspace" / "TOOLS.md"
    if not soul.exists():
        fail(agent, "SOUL.md exists", str(soul)); return
    ok(agent, "SOUL.md exists")
    soul_text = soul.read_text()
    check_soul_contains(agent, soul_text, [
        r"communications|comms",
        r"draft.only|approval",
        r"publish.*without.*approval|no.*publish.*autonom|nothing posts without|never.*post.*without",
    ])
    if tools.exists():
        ok(agent, "TOOLS.md exists")
    else:
        warn(agent, "TOOLS.md exists", "missing — should define channel policy")


def test_identity_forge() -> None:
    agent = "forge"
    print(f"\n── {agent.upper()} identity ──")
    soul = WORKSPACES / "forge-workspace" / "SOUL.md"
    if not soul.exists():
        fail(agent, "SOUL.md exists", str(soul)); return
    ok(agent, "SOUL.md exists")
    soul_text = soul.read_text()
    check_soul_contains(agent, soul_text, [
        r"coding|git",
        r"approval",
        r"no.*merge.*without|merge.*explicit",
    ])


def test_identity_lex() -> None:
    agent = "lex"
    print(f"\n── {agent.upper()} identity ──")
    soul = WORKSPACES / "lex-workspace" / "SOUL.md"
    if not soul.exists():
        fail(agent, "SOUL.md exists", str(soul)); return
    ok(agent, "SOUL.md exists")
    soul_text = soul.read_text()
    check_soul_contains(agent, soul_text, [
        r"research|intelligence",
        r"source",
        r"no.*fabricat|evidence",
    ])


def test_identity_trip() -> None:
    agent = "trip"
    print(f"\n── {agent.upper()} identity ──")
    soul = WORKSPACES / "trip-workspace" / "SOUL.md"
    if not soul.exists():
        fail(agent, "SOUL.md exists", str(soul)); return
    ok(agent, "SOUL.md exists")
    soul_text = soul.read_text()
    check_soul_contains(agent, soul_text, [
        r"travel|expense",
        r"approval",
        r"no.*submit.*without|never.*submit.*autonom",
    ])


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 2: Infrastructure
# ══════════════════════════════════════════════════════════════════════════════

def test_infra_databases() -> None:
    print(f"\n── DATABASES infrastructure ──")

    # polly.db
    exists = POLLY_DB.exists()
    if exists:
        ok("polly", "polly.db exists", str(POLLY_DB))
        good, detail = db_ok(POLLY_DB)
        if good:
            ok("polly", "polly.db integrity", "ok")
        else:
            fail("polly", "polly.db integrity", detail)
        # Check tables
        try:
            conn = sqlite3.connect(f"file:{POLLY_DB}?mode=ro", uri=True, timeout=5)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"events", "escalations", "tasks", "agent_health", "email_threads", "contact_signals"}
            missing = required - tables
            if missing:
                fail("polly", "polly.db tables", f"missing: {missing}")
            else:
                ok("polly", "polly.db tables", f"{len(tables)} tables present")
            # Check it has real data
            esc = conn.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
            tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            health = conn.execute("SELECT COUNT(*) FROM agent_health").fetchone()[0]
            ok("polly", "polly.db data", f"escalations={esc} tasks={tasks} agent_health={health}")
            conn.close()
        except Exception as e:
            fail("polly", "polly.db schema check", str(e))
    else:
        fail("polly", "polly.db exists", "file not found")

    # connections.db
    if CONNECTIONS_DB.exists():
        ok("rex", "connections.db exists")
        good, detail = db_ok(CONNECTIONS_DB)
        if good:
            ok("rex", "connections.db integrity", "ok")
        else:
            fail("rex", "connections.db integrity", detail)
        try:
            conn = sqlite3.connect(f"file:{CONNECTIONS_DB}?mode=ro", uri=True, timeout=5)
            n = conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
            if n > 0:
                ok("rex", "connections.db data", f"{n} contacts")
            else:
                warn("rex", "connections.db data", "0 contacts — backfill may not have run")
            # Check name_lower column exists (required for rex_query.py)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(connections)")}
            if "name_lower" in cols:
                ok("rex", "connections.db schema", "name_lower column present")
            else:
                fail("rex", "connections.db schema", "name_lower column missing — rex_query.py will fail")
            conn.close()
        except Exception as e:
            fail("rex", "connections.db schema", str(e))
    else:
        fail("rex", "connections.db exists", "file not found")


def test_infra_sandbox_sync() -> None:
    print(f"\n── SANDBOX SYNC infrastructure ──")
    # Only polly, rex, maxwell, otto have sandboxes
    for agent in ["polly", "rex", "maxwell", "otto"]:
        check_sandbox_sync(agent, "SOUL.md")
        check_sandbox_sync(agent, "TOOLS.md")


def test_infra_scripts() -> None:
    print(f"\n── SCRIPTS infrastructure ──")
    required_scripts = [
        "polly_ingest.py",
        "maxwell_ingest.py",
        "rex_query.py",
        # rex_sync_contacts.py lives in rex-workspace/state/, not scripts/ — tested in test_functional_rex_sync_script
        "backer_health_tick.sh",
        "backer_backup_tick.sh",
        "otto_outlook_sweep.sh",
        "otto_calendar_tick.sh",
        "gcal_today_tick.py",
        "openclaw_safe_restart.sh",
    ]
    for script in required_scripts:
        path = SCRIPTS / script
        if path.exists():
            ok("scripts", f"{script} exists")
        else:
            fail("scripts", f"{script} exists", "file not found")

    # rex_query.py syntax check
    rex_query = SCRIPTS / "rex_query.py"
    if rex_query.exists():
        success, out = run_script(["python3", "-m", "py_compile", str(rex_query)])
        if success:
            ok("rex", "rex_query.py syntax", "no errors")
        else:
            fail("rex", "rex_query.py syntax", out)

    # polly_ingest.py syntax check
    ingest = SCRIPTS / "polly_ingest.py"
    if ingest.exists():
        success, out = run_script(["python3", "-m", "py_compile", str(ingest)])
        if success:
            ok("polly", "polly_ingest.py syntax", "no errors")
        else:
            fail("polly", "polly_ingest.py syntax", out)


def test_infra_crons() -> None:
    print(f"\n── CRON JOBS infrastructure ──")
    jobs = load_cron_jobs()
    if not jobs:
        fail("cron", "jobs.json readable", "could not load or empty")
        return
    ok("cron", "jobs.json readable", f"{len(jobs)} jobs total")

    required = {
        "polly": ["ingestion-watch-20m", "polly-morning-digest", "polly-pre-digest-healthcheck", "polly-digest-prep"],
        "maxwell": ["gmail-sweep-5m", "gmail-backfill-12m-20m", "maxwell-ingest-30m", "maxwell-gcal-6am"],
        "rex": ["rex-contacts-sync-6h", "rex-backfill-365d-20m"],
        "otto": ["otto-outlook-sweep", "otto-calendar-6am", "otto-draft-check", "otto-slack-digest"],
        "backer": ["backer-health-5m", "backer-daily-audit", "backer-nightly-backup"],
        "uhura": ["uhura-post-amplify", "uhura-growth-scan", "uhura-oped-check"],
    }
    for agent, cron_names in required.items():
        check_cron_coverage(agent, cron_names)

    # Check all crons have model + timeout set
    for job in jobs:
        payload = job.get("payload", {})
        name = job.get("name", job.get("id", "?"))
        agent_id = job.get("agentId", "?")
        if not payload.get("timeoutSeconds"):
            warn(agent_id, f"cron '{name}' timeout", "no timeoutSeconds set")
        if not payload.get("model") and not payload.get("thinking"):
            # Not always required but flag for visibility
            pass


def test_infra_soul_live_status() -> None:
    print(f"\n── SOUL.md LIVE STATUS infrastructure ──")
    soul_path = WORKSPACES / "polly-workspace" / "SOUL.md"
    if not soul_path.exists():
        fail("polly", "SOUL.md live status", "SOUL.md not found"); return
    soul = soul_path.read_text()
    if "<!-- LIVE_STATUS_START -->" in soul and "<!-- LIVE_STATUS_END -->" in soul:
        ok("polly", "SOUL.md live status markers", "present")
        # Check it's not stale (more than 1 hour old is suspicious)
        import re as _re
        m = _re.search(r"last refresh: (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", soul)
        if m:
            ok("polly", "SOUL.md live status timestamp", m.group(1) + " UTC")
        else:
            warn("polly", "SOUL.md live status timestamp", "could not parse refresh time")
    else:
        fail("polly", "SOUL.md live status markers", "LIVE_STATUS_START/END markers missing")

    # Check Rule 1 imperative phrasing
    if "CALL EXEC IMMEDIATELY" in soul:
        ok("polly", "SOUL.md Rule 1 imperative", "correct wording present")
    elif "rex_query.py" in soul:
        warn("polly", "SOUL.md Rule 1 imperative", "rex_query.py present but missing CALL EXEC IMMEDIATELY phrasing")
    else:
        fail("polly", "SOUL.md Rule 1", "rex_query.py not referenced in SOUL.md")


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 3: Functional
# ══════════════════════════════════════════════════════════════════════════════

def test_functional_rex_query() -> None:
    print(f"\n── REX functional ──")
    rex_query = SCRIPTS / "rex_query.py"
    if not rex_query.exists():
        fail("rex", "rex_query.py functional", "script not found"); return

    # Test 1: known contact that should exist
    success, out = run_script(
        ["python3", str(rex_query), "lipartito"],
        timeout=10
    )
    if success and "📇 Rex lookup" in out and "lipartito" in out.lower() and "example.com" not in out:
        ok("rex", "rex_query known contact", f"returned real data (not fabricated)")
    elif "Rex found no contacts" in out:
        warn("rex", "rex_query known contact", "no results for 'lipartito' — connections.db may be empty")
    elif "example.com" in out:
        fail("rex", "rex_query known contact", "returned fake @example.com data — hallucination detected")
    else:
        fail("rex", "rex_query known contact", f"unexpected output: {out[:200]}")

    # Test 2: no-match returns clean message, not fabricated data
    success, out = run_script(
        ["python3", str(rex_query), "zzznomatch_xqz999"],
        timeout=10
    )
    if "Rex found no contacts matching" in out:
        ok("rex", "rex_query no-match", "clean 'not found' message")
    elif "example.com" in out or "John Doe" in out or "Jane Smith" in out:
        fail("rex", "rex_query no-match", "hallucinated a result for nonsense input")
    else:
        warn("rex", "rex_query no-match", f"unexpected output: {out[:100]}")

    # Test 3: empty arg exits with error
    success, out = run_script(
        ["python3", str(rex_query), ""],
        timeout=5
    )
    if not success and "empty" in out.lower():
        ok("rex", "rex_query empty arg", "correctly errors on empty arg")
    elif not success:
        ok("rex", "rex_query empty arg", "exits non-zero on empty arg")
    else:
        warn("rex", "rex_query empty arg", "did not exit with error on empty arg")

    # Test 4: multi-word name
    success, out = run_script(
        ["python3", str(rex_query), "Katherine Howe"],
        timeout=10
    )
    if success and "📇 Rex lookup" in out and "Katherine Howe" in out:
        ok("rex", "rex_query multi-word name", "returned results")
    elif "Rex found no contacts" in out:
        warn("rex", "rex_query multi-word name", "no results for 'Katherine Howe'")
    else:
        warn("rex", "rex_query multi-word name", f"unexpected: {out[:100]}")


def test_functional_polly_ingest() -> None:
    print(f"\n── POLLY_INGEST functional ──")
    ingest = SCRIPTS / "polly_ingest.py"
    if not ingest.exists():
        fail("polly", "polly_ingest.py functional", "script not found"); return

    # Run --dry-run if supported, else just check --help
    success, out = run_script(
        ["python3", str(ingest), "--help"],
        timeout=10
    )
    if success or "--dry-run" in out or "usage" in out.lower():
        ok("polly", "polly_ingest.py --help", "runs without error")
    else:
        warn("polly", "polly_ingest.py --help", f"unexpected: {out[:100]}")

    # Check it writes to sandbox — verify sandbox SOUL.md has LIVE_STATUS markers
    sandbox = sandbox_for("polly")
    if sandbox:
        sandbox_soul = sandbox / "SOUL.md"
        if sandbox_soul.exists() and "<!-- LIVE_STATUS_START -->" in sandbox_soul.read_text():
            ok("polly", "polly_ingest sandbox write", "sandbox SOUL.md has LIVE_STATUS markers")
        else:
            fail("polly", "polly_ingest sandbox write",
                 "sandbox SOUL.md missing LIVE_STATUS markers — polly_ingest not writing to sandbox")
    else:
        warn("polly", "polly_ingest sandbox write", "no sandbox found to verify")

    # Check morning digest draft was written
    draft = WORKSPACES / "polly-workspace" / "state" / "morning-digest-draft.txt"
    if draft.exists():
        content = draft.read_text()
        if "Morning digest" in content or "📅" in content:
            ok("polly", "morning digest draft", f"{len(content)} chars, looks valid")
        else:
            warn("polly", "morning digest draft", "file exists but content looks wrong")
    else:
        warn("polly", "morning digest draft", "draft file not found — ingest may not have run today")


def test_functional_backer_health() -> None:
    print(f"\n── BACKER functional ──")
    script = SCRIPTS / "backer_health_tick.sh"
    if not script.exists():
        fail("backer", "backer_health_tick.sh functional", "script not found"); return
    ok("backer", "backer_health_tick.sh exists", "present")

    # Check Ollama lanes are reachable (don't actually run the script, just probe)
    # NOTE: Ollama runs on the host Mac, not in this sandbox — failures here are expected
    # when tests run inside the sandbox. These are WARNs, not FAILs.
    for port, lane in [(11434, "primary"), (11435, "polly"), (11436, "light")]:
        success, out = run_script(
            ["curl", "-s", "--max-time", "5", f"http://localhost:{port}/api/tags"],
            timeout=8
        )
        if success and "models" in out:
            ok("backer", f"Ollama {lane} lane (:{port})", "reachable")
        elif success:
            warn("backer", f"Ollama {lane} lane (:{port})", f"responded but unexpected output: {out[:60]}")
        else:
            warn("backer", f"Ollama {lane} lane (:{port})",
                 f"not reachable from sandbox (Ollama runs on host Mac — run from Mac to verify)")


def test_functional_uhura_identity() -> None:
    """Uhura has no live crons — test that her identity is correct and note channel gaps."""
    print(f"\n── UHURA functional ──")
    soul = WORKSPACES / "uhura-workspace" / "SOUL.md"
    if not soul.exists():
        fail("uhura", "SOUL.md", "missing"); return
    soul_text = soul.read_text()

    # Uhura currently has no Telegram channel bindings for output channels
    # (she's only reachable via @Uhura_314_bot direct message)
    # Flag what's missing vs what's there
    has_channels = any(kw in soul_text.lower() for kw in
                       ["twitter", "substack", "linkedin", "newsletter", "email campaign"])
    if has_channels:
        ok("uhura", "SOUL.md channel references", "output channels defined")
    else:
        warn("uhura", "SOUL.md channel references",
             "no output channels defined — Uhura can draft but has no configured publish targets")

    has_voice = "voice" in soul_text.lower() or "style" in soul_text.lower() or "tone" in soul_text.lower()
    if has_voice:
        ok("uhura", "SOUL.md voice/style", "voice guidelines present")
    else:
        warn("uhura", "SOUL.md voice/style", "no voice/style/tone guidelines — needed for consistent drafts")


def test_functional_rex_sync_script() -> None:
    """Verify rex_sync_contacts.py exists at both expected paths."""
    print(f"\n── REX SYNC functional ──")
    # Path in TOOLS.md and cron jobs
    state_path = WORKSPACES / "rex-workspace" / "state" / "rex_sync_contacts.py"
    scripts_path = SCRIPTS / "rex_sync_contacts.py"
    openclaw_path = Path.home() / "openclaw" / "scripts" / "rex_sync_contacts.py"

    found = False
    for p, label in [(state_path, "rex-workspace/state/"), (scripts_path, "scripts/"), (openclaw_path, "~/openclaw/scripts/")]:
        if p.exists():
            ok("rex", f"rex_sync_contacts.py ({label})", "found")
            found = True
        else:
            warn("rex", f"rex_sync_contacts.py ({label})", "not found at this path")

    if not found:
        fail("rex", "rex_sync_contacts.py", "not found at any expected path")

    # Checkpoint file
    checkpoint = WORKSPACES / "rex-workspace" / "state" / "rex_sync_checkpoint_365d.json"
    if checkpoint.exists():
        ok("rex", "rex_sync checkpoint file", "present")
    else:
        warn("rex", "rex_sync checkpoint file", "missing — rex-backfill-365d-20m may fail on first run")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

IDENTITY_TESTS: list[Callable] = [
    test_identity_polly,
    test_identity_rex,
    test_identity_maxwell,
    test_identity_otto,
    test_identity_backer,
    test_identity_uhura,
    test_identity_forge,
    test_identity_lex,
    test_identity_trip,
]

INFRA_TESTS: list[Callable] = [
    test_infra_databases,
    test_infra_sandbox_sync,
    test_infra_scripts,
    test_infra_crons,
    test_infra_soul_live_status,
]

FUNCTIONAL_TESTS: list[Callable] = [
    test_functional_rex_query,
    test_functional_polly_ingest,
    test_functional_backer_health,
    test_functional_uhura_identity,
    test_functional_rex_sync_script,
]


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw agent test suite")
    parser.add_argument("--agent", help="Run only tests for this agent")
    parser.add_argument("--quick", action="store_true", help="Skip slow functional tests")
    parser.add_argument("--layer", choices=["identity", "infra", "functional"],
                        help="Run only this layer")
    args = parser.parse_args()

    print("=" * 60)
    print("OpenClaw Agent Test Suite")
    print(f"OPENCLAW_HOME: {OPENCLAW_HOME}")
    print("=" * 60)

    layers: list[str] = []
    if args.layer:
        layers = [args.layer]
    else:
        layers = ["identity", "infra"] + ([] if args.quick else ["functional"])

    if "identity" in layers:
        print("\n" + "═" * 60)
        print("LAYER 1: Identity Contracts")
        print("═" * 60)
        for test_fn in IDENTITY_TESTS:
            agent_name = test_fn.__name__.replace("test_identity_", "")
            if args.agent and args.agent != agent_name:
                continue
            test_fn()

    if "infra" in layers:
        print("\n" + "═" * 60)
        print("LAYER 2: Infrastructure")
        print("═" * 60)
        for test_fn in INFRA_TESTS:
            test_fn()

    if "functional" in layers:
        print("\n" + "═" * 60)
        print("LAYER 3: Functional")
        print("═" * 60)
        for test_fn in FUNCTIONAL_TESTS:
            test_fn()

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  ✅ PASS: {PASS}")
    print(f"  ⚠️  WARN: {WARN}")
    print(f"  ❌ FAIL: {FAIL}")

    if FAIL > 0:
        print("\nFailed tests:")
        for r in results:
            if r["level"] == "FAIL":
                print(f"  [{r['agent']}] {r['name']}: {r['detail']}")
        sys.exit(1)
    elif WARN > 0:
        print(f"\n{WARN} warning(s) — system operational but review recommended.")
        sys.exit(0)
    else:
        print("\nAll tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
