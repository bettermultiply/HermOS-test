#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/run.sh [-t TOTAL] [-c CONCURRENCY] [--no-build] [--no-plot]

Traverse and run all available bench ops.

Options:
  -t, --total TOTAL              Total iterations per op (default: 100)
  -c, --concurrency CONCURRENCY  Concurrency passed to bench (default: TOTAL)
      --no-build                 Skip cargo build --release
      --no-plot                  Skip plotting after all runs
  -h, --help                     Show this help
EOF
}

TOTAL=100
CONCURRENCY=""
OUT_DIR="results"
DO_BUILD=1
DO_PLOT=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--total)
            [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; usage; exit 1; }
            TOTAL="$2"
            shift 2
            ;;
        -c|--concurrency)
            [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; usage; exit 1; }
            CONCURRENCY="$2"
            shift 2
            ;;
        --no-build)
            DO_BUILD=0
            shift
            ;;
        --no-plot)
            DO_PLOT=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if ! [[ "$TOTAL" =~ ^[1-9][0-9]*$ ]]; then
    echo "TOTAL must be a positive integer: $TOTAL" >&2
    exit 1
fi

if [[ -z "$CONCURRENCY" ]]; then
    CONCURRENCY="$TOTAL"
fi

if ! [[ "$CONCURRENCY" =~ ^[1-9][0-9]*$ ]]; then
    echo "CONCURRENCY must be a positive integer: $CONCURRENCY" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

echo "=== fc-restore-bench ==="
echo "total=$TOTAL  concurrency=$CONCURRENCY"
echo ""

if [[ "$DO_BUILD" -eq 1 ]]; then
    cargo build --release 2>&1 | tail -1
fi

mapfile -t OPS < <(
    ./target/release/bench --op help 2>&1 \
        | awk '/Available ops:/{flag=1; next} flag && /^  /{print $1}'
)

if [[ "${#OPS[@]}" -eq 0 ]]; then
    echo "no ops found" >&2
    exit 1
fi

for op in "${OPS[@]}"; do
    echo "Running: $op"
    ./target/release/bench \
        --op "$op" \
        --total "$TOTAL" \
        --concurrency "$CONCURRENCY"
done

if [[ "$DO_PLOT" -eq 1 ]]; then
    echo ""
    echo "=== Plotting ==="
    scripts/plot/bin/python scripts/plot.py "$OUT_DIR"/*.csv -o "$OUT_DIR/results.png"
fi
