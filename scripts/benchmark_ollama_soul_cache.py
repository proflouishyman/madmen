#!/usr/bin/env python3
"""
Benchmark repeated OpenClaw turns for SOUL-heavy prompts to estimate warm-cache impact.

Usage:
  python3 scripts/benchmark_ollama_soul_cache.py \
    --agent polly \
    --session-id ollama-kv-bench \
    --turns 8 \
    --warmup 1 \
    --message "Summarize yesterday's top 3 priorities in 3 bullets."
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TurnResult:
    turn: int
    ok: bool
    latency_s: float
    timeout: bool
    rc: int
    stderr_excerpt: str
    stdout_excerpt: str


def run_turn(agent: str, session_id: str, message: str, timeout_seconds: int) -> TurnResult:
    cmd = [
        "openclaw",
        "agent",
        "--agent",
        agent,
        "--session-id",
        session_id,
        "--message",
        message,
        "--json",
        "--thinking",
        "off",
    ]
    start = time.perf_counter()
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        latency = time.perf_counter() - start
        ok = completed.returncode == 0
        return TurnResult(
            turn=-1,
            ok=ok,
            latency_s=latency,
            timeout=False,
            rc=completed.returncode,
            stderr_excerpt=(completed.stderr or "")[:1200],
            stdout_excerpt=(completed.stdout or "")[:1200],
        )
    except subprocess.TimeoutExpired as exc:
        latency = time.perf_counter() - start
        stdout_excerpt = ""
        stderr_excerpt = ""
        if exc.stdout:
            stdout_excerpt = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else exc.stdout
            )[:1200]
        if exc.stderr:
            stderr_excerpt = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else exc.stderr
            )[:1200]
        return TurnResult(
            turn=-1,
            ok=False,
            latency_s=latency,
            timeout=True,
            rc=124,
            stderr_excerpt=stderr_excerpt,
            stdout_excerpt=stdout_excerpt,
        )


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="polly")
    parser.add_argument("--session-id", default="ollama-kv-bench")
    parser.add_argument("--message", required=True)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--turns", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument(
        "--out",
        default="/Users/louishyman/openclaw/runtime_metrics/ollama-kv-benchmark-results.json",
    )
    return parser.parse_args()


def summarize(results: list[TurnResult]) -> dict[str, Any]:
    latencies = [r.latency_s for r in results if r.ok]
    timeout_count = sum(1 for r in results if r.timeout)
    failure_count = sum(1 for r in results if not r.ok)
    total = len(results)

    return {
        "count": total,
        "ok": total - failure_count,
        "failed": failure_count,
        "timeouts": timeout_count,
        "timeout_rate": (timeout_count / total) if total else 0.0,
        "latency": {
            "p50": percentile(latencies, 50) if latencies else None,
            "p95": percentile(latencies, 95) if latencies else None,
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "mean": statistics.mean(latencies) if latencies else None,
        },
    }


def main() -> None:
    args = parse_args()

    warmup_results: list[TurnResult] = []
    measured_results: list[TurnResult] = []

    for idx in range(args.warmup):
        result = run_turn(args.agent, args.session_id, args.message, args.timeout_seconds)
        result.turn = idx + 1
        warmup_results.append(result)
        print(f"warmup {result.turn}/{args.warmup}: ok={result.ok} latency={result.latency_s:.3f}s")

    for idx in range(args.turns):
        result = run_turn(args.agent, args.session_id, args.message, args.timeout_seconds)
        result.turn = idx + 1
        measured_results.append(result)
        print(f"turn {result.turn}/{args.turns}: ok={result.ok} latency={result.latency_s:.3f}s")

    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "agent": args.agent,
        "session_id": args.session_id,
        "message": args.message,
        "timeout_seconds": args.timeout_seconds,
        "warmup": {
            "count": args.warmup,
            "summary": summarize(warmup_results),
            "turns": [asdict(r) for r in warmup_results],
        },
        "measured": {
            "count": args.turns,
            "summary": summarize(measured_results),
            "turns": [asdict(r) for r in measured_results],
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"saved: {out_path}")


if __name__ == "__main__":
    main()
