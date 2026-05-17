#!/usr/bin/env python3
import sys

from analyze_common import (
    PAIR_FIELDS,
    SAME_TASK_SCALE_FIELDS,
    SUMMARY_DIR,
    build_same_task_outputs,
    ensure_summary_dir,
    load_metric_data,
    write_csv,
)


def main():
    if len(sys.argv) not in (1, 2):
        print("usage: analyze.py [BLOCK_SIZE]", file=sys.stderr)
        return 2

    block_size = int(sys.argv[1]) if len(sys.argv) == 2 else 4096
    metrics = load_metric_data(block_size)
    scale_rows, pair_rows = build_same_task_outputs(metrics)

    ensure_summary_dir()
    write_csv(SUMMARY_DIR / "same_task_scale.csv", SAME_TASK_SCALE_FIELDS, scale_rows)
    write_csv(SUMMARY_DIR / "same_task_run_pairs.csv", PAIR_FIELDS, pair_rows)

    print(f"wrote {SUMMARY_DIR / 'same_task_scale.csv'}")
    print(f"wrote {SUMMARY_DIR / 'same_task_run_pairs.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
