#!/usr/bin/env python3
"""
Delete rows from all CSV files in data/ and corresponding runs/ directories.

Usage:
    python scripts/maintenance/delete_data.py --run run-2
    python scripts/maintenance/delete_data.py --task sympy__sympy-12096
    python scripts/maintenance/delete_data.py --run run-1 --task sympy__sympy-12096 --dry-run

--task matches the full task_id exactly.
--run and --task can be combined (AND logic).
"""

import argparse
import os
import sys
import csv
import io
import shutil

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(ROOT_DIR, "data")
RUNS_DIR = os.path.join(ROOT_DIR, "runs")

CSV_FILES = [
    "block_class_summary.csv",
    "block_phased.csv",
    "file_read_blocks.csv",
    "file_read_summary.csv",
    "unmapped_read_blocks.csv",
]


def row_matches(row_run, row_task, run_id, task_id_filter):
    run_match = (run_id is None) or (row_run == run_id)
    task_match = (task_id_filter is None) or (row_task == task_id_filter)
    return run_match and task_match


def delete_csv_rows(run_id, task_id_filter, dry_run):
    print("\n[data/]")
    for filename in CSV_FILES:
        filepath = os.path.join(DATA_DIR, filename)
        if not os.path.exists(filepath):
            print(f"  [skip] {filename} not found")
            continue

        with open(filepath, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)

        kept = []
        deleted = 0
        for row in rows:
            if row_matches(row.get("run_id", ""), row.get("task_id", ""), run_id, task_id_filter):
                deleted += 1
            else:
                kept.append(row)

        print(f"  {filename}: {deleted} rows deleted, {len(kept)} rows kept")

        if not dry_run and deleted > 0:
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(kept)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(buf.getvalue())


def delete_run_dirs(run_id, task_id_filter, dry_run):
    print("\n[runs/]")
    if not os.path.isdir(RUNS_DIR):
        print("  runs/ directory not found, skipping.")
        return

    for task_id in sorted(os.listdir(RUNS_DIR)):
        task_dir = os.path.join(RUNS_DIR, task_id)
        if not os.path.isdir(task_dir):
            continue

        if task_id_filter is not None and task_id != task_id_filter:
            continue

        if run_id is None:
            print(f"  {'[dry]' if dry_run else 'delete'} runs/{task_id}/")
            if not dry_run:
                shutil.rmtree(task_dir)
        else:
            run_dir = os.path.join(task_dir, run_id)
            if os.path.isdir(run_dir):
                print(f"  {'[dry]' if dry_run else 'delete'} runs/{task_id}/{run_id}/")
                if not dry_run:
                    shutil.rmtree(run_dir)
                    if not os.listdir(task_dir):
                        os.rmdir(task_dir)
                        print(f"  (removed empty) runs/{task_id}/")


def main():
    parser = argparse.ArgumentParser(description="Delete data rows and run directories by run/task.")
    parser.add_argument("--run", metavar="RUN_ID", help="run_id to delete (e.g. run-1)")
    parser.add_argument("--task", metavar="TASK_ID", help="exact task_id to delete (e.g. sympy__sympy-12096)")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing changes")
    args = parser.parse_args()

    if not args.run and not args.task:
        print("Error: at least one of --run or --task must be specified.", file=sys.stderr)
        sys.exit(1)

    tag = []
    if args.run:
        tag.append(f"run={args.run}")
    if args.task:
        tag.append(f"task={args.task}")
    mode = "DRY RUN" if args.dry_run else "DELETING"
    print(f"{mode}: {', '.join(tag)}")

    delete_csv_rows(args.run, args.task, args.dry_run)
    delete_run_dirs(args.run, args.task, args.dry_run)

    if args.dry_run:
        print("\nDry run complete. No files were modified.")
    else:
        print("\nDone.")


if __name__ == "__main__":
    main()
