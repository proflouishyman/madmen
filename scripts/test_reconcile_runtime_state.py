#!/usr/bin/env python3
"""Focused tests for runtime reconciliation edge cases."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path


def _load_module():
    module_path = Path(__file__).with_name("reconcile_runtime_state.py")
    spec = importlib.util.spec_from_file_location("reconcile_runtime_state", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Dataclass decoration expects the module to be discoverable in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RECONCILE = _load_module()


def _init_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE task_runs (
              task_id TEXT PRIMARY KEY,
              runtime TEXT,
              source_id TEXT,
              status TEXT,
              created_at INTEGER,
              started_at INTEGER,
              last_event_at INTEGER,
              ended_at INTEGER,
              error TEXT,
              terminal_outcome TEXT,
              cleanup_after INTEGER
            )
            """
        )
        conn.commit()


class ReconcileRuntimeStateTests(unittest.TestCase):
    def test_running_row_with_ended_at_is_reconciled_immediately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runs.sqlite"
            _init_db(db_path)
            now_ms = int(time.time() * 1000)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO task_runs (
                      task_id, runtime, status, created_at, started_at, last_event_at, ended_at, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "t1",
                        "cli",
                        "running",
                        now_ms - 1_000,
                        now_ms - 900,
                        now_ms - 800,
                        now_ms - 700,
                        "timed out",
                    ),
                )
                conn.commit()

            candidates, marked = RECONCILE._reconcile_running_tasks(
                db_path=db_path,
                grace_seconds=3600,
                dry_run=False,
                reason="test reconcile",
            )
            self.assertEqual(candidates, 1)
            self.assertEqual(marked, 1)

            with sqlite3.connect(db_path) as conn:
                status, terminal_outcome = conn.execute(
                    "SELECT status, terminal_outcome FROM task_runs WHERE task_id = 't1'"
                ).fetchone()
            self.assertEqual(status, "lost")
            self.assertEqual(terminal_outcome, "error")

    def test_recent_running_row_without_terminal_markers_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runs.sqlite"
            _init_db(db_path)
            now_ms = int(time.time() * 1000)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO task_runs (
                      task_id, runtime, status, created_at, started_at, last_event_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("t2", "cli", "running", now_ms - 2_000, now_ms - 1_500, now_ms - 500),
                )
                conn.commit()

            candidates, marked = RECONCILE._reconcile_running_tasks(
                db_path=db_path,
                grace_seconds=3600,
                dry_run=False,
                reason="test reconcile",
            )
            self.assertEqual(candidates, 0)
            self.assertEqual(marked, 0)

            with sqlite3.connect(db_path) as conn:
                status = conn.execute("SELECT status FROM task_runs WHERE task_id = 't2'").fetchone()[0]
            self.assertEqual(status, "running")

    def test_duplicate_running_cron_rows_keep_newest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runs.sqlite"
            _init_db(db_path)
            now_ms = int(time.time() * 1000)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO task_runs (
                      task_id, runtime, source_id, status, created_at, started_at, last_event_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("c1", "cron", "job-1", "running", now_ms - 20_000, now_ms - 19_000, now_ms - 18_000),
                )
                conn.execute(
                    """
                    INSERT INTO task_runs (
                      task_id, runtime, source_id, status, created_at, started_at, last_event_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("c2", "cron", "job-1", "running", now_ms - 10_000, now_ms - 9_000, now_ms - 8_000),
                )
                conn.commit()

            marked = RECONCILE._reconcile_duplicate_running_cron_tasks(
                db_path=db_path,
                dry_run=False,
                reason="test duplicate cron",
            )
            self.assertEqual(marked, 1)

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT task_id, status FROM task_runs ORDER BY task_id"
                ).fetchall()
            self.assertEqual(rows, [("c1", "lost"), ("c2", "running")])

    def test_superseded_running_cron_row_is_reconciled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runs.sqlite"
            _init_db(db_path)
            now_ms = int(time.time() * 1000)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO task_runs (
                      task_id, runtime, source_id, status, created_at, started_at, last_event_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "c3",
                        "cron",
                        "job-2",
                        "running",
                        now_ms - 20_000,
                        now_ms - 19_000,
                        now_ms - 18_000,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO task_runs (
                      task_id, runtime, source_id, status, created_at, started_at, ended_at, last_event_at, terminal_outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "c4",
                        "cron",
                        "job-2",
                        "timed_out",
                        now_ms - 1_000,
                        now_ms - 900,
                        now_ms - 100,
                        now_ms - 100,
                        "error",
                    ),
                )
                conn.commit()

            marked = RECONCILE._reconcile_duplicate_running_cron_tasks(
                db_path=db_path,
                dry_run=False,
                reason="test superseded cron",
            )
            self.assertEqual(marked, 1)

            with sqlite3.connect(db_path) as conn:
                status_c3 = conn.execute(
                    "SELECT status FROM task_runs WHERE task_id = 'c3'"
                ).fetchone()[0]
                status_c4 = conn.execute(
                    "SELECT status FROM task_runs WHERE task_id = 'c4'"
                ).fetchone()[0]
            self.assertEqual(status_c3, "lost")
            self.assertEqual(status_c4, "timed_out")


if __name__ == "__main__":
    unittest.main()
