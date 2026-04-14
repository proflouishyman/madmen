#!/usr/bin/env bash
# install_ingest_launchd.sh
#
# Installs (or uninstalls) the polly-ingest and maxwell-ingest launchd agents.
# These replace the ingestion-watch-20m and maxwell-ingest-30m OpenClaw cron
# jobs, which previously routed script execution through an LLM agent turn.
#
# Running the ingest scripts directly via launchd eliminates:
#   - LLM inference overhead and model hallucination risk on exec parameters
#   - Gateway event loop contention during every ingest cycle
#   - Session lock accumulation from isolated cron sessions
#   - exec-parameter generation failures (security:allowlist, elevated, etc.)
#
# Usage:
#   bash install_ingest_launchd.sh          # install and load both agents
#   bash install_ingest_launchd.sh --uninstall  # unload and remove both agents
#   bash install_ingest_launchd.sh --status     # show current status
#
# Prerequisites:
#   - Run on the Mac as the user who owns ~/.openclaw (louishyman)
#   - OpenClaw gateway does NOT need to be stopped — these agents are independent
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHD_DIR="${SCRIPT_DIR}/../launchd"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
LOG_DIR="${HOME}/.openclaw/workspaces/backer-workspace/logs"

AGENTS=(
  "com.openclaw.polly-ingest"
  "com.openclaw.maxwell-ingest"
)

# ── helpers ──────────────────────────────────────────────────────────────────

ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m⚠\033[0m  %s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$*"; exit 1; }

is_loaded() {
  local label="$1"
  launchctl list "${label}" >/dev/null 2>&1
}

# ── mode selection ────────────────────────────────────────────────────────────

MODE="install"
for arg in "$@"; do
  case "${arg}" in
    --uninstall) MODE="uninstall" ;;
    --status)    MODE="status" ;;
    --help|-h)
      echo "Usage: $0 [--install|--uninstall|--status]"
      exit 0
      ;;
  esac
done

# ── status ────────────────────────────────────────────────────────────────────

if [[ "${MODE}" == "status" ]]; then
  echo "=== OpenClaw ingest launchd agent status ==="
  for label in "${AGENTS[@]}"; do
    plist="${LAUNCH_AGENTS_DIR}/${label}.plist"
    if [[ -f "${plist}" ]]; then
      if is_loaded "${label}"; then
        pid_line="$(launchctl list "${label}" 2>/dev/null | grep '"PID"' || true)"
        ok "${label}: loaded${pid_line:+ ($pid_line)}"
      else
        warn "${label}: plist installed but NOT loaded"
      fi
    else
      warn "${label}: plist NOT installed (${plist})"
    fi
  done
  echo ""
  echo "=== Log locations ==="
  info "polly-ingest:   ${LOG_DIR}/polly-ingest.log"
  info "maxwell-ingest: ${LOG_DIR}/maxwell-ingest.log"
  exit 0
fi

# ── uninstall ─────────────────────────────────────────────────────────────────

if [[ "${MODE}" == "uninstall" ]]; then
  echo "=== Uninstalling OpenClaw ingest launchd agents ==="
  for label in "${AGENTS[@]}"; do
    plist="${LAUNCH_AGENTS_DIR}/${label}.plist"
    if is_loaded "${label}"; then
      launchctl bootout "gui/${UID_NUM}/${label}" >/dev/null 2>&1 || \
        launchctl unload "${plist}" >/dev/null 2>&1 || true
      ok "Unloaded: ${label}"
    else
      info "Not loaded (skip): ${label}"
    fi
    if [[ -f "${plist}" ]]; then
      rm "${plist}"
      ok "Removed plist: ${plist}"
    fi
  done
  echo ""
  warn "The OpenClaw cron jobs ingestion-watch-20m and maxwell-ingest-30m"
  warn "are still disabled in jobs.json. Re-enable them if needed:"
  info "  ~/.openclaw/cron/jobs.json → set \"enabled\": true for each job"
  exit 0
fi

# ── install ───────────────────────────────────────────────────────────────────

echo "=== Installing OpenClaw ingest launchd agents ==="

# Pre-flight: verify the scripts exist
POLLY_INGEST="${SCRIPT_DIR}/polly_ingest.py"
MAXWELL_INGEST="${SCRIPT_DIR}/maxwell_ingest.py"

[[ -f "${POLLY_INGEST}" ]]   || fail "polly_ingest.py not found at ${POLLY_INGEST}"
[[ -f "${MAXWELL_INGEST}" ]] || fail "maxwell_ingest.py not found at ${MAXWELL_INGEST}"
ok "Scripts verified"

# Pre-flight: verify python3 is at /usr/bin/python3
[[ -x "/usr/bin/python3" ]] || fail "/usr/bin/python3 not found — check PATH in plist"
ok "python3 at /usr/bin/python3"

# Create log directory
mkdir -p "${LOG_DIR}"
ok "Log directory: ${LOG_DIR}"

# Install each plist
for label in "${AGENTS[@]}"; do
  src="${LAUNCHD_DIR}/${label}.plist"
  dst="${LAUNCH_AGENTS_DIR}/${label}.plist"

  [[ -f "${src}" ]] || fail "Plist not found: ${src}"

  # Unload first if already running (allows re-install)
  if is_loaded "${label}"; then
    launchctl bootout "gui/${UID_NUM}/${label}" >/dev/null 2>&1 || \
      launchctl unload "${dst}" >/dev/null 2>&1 || true
    info "Unloaded existing: ${label}"
  fi

  cp "${src}" "${dst}"
  ok "Copied: ${dst}"

  launchctl bootstrap "gui/${UID_NUM}" "${dst}"
  ok "Loaded: ${label}"
done

echo ""
echo "=== Verification ==="
for label in "${AGENTS[@]}"; do
  if is_loaded "${label}"; then
    ok "${label}: running"
  else
    fail "${label}: failed to load — check ~/Library/Logs/com.apple.xpc.launchd/ for errors"
  fi
done

echo ""
ok "Installation complete."
info ""
info "The OpenClaw cron jobs ingestion-watch-20m and maxwell-ingest-30m have"
info "been disabled in jobs.json — they are now replaced by these launchd agents."
info ""
info "Logs:"
info "  polly-ingest:   ${LOG_DIR}/polly-ingest.log"
info "  maxwell-ingest: ${LOG_DIR}/maxwell-ingest.log"
info ""
info "Both agents run silently in the background. The gateway event loop is no"
info "longer involved in data ingestion."
