#!/usr/bin/env bash
set -euo pipefail

# Collect a reproducible operational metrics bundle for OpenClaw.
# This script intentionally captures both raw command outputs and summarized metrics.

ROOT_DIR="/Users/louishyman/openclaw"
OUT_BASE="$ROOT_DIR/runtime_metrics"
STAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
OUT_DIR="$OUT_BASE/$STAMP"
LATEST_LINK="$OUT_BASE/latest"

mkdir -p "$OUT_DIR"

run_and_capture() {
  local name="$1"
  shift
  local outfile="$OUT_DIR/$name"
  "$@" >"$outfile" 2>&1 || true
}

run_and_capture "models-status.json" openclaw models status --json
run_and_capture "channels-probe.txt" openclaw channels status --probe
run_and_capture "health-verbose.txt" openclaw health --verbose
run_and_capture "cron-list.json" openclaw cron list --json
run_and_capture "doctor.txt" openclaw doctor
run_and_capture "security-audit.json" openclaw security audit --deep --json
run_and_capture "otto-suite-quick.txt" "$ROOT_DIR/scripts/test_otto_suite.sh" --quick

python3 - <<'PY' >"$OUT_DIR/agent-latency.json"
import json
import subprocess
import time

def to_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)

tests = [
    ("polly", 90, ["openclaw", "agent", "--agent", "polly", "--timeout", "60",
                   "--message", "Return one line: HEARTBEAT_OK with current ingest mode."]),
    ("rex", 120, ["openclaw", "agent", "--agent", "rex", "--timeout", "90",
                  "--message", "Return compact status from state/connections-sync-last.json: account, pages_read, messages_scanned, unique_senders, inserted, updated, total_connections, cycle_complete."]),
    ("maxwell", 120, ["openclaw", "agent", "--agent", "maxwell", "--timeout", "90",
                      "--message", "Return compact status from memory/gmail-backfill-12m-checkpoint.json: pages_processed, complete, next_page_token, account, query. Do not run new sweep."]),
]

results = []
for agent, hard_timeout_seconds, cmd in tests:
    start = time.time()
    timed_out = False
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=hard_timeout_seconds)
        return_code = cp.returncode
        out = (cp.stdout or "") + (cp.stderr or "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return_code = 124
        out = (
            to_text(exc.stdout)
            + to_text(exc.stderr)
            + "\n[collector-timeout] hard timeout exceeded"
        ).strip()
    latency = round(time.time() - start, 3)
    results.append(
        {
            "agent": agent,
            "return_code": return_code,
            "timed_out": timed_out,
            "latency_seconds": latency,
            "output_excerpt": out.strip()[:1200],
        }
    )

print(json.dumps({"tests": results}, indent=2))
PY

python3 - <<'PY' >"$OUT_DIR/summary.json"
import json
import pathlib
import re

out_dir = pathlib.Path("/Users/louishyman/openclaw/runtime_metrics") / pathlib.Path().cwd().name
# We cannot rely on cwd name inside heredoc in a portable way; infer latest timestamp dir.
base = pathlib.Path("/Users/louishyman/openclaw/runtime_metrics")
dirs = sorted([p for p in base.iterdir() if p.is_dir() and p.name != "latest"])
target = dirs[-1]

def read_text(name):
    p = target / name
    return p.read_text() if p.exists() else ""

def read_json(name):
    p = target / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}

channels_txt = read_text("channels-probe.txt")
otto_txt = read_text("otto-suite-quick.txt")
doctor_txt = read_text("doctor.txt")

channels_working = len(re.findall(r"enabled, configured, running.*works", channels_txt))

otto_pass = re.search(r"PASS:\s+(\d+)", otto_txt)
otto_warn = re.search(r"WARN:\s+(\d+)", otto_txt)
otto_fail = re.search(r"FAIL:\s+(\d+)", otto_txt)

security = read_json("security-audit.json")
cron = read_json("cron-list.json")
models = read_json("models-status.json")
lat = read_json("agent-latency.json")

summary = {
    "generated_at_utc": target.name,
    "models": {
        "default_model": models.get("defaultModel"),
        "fallbacks": models.get("fallbacks", []),
    },
    "channels": {
        "telegram_working_count": channels_working,
    },
    "cron": {
        "total_jobs": cron.get("total"),
        "enabled_jobs": sum(1 for j in cron.get("jobs", []) if j.get("enabled")),
    },
    "security_audit": security.get("summary", {}),
    "otto_suite_quick": {
        "pass": int(otto_pass.group(1)) if otto_pass else None,
        "warn": int(otto_warn.group(1)) if otto_warn else None,
        "fail": int(otto_fail.group(1)) if otto_fail else None,
    },
    "agent_latency": lat.get("tests", []),
    "doctor_excerpt": doctor_txt.splitlines()[:40],
}

print(json.dumps(summary, indent=2))
PY

python3 - <<'PY'
import json
import pathlib

base = pathlib.Path("/Users/louishyman/openclaw/runtime_metrics")
dirs = sorted([p for p in base.iterdir() if p.is_dir() and p.name != "latest"])
target = dirs[-1]
summary = json.loads((target / "summary.json").read_text())

lines = []
lines.append("# OpenClaw Feature Metrics")
lines.append("")
lines.append(f"- Generated UTC: `{summary.get('generated_at_utc')}`")
lines.append(f"- Default model: `{summary['models'].get('default_model')}`")
lines.append(f"- Fallbacks: `{', '.join(summary['models'].get('fallbacks', [])) or '-'}`")
lines.append(f"- Telegram channels working: `{summary['channels'].get('telegram_working_count')}`")
lines.append(f"- Cron jobs enabled/total: `{summary['cron'].get('enabled_jobs')}/{summary['cron'].get('total_jobs')}`")
sa = summary.get("security_audit", {})
lines.append(f"- Security audit (critical/warn/info): `{sa.get('critical', 0)}/{sa.get('warn', 0)}/{sa.get('info', 0)}`")
otto = summary.get("otto_suite_quick", {})
lines.append(f"- Otto suite quick (pass/warn/fail): `{otto.get('pass')}/{otto.get('warn')}/{otto.get('fail')}`")
lines.append("")
lines.append("## Agent Latency")
lines.append("")
lines.append("| Agent | Return Code | Latency (s) |")
lines.append("|---|---:|---:|")
for row in summary.get("agent_latency", []):
    lines.append(f"| {row.get('agent')} | {row.get('return_code')} | {row.get('latency_seconds')} |")
lines.append("")
lines.append("Raw outputs are stored next to this report in the same timestamp directory.")

(target / "summary.md").write_text("\n".join(lines) + "\n")
print(target / "summary.md")
PY

rm -f "$LATEST_LINK"
ln -s "$OUT_DIR" "$LATEST_LINK"

echo "Metrics bundle written to: $OUT_DIR"
echo "Latest symlink: $LATEST_LINK"
