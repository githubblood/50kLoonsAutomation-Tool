#!/usr/bin/env bash
# run.sh — Production loop wrapper for lead-automation
#
# Runs main.py in a continuous loop with a configurable sleep between
# runs.  Designed to be used both directly (non-Docker) and as the
# Docker CMD so SIGTERM causes a clean exit after the current row.
#
# Environment variables:
#   POLL_INTERVAL   Seconds to wait between runs (default: 60)
#   LOG_FILE        Path for tee output (default: logs/automation.log)
#
# Usage (local):   ./run.sh
# Usage (Docker):  CMD ["bash", "run.sh"]

set -uo pipefail

# ── Config ────────────────────────────────────────────────────────────
POLL="${POLL_INTERVAL:-60}"
LOG_FILE="${LOG_FILE:-logs/automation.log}"

# ── Setup ─────────────────────────────────────────────────────────────
cd "$(dirname "$0")"
mkdir -p logs screenshots

# Activate venv if present (local / systemd usage)
if [[ -d "venv/bin" ]]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

_ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "$(_ts) [run.sh] Starting — poll interval=${POLL}s, log=${LOG_FILE}" \
    | tee -a "$LOG_FILE"

# ── SIGTERM / SIGINT handler ──────────────────────────────────────────
# Propagate the signal to the child Python process so it can clean up
# (mark the current row as Retry/Failed), then exit the loop.
_CHILD_PID=""
_stop() {
    echo "$(_ts) [run.sh] Signal received — stopping after current run" \
        | tee -a "$LOG_FILE"
    [[ -n "$_CHILD_PID" ]] && kill -TERM "$_CHILD_PID" 2>/dev/null || true
    exit 0
}
trap _stop SIGTERM SIGINT

# ── Main loop ─────────────────────────────────────────────────────────
while true; do
    # Run main.py; tee duplicates stdout/stderr to the log file
    python main.py 2>&1 | tee -a "$LOG_FILE" &
    _CHILD_PID=$!
    wait "$_CHILD_PID"
    EXIT_CODE=$?
    _CHILD_PID=""

    echo "$(_ts) [run.sh] Exit code=${EXIT_CODE}. Sleeping ${POLL}s…" \
        | tee -a "$LOG_FILE"

    # Sleep in background so the trap fires immediately on signal
    sleep "$POLL" &
    _CHILD_PID=$!
    wait "$_CHILD_PID"
    _CHILD_PID=""
done
