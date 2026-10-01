#!/bin/bash
# Runs R:soul in a loop inside Docker: one run, then wait SCRIPT_INTERVAL seconds (default 300).
# Runs never overlap because each one finishes before the wait starts.

INTERVAL=${SCRIPT_INTERVAL:-300}
case "$INTERVAL" in
    ''|*[!0-9]*) echo "SCRIPT_INTERVAL must be a whole number of seconds, got '$INTERVAL'" >&2; exit 2 ;;
esac
if [ "$INTERVAL" -lt 1 ]; then
    echo "SCRIPT_INTERVAL must be at least 1" >&2
    exit 2
fi

# Stop promptly on "docker stop": pass the signal to the running R:soul (which keeps its
# saved state for the next start) instead of waiting for Docker to kill the container
child=""
stop() {
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null
        wait "$child" 2>/dev/null
    fi
    exit 143
}
trap stop TERM INT

while true; do
    python -u /app/rsoul.py "$@" &
    child=$!
    wait "$child"
    status=$?
    child=""
    case "$status" in
        0) ;;
        2) echo "R:soul finished, but searching for new books failed (exit status 2); see the log above." >&2 ;;
        *) echo "R:soul run failed with exit status $status; see the log above." >&2 ;;
    esac

    echo "$(date '+%d/%m/%Y %H:%M:%S') - Waiting for $INTERVAL seconds before checking again..."
    sleep "$INTERVAL" &
    child=$!
    wait "$child"
    child=""
done
