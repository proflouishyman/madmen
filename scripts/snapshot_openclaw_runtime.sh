#!/usr/bin/env bash
set -euo pipefail

# Copy essential OpenClaw runtime artifacts into this repo for versioned backup.
# Default mode is secret-safe and excludes high-risk/private raw content.
#
# Usage:
#   scripts/snapshot_openclaw_runtime.sh
#   scripts/snapshot_openclaw_runtime.sh --include-sensitive

ROOT_DIR="/Users/louishyman/openclaw"
SRC_DIR="$HOME/.openclaw"
DST_BASE="$ROOT_DIR/runtime_snapshots"
STAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
DST_DIR="$DST_BASE/$STAMP"
LATEST_LINK="$DST_BASE/latest"
INCLUDE_SENSITIVE=0

for arg in "$@"; do
  case "$arg" in
    --include-sensitive)
      INCLUDE_SENSITIVE=1
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$DST_DIR"

# Capture operational command outputs first.
openclaw models status --json >"$DST_DIR/models-status.json" 2>&1 || true
openclaw cron list --json >"$DST_DIR/cron-list.json" 2>&1 || true
openclaw channels status --probe >"$DST_DIR/channels-probe.txt" 2>&1 || true
openclaw health --verbose >"$DST_DIR/health-verbose.txt" 2>&1 || true
openclaw security audit --deep --json >"$DST_DIR/security-audit.json" 2>&1 || true

# Redact secrets from openclaw.json before persisting to repo.
python3 - <<'PY'
import json
import pathlib
import re

src = pathlib.Path.home() / ".openclaw" / "openclaw.json"
dst = pathlib.Path("/Users/louishyman/openclaw/runtime_snapshots")
dirs = sorted([p for p in dst.iterdir() if p.is_dir() and p.name != "latest"])
target = dirs[-1] / "openclaw.redacted.json"

if not src.exists():
    target.write_text("{}\n")
    raise SystemExit(0)

data = json.loads(src.read_text())
secret_key = re.compile(r"(token|secret|password|api[_-]?key|oauth|auth)", re.IGNORECASE)

def scrub(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if secret_key.search(str(k)):
                out[k] = "__REDACTED__"
            else:
                out[k] = scrub(v)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj

target.write_text(json.dumps(scrub(data), indent=2) + "\n")
PY

# Copy role/contracts that are useful for debugging behavior regressions.
mkdir -p "$DST_DIR/workspaces"
for agent in worf forge polly maxwell otto finn prof weber uhura emma john balt lex spark trip rex; do
  src_ws="$SRC_DIR/workspaces/${agent}-workspace"
  dst_ws="$DST_DIR/workspaces/${agent}-workspace"
  mkdir -p "$dst_ws"
  for f in IDENTITY.md SOUL.md TOOLS.md MEMORY.md USER.md; do
    if [[ -f "$src_ws/$f" ]]; then
      cp "$src_ws/$f" "$dst_ws/$f"
    fi
  done
done

# Include selected low-risk operational state by default.
mkdir -p "$DST_DIR/state"
for p in \
  "$SRC_DIR/workspaces/rex-workspace/state/connections-sync-last.json" \
  "$SRC_DIR/workspaces/rex-workspace/state/rex_sync_checkpoint_365d.json" \
  "$SRC_DIR/workspaces/maxwell-workspace/memory/gmail-backfill-12m-state.json"; do
  if [[ -f "$p" ]]; then
    cp "$p" "$DST_DIR/state/$(basename "$p")"
  fi
done

# Optional sensitive sync for private/offline-only backups.
if [[ "$INCLUDE_SENSITIVE" -eq 1 ]]; then
  mkdir -p "$DST_DIR/sensitive"
  if [[ -d "$SRC_DIR/workspaces/maxwell-workspace/memory" ]]; then
    cp -R "$SRC_DIR/workspaces/maxwell-workspace/memory" "$DST_DIR/sensitive/maxwell-memory"
  fi
  if [[ -d "$SRC_DIR/workspaces/polly-workspace/memory" ]]; then
    cp -R "$SRC_DIR/workspaces/polly-workspace/memory" "$DST_DIR/sensitive/polly-memory"
  fi
  if [[ -f "$SRC_DIR/workspaces/rex-workspace/connections.db" ]]; then
    cp "$SRC_DIR/workspaces/rex-workspace/connections.db" "$DST_DIR/sensitive/connections.db"
  fi
fi

# Create checksums for auditability.
(
  cd "$DST_DIR"
  find . -type f -print0 | sort -z | xargs -0 shasum -a 256 > manifest.sha256
)

rm -f "$LATEST_LINK"
ln -s "$DST_DIR" "$LATEST_LINK"

echo "Snapshot written to: $DST_DIR"
echo "Latest symlink: $LATEST_LINK"
if [[ "$INCLUDE_SENSITIVE" -eq 1 ]]; then
  echo "Sensitive data included: yes"
else
  echo "Sensitive data included: no"
fi
