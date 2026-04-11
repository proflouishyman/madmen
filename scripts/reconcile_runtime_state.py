#!/usr/bin/env python3
"""Reconcile stale OpenClaw runtime state.

Purpose:
- mark stale `running` task rows as `lost` (for runtimes that cannot be cancelled)
- clear stale `status=running` markers from per-agent session stores
- remove orphaned `*.jsonl.lock` files whose PIDs no longer exist

This script is safe to run at startup and periodically during runtime.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


RUNTIMES_TO_RECONCILE = ("cli", "subagent")


@dataclass
class ReconcileSummary:
    db_exists: bool
    db_backed_up_to: str | None
    running_candidates: int
    tasks_marked_lost: int
    duplicate_cron_marked_lost: int
    session_files_seen: int
    session_entries_running: int
    sessions_marked_idle: int
    lock_files_seen: int
    lock_files_removed: int
    skipped_due_to_active_runtime: bool
    active_runtime_pids: list[str]


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pgrep_exact(name: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["pgrep", "-x", name],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _active_runtime_pids() -> list[str]:
    # OpenClaw can run with different process names depending on install/runtime.
    pids = set()
    for exe in ("openclaw-gateway", "openclaw-agent", "openclaw"):
        pids.update(_pgrep_exact(exe))
    return sorted(pids)


def _cleanup_orphan_locks(openclaw_home: Path, dry_run: bool) -> tuple[int, int]:
    pattern = str(openclaw_home / "agents" / "*" / "sessions" / "*.jsonl.lock")
    lock_paths = sorted(glob.glob(pattern))
    removed = 0
    for lock_path in lock_paths:
        try:
            payload = json.loads(Path(lock_path).read_text(encoding="utf-8"))
            pid = int(payload.get("pid", 0))
        except Exception:
            pid = 0
        if _pid_exists(pid):
            continue
        if not dry_run:
            try:
                Path(lock_path).unlink(missing_ok=True)
            except Exception:
                continue
        removed += 1
    return len(lock_paths), removed


def _backup_db(db_path: Path, dry_run: bool) -> str | None:
    if dry_run or not db_path.exists():
        return None
    ts = time.strftime("%Y%m%dT%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak-runtime-reconcile-{ts}")
    shutil.copy2(db_path, backup_path)
    return str(backup_path)


def _reconcile_running_tasks(
    db_path: Path, grace_seconds: int, dry_run: bool, reason: str
) -> tuple[int, int]:
    if not db_path.exists():
        return 0, 0
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - max(0, grace_seconds) * 1000
    placeholders = ",".join("?" for _ in RUNTIMES_TO_RECONCILE)
    # Non-obvious invariant: a `running` row must not already have terminal markers.
    # If ended_at/terminal_outcome is present, treat it as stale immediately.
    where_clause = (
        "status='running' "
        f"AND runtime IN ({placeholders}) "
        "AND ("
        "COALESCE(last_event_at, started_at, created_at, 0) <= ? "
        "OR ended_at IS NOT NULL "
        "OR terminal_outcome IS NOT NULL"
        ")"
    )
    params: Iterable[object] = tuple(RUNTIMES_TO_RECONCILE) + (cutoff_ms,)

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM task_runs WHERE {where_clause}", params)
        candidates = int(cur.fetchone()[0] or 0)
        if dry_run or candidates == 0:
            return candidates, 0
        cur.execute(
            (
                "UPDATE task_runs "
                "SET status='lost', "
                "error=?, "
                "terminal_outcome='error', "
                "ended_at=?, "
                "last_event_at=?, "
                "cleanup_after=? "
                f"WHERE {where_clause}"
            ),
            (reason, now_ms, now_ms, now_ms + 86400000, *params),
        )
        conn.commit()
        return candidates, int(cur.rowcount or 0)


def _reconcile_duplicate_running_cron_tasks(db_path: Path, dry_run: bool, reason: str) -> int:
    if not db_path.exists():
        return 0

    query = (
        "SELECT task_id, source_id, COALESCE(started_at, created_at, 0) AS ts "
        "FROM task_runs "
        "WHERE runtime='cron' AND status='running' AND source_id IS NOT NULL AND source_id <> '' "
        "ORDER BY source_id, ts DESC"
    )

    to_mark: list[str] = []
    seen_sources: set[str] = set()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(query).fetchall()
        for task_id, source_id, _ts in rows:
            source = str(source_id)
            if source not in seen_sources:
                seen_sources.add(source)
                continue
            to_mark.append(str(task_id))

        if dry_run or not to_mark:
            return len(to_mark)

        now_ms = int(time.time() * 1000)
        placeholders = ",".join("?" for _ in to_mark)
        conn.execute(
            (
                "UPDATE task_runs "
                "SET status='lost', "
                "error=?, "
                "terminal_outcome='error', "
                "ended_at=?, "
                "last_event_at=?, "
                "cleanup_after=? "
                f"WHERE task_id IN ({placeholders})"
            ),
            (reason, now_ms, now_ms, now_ms + 86400000, *to_mark),
        )
        conn.commit()
        return len(to_mark)


def _recent_running_task_keys(db_path: Path, cutoff_ms: int) -> set[str]:
    if not db_path.exists():
        return set()
    query = (
        "SELECT child_session_key "
        "FROM task_runs "
        "WHERE status='running' "
        "AND child_session_key IS NOT NULL "
        "AND child_session_key <> '' "
        "AND COALESCE(last_event_at, started_at, created_at, 0) > ?"
    )
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(query, (cutoff_ms,)).fetchall()
    return {str(row[0]) for row in rows if row and row[0]}


def _reconcile_running_sessions(
    openclaw_home: Path, db_path: Path, grace_seconds: int, dry_run: bool
) -> tuple[int, int, int]:
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - max(0, grace_seconds) * 1000
    active_task_session_keys = _recent_running_task_keys(db_path, cutoff_ms)

    store_paths = sorted((openclaw_home / "agents").glob("*/sessions/sessions.json"))
    running_entries = 0
    marked_idle = 0

    for store_path in store_paths:
        try:
            payload = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue

        changed = False
        for session_key, record in payload.items():
            if not isinstance(record, dict):
                continue
            if record.get("status") != "running":
                continue

            running_entries += 1
            last_ms = int(
                record.get("lastEventAt")
                or record.get("startedAt")
                or record.get("updatedAt")
                or 0
            )
            if last_ms > cutoff_ms:
                continue
            if session_key in active_task_session_keys:
                continue

            marked_idle += 1
            changed = True
            record["abortedLastRun"] = True
            record["updatedAt"] = now_ms
            record.pop("status", None)
            record.pop("startedAt", None)
            record.pop("lastEventAt", None)

        if changed and not dry_run:
            store_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    return len(store_paths), running_entries, marked_idle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--openclaw-home",
        default=os.environ.get("OPENCLAW_HOME", str(Path.home() / ".openclaw")),
        help="OpenClaw home directory (default: ~/.openclaw)",
    )
    parser.add_argument(
        "--grace-seconds",
        type=int,
        default=420,
        help="Only reconcile running records older than this threshold.",
    )
    parser.add_argument(
        "--reason",
        default="runtime stale task reconciliation",
        help="Reason string written into reconciled task rows.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run reconciliation even when OpenClaw processes are active.",
    )
    args = parser.parse_args()

    openclaw_home = Path(args.openclaw_home).expanduser()
    db_path = openclaw_home / "tasks" / "runs.sqlite"
    active_pids = _active_runtime_pids()
    skip = bool(active_pids) and not args.force

    db_backup = None
    candidates = 0
    marked = 0
    duplicate_cron_marked = 0
    store_count = 0
    running_entries = 0
    sessions_marked = 0
    if not skip:
        db_backup = _backup_db(db_path, args.dry_run)
        candidates, marked = _reconcile_running_tasks(
            db_path=db_path,
            grace_seconds=args.grace_seconds,
            dry_run=args.dry_run,
            reason=args.reason,
        )
        duplicate_cron_marked = _reconcile_duplicate_running_cron_tasks(
            db_path=db_path,
            dry_run=args.dry_run,
            reason=f"{args.reason} (duplicate cron run)",
        )
        store_count, running_entries, sessions_marked = _reconcile_running_sessions(
            openclaw_home=openclaw_home,
            db_path=db_path,
            grace_seconds=args.grace_seconds,
            dry_run=args.dry_run,
        )

    locks_seen, locks_removed = _cleanup_orphan_locks(
        openclaw_home=openclaw_home,
        dry_run=args.dry_run,
    )

    summary = ReconcileSummary(
        db_exists=db_path.exists(),
        db_backed_up_to=db_backup,
        running_candidates=candidates,
        tasks_marked_lost=marked + duplicate_cron_marked,
        duplicate_cron_marked_lost=duplicate_cron_marked,
        session_files_seen=store_count,
        session_entries_running=running_entries,
        sessions_marked_idle=sessions_marked,
        lock_files_seen=locks_seen,
        lock_files_removed=locks_removed,
        skipped_due_to_active_runtime=skip,
        active_runtime_pids=active_pids,
    )
    print(json.dumps(summary.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
