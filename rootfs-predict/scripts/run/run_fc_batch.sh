#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 4 ]; then
    echo "usage: run_fc_batch.sh TASK_FILE REPEAT_COUNT [JOBS] [START_RUN]" >&2
    echo "       FC_BATCH_JOBS can also set JOBS" >&2
    echo "       FC_BATCH_START_RUN can also set START_RUN" >&2
    exit 2
fi

TASK_FILE="$1"
REPEAT_COUNT="$2"
JOBS="${3:-${FC_BATCH_JOBS:-1}}"
START_RUN="${4:-${FC_BATCH_START_RUN:-1}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "$REPEAT_COUNT" in
    ""|*[!0-9]*)
        echo "REPEAT_COUNT must be a positive integer" >&2
        exit 2
        ;;
esac
case "$JOBS" in
    ""|*[!0-9]*)
        echo "JOBS must be a positive integer" >&2
        exit 2
        ;;
esac
case "$START_RUN" in
    ""|*[!0-9]*)
        echo "START_RUN must be a positive integer" >&2
        exit 2
        ;;
esac
REPEAT_COUNT=$((10#$REPEAT_COUNT))
JOBS=$((10#$JOBS))
START_RUN=$((10#$START_RUN))
if [ "$REPEAT_COUNT" -lt 1 ]; then
    echo "REPEAT_COUNT must be a positive integer" >&2
    exit 2
fi
if [ "$JOBS" -lt 1 ]; then
    echo "JOBS must be a positive integer" >&2
    exit 2
fi
if [ "$START_RUN" -lt 1 ]; then
    echo "START_RUN must be a positive integer" >&2
    exit 2
fi

TASKS=()
while IFS= read -r INSTANCE_ID || [ -n "$INSTANCE_ID" ]; do
    if [ -z "$INSTANCE_ID" ] || [[ "$INSTANCE_ID" == \#* ]]; then
        continue
    fi
    TASKS+=("$INSTANCE_ID")
done < "$TASK_FILE"

TOTAL_TASKS="${#TASKS[@]}"
TOTAL_RUNS=$((TOTAL_TASKS * REPEAT_COUNT))
if [ "$TOTAL_TASKS" -eq 0 ]; then
    echo "no tasks to run" >&2
    exit 0
fi

run_task_group() {
    local INSTANCE_ID="$1"
    local TASK_INDEX="$2"
    local i
    local RUN_INDEX
    local RUN_ID_NUMBER
    local RUN_ID

    for i in $(seq 1 "$REPEAT_COUNT"); do
        RUN_INDEX=$(((TASK_INDEX - 1) * REPEAT_COUNT + i))
        RUN_ID_NUMBER=$((START_RUN + i - 1))
        RUN_ID="run-$RUN_ID_NUMBER"
        printf '\n[%s] run %s/%s task %s/%s instance=%s repeat=%s/%s run-id=%s \n' \
            "$(date '+%Y-%m-%d %H:%M:%S')" \
            "$RUN_INDEX" "$TOTAL_RUNS" \
            "$TASK_INDEX" "$TOTAL_TASKS" \
            "$INSTANCE_ID" "$i" "$REPEAT_COUNT" "$RUN_ID" >&2
        "$ROOT/scripts/run/run_fc_task.py" "$INSTANCE_ID" "$RUN_ID" </dev/null
    done
}

ACTIVE=0
FAILURES=0

cleanup_jobs() {
    local pids
    pids="$(jobs -pr)"
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
    fi
}

trap cleanup_jobs INT TERM EXIT

wait_for_one() {
    local status
    set +e
    wait -n
    status=$?
    set -e
    ACTIVE=$((ACTIVE - 1))
    if [ "$status" -ne 0 ]; then
        FAILURES=$((FAILURES + 1))
    fi
}

for index in "${!TASKS[@]}"; do
    if [ "$FAILURES" -ne 0 ]; then
        break
    fi

    run_task_group "${TASKS[$index]}" "$((index + 1))" &
    ACTIVE=$((ACTIVE + 1))

    if [ "$ACTIVE" -ge "$JOBS" ]; then
        wait_for_one
    fi
done

while [ "$ACTIVE" -gt 0 ]; do
    wait_for_one
done

if [ "$FAILURES" -ne 0 ]; then
    echo "batch failed: $FAILURES worker(s) returned non-zero" >&2
    exit 1
fi

trap - INT TERM EXIT
