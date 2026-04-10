#!/usr/bin/env python3
"""Run one quota-aware Gmail backfill tick for Maxwell.

This script is designed for cron use where we want deterministic, low-context
execution without requiring the LLM to parse large state files.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    """Parse CLI args for one backfill tick."""
    parser = argparse.ArgumentParser(description="Maxwell Gmail backfill tick")
    parser.add_argument("--account", required=True, help="Gmail account")
    parser.add_argument(
        "--query",
        default="newer_than:365d in:inbox",
        help="Gmail search query",
    )
    parser.add_argument(
        "--state-file",
        required=True,
        help="Checkpoint JSON path",
    )
    parser.add_argument(
        "--run-dir",
        default="",
        help="Directory for per-run summary JSON (defaults to state-file parent)",
    )
    parser.add_argument("--max-per-page", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--backoff-base-seconds", type=int, default=180)
    parser.add_argument("--backoff-max-seconds", type=int, default=7200)
    parser.add_argument("--cycle-holdoff-seconds", type=int, default=86400)
    return parser.parse_args()


def now_utc() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Format datetime as stable UTC ISO8601."""
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_or_none(value: str | None) -> datetime | None:
    """Parse an ISO datetime string if present."""
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def load_state(path: Path, account: str, query: str) -> dict[str, Any]:
    """Load checkpoint state; initialize defaults when missing."""
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    now = iso(now_utc())
    return {
        "account": account,
        "query": query,
        "started_at": now,
        "updated_at": now,
        "complete": False,
        "next_page_token": None,
        "pages_processed": 0,
        "total_threads_scanned": 0,
        "consecutive_quota_limits": 0,
        "backoff_seconds": 0,
        "next_allowed_at_utc": None,
        "last_error": None,
    }


def save_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON to disk atomically via temp file replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def is_quota_or_rate_limit(stderr_text: str) -> bool:
    """Best-effort detection for transient quota/rate limits from gog."""
    lowered = stderr_text.lower()
    markers = ["rate", "quota", "too many requests", "429", "resource_exhausted"]
    return any(marker in lowered for marker in markers)


def gog_search(
    account: str,
    query: str,
    max_per_page: int,
    page_token: str | None,
) -> dict[str, Any]:
    """Execute one paginated Gmail search through gog and return JSON."""
    cmd = [
        "gog",
        "-a",
        account,
        "gmail",
        "search",
        query,
        "--max",
        str(max_per_page),
        "-j",
    ]
    if page_token:
        cmd.extend(["--page", page_token])
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if cp.returncode != 0:
        msg = (cp.stderr or cp.stdout or "").strip()
        raise RuntimeError(msg or f"gog exited with code {cp.returncode}")
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from gog: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected gog payload shape (expected object)")
    return data


def main() -> int:
    """Run one checkpointed backfill tick and emit compact JSON summary."""
    args = parse_args()
    state_path = Path(args.state_file).expanduser()
    run_dir = (
        Path(args.run_dir).expanduser()
        if args.run_dir
        else state_path.parent.expanduser()
    )

    state = load_state(state_path, args.account, args.query)
    state["account"] = args.account
    state["query"] = args.query

    now = now_utc()
    next_allowed = parse_iso_or_none(state.get("next_allowed_at_utc"))
    if next_allowed and now < next_allowed:
        summary = {
            "generated_at_utc": iso(now),
            "account": args.account,
            "query": args.query,
            "status": "backoff_wait",
            "quota_limited": True,
            "backoff_seconds": int(state.get("backoff_seconds") or 0),
            "next_allowed_at_utc": state.get("next_allowed_at_utc"),
            "pages_read": 0,
            "threads_scanned": 0,
            "start_page_token": state.get("next_page_token"),
            "next_page_token": state.get("next_page_token"),
            "cycle_complete": bool(state.get("complete")),
        }
        print(json.dumps(summary))
        return 0

    start_token = state.get("next_page_token")
    next_token = start_token
    pages_read = 0
    threads_scanned = 0
    sample_threads: list[dict[str, Any]] = []
    quota_limited = False
    status = "ok"

    try:
        for _ in range(max(1, args.max_pages)):
            result = gog_search(
                account=args.account,
                query=args.query,
                max_per_page=max(1, args.max_per_page),
                page_token=next_token,
            )
            threads = result.get("threads") or []
            if not isinstance(threads, list):
                threads = []

            pages_read += 1
            threads_scanned += len(threads)
            sample_threads.extend(threads[:3])
            if len(sample_threads) > 5:
                sample_threads = sample_threads[:5]

            next_token = result.get("nextPageToken")
            if not next_token:
                break
    except RuntimeError as exc:
        err_text = str(exc)
        if is_quota_or_rate_limit(err_text):
            quota_limited = True
            status = "quota_backoff"
            consecutive = int(state.get("consecutive_quota_limits") or 0) + 1
            backoff = min(
                args.backoff_max_seconds,
                args.backoff_base_seconds * (2 ** max(0, consecutive - 1)),
            )
            next_allowed_at = now + timedelta(seconds=backoff)
            state.update(
                {
                    "updated_at": iso(now),
                    "consecutive_quota_limits": consecutive,
                    "backoff_seconds": backoff,
                    "next_allowed_at_utc": iso(next_allowed_at),
                    "last_error": err_text,
                }
            )
            save_json(state_path, state)
        else:
            state.update(
                {
                    "updated_at": iso(now),
                    "last_error": err_text,
                }
            )
            save_json(state_path, state)
            print(
                json.dumps(
                    {
                        "generated_at_utc": iso(now),
                        "account": args.account,
                        "query": args.query,
                        "status": "error",
                        "error": err_text,
                        "pages_read": pages_read,
                        "threads_scanned": threads_scanned,
                        "start_page_token": start_token,
                        "next_page_token": next_token,
                    }
                )
            )
            return 1

    cycle_complete = bool(not quota_limited and not next_token)
    next_allowed_at_utc = None
    if cycle_complete:
        # Completed traversal; pause before restarting from beginning.
        next_allowed_at_utc = iso(now + timedelta(seconds=args.cycle_holdoff_seconds))

    if cycle_complete:
        next_token = None

    state.update(
        {
            "updated_at": iso(now),
            "complete": cycle_complete,
            "next_page_token": next_token,
            "pages_processed": int(state.get("pages_processed") or 0) + pages_read,
            "total_threads_scanned": int(state.get("total_threads_scanned") or 0)
            + threads_scanned,
            "last_batch_pages": pages_read,
            "last_batch_threads": threads_scanned,
            "consecutive_quota_limits": (
                int(state.get("consecutive_quota_limits") or 0)
                if quota_limited
                else 0
            ),
            "backoff_seconds": (
                int(state.get("backoff_seconds") or 0) if quota_limited else 0
            ),
            "next_allowed_at_utc": (
                state.get("next_allowed_at_utc") if quota_limited else next_allowed_at_utc
            ),
            "last_error": state.get("last_error") if quota_limited else None,
            "sample_threads": sample_threads,
        }
    )
    save_json(state_path, state)

    summary = {
        "generated_at_utc": iso(now),
        "account": args.account,
        "query": args.query,
        "status": status,
        "quota_limited": quota_limited,
        "pages_read": pages_read,
        "threads_scanned": threads_scanned,
        "start_page_token": start_token,
        "next_page_token": next_token,
        "cycle_complete": cycle_complete,
        "consecutive_quota_limits": int(state.get("consecutive_quota_limits") or 0),
        "backoff_seconds": int(state.get("backoff_seconds") or 0),
        "next_allowed_at_utc": state.get("next_allowed_at_utc"),
    }

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    run_path = run_dir / f"gmail-backfill-12m-tick-{stamp}.json"
    save_json(run_path, summary)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
