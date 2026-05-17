#!/usr/bin/env python3
import sys

from analyze_common import (
    CROSS_TASK_SUMMARY_FIELDS,
    PAIR_FIELDS,
    SUMMARY_DIR,
    build_cross_task_outputs,
    ensure_summary_dir,
    load_metric_data,
    write_csv,
)


def main():
    if len(sys.argv) not in (1, 2):
        print("usage: all_task_analyze.py [BLOCK_SIZE]", file=sys.stderr)
        return 2

    block_size = int(sys.argv[1]) if len(sys.argv) == 2 else 4096
    metrics = load_metric_data(block_size)
    summary_rows, pair_rows = build_cross_task_outputs(metrics)

    summary_rows = [row for row in summary_rows if row["_relation"] == "other_repo"]
    pair_rows = [row for row in pair_rows if row["_relation"] == "other_repo"]

    ensure_summary_dir()
    write_csv(SUMMARY_DIR / "all_task_summary.csv", CROSS_TASK_SUMMARY_FIELDS, summary_rows)
    write_csv(SUMMARY_DIR / "all_task_run_pairs.csv", PAIR_FIELDS, pair_rows)

    print(f"wrote {SUMMARY_DIR / 'all_task_summary.csv'}")
    print(f"wrote {SUMMARY_DIR / 'all_task_run_pairs.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
