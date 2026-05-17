#!/usr/bin/env python3
import bisect
import csv
import sys
from collections import defaultdict
from pathlib import Path


def append_rows(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    if exists:
        with path.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle), [])
        if header != fieldnames:
            raise SystemExit(f"{path} has incompatible header: {header}")
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def load_block_indexes(paths):
    grouped = defaultdict(lambda: defaultdict(list))
    for csv_path in paths:
        with Path(csv_path).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                grouped[row["drive_id"]][int(row["block_id"])].append(
                    (
                        row["class"],
                        row.get("path", ""),
                        row.get("inode", ""),
                        row.get("logical_block", ""),
                    )
                )

    indexes = {}
    for drive_id, blocks in grouped.items():
        items = sorted((block_id, entries) for block_id, entries in blocks.items())
        block_ids = [block_id for block_id, _entries in items]
        indexes[drive_id] = (block_ids, items)
    return indexes


def lookup_entries(index, block_id):
    block_ids, items = index
    pos = bisect.bisect_left(block_ids, block_id)
    if pos < 0:
        return []
    if pos < len(items) and items[pos][0] == block_id:
        return items[pos][1]
    return []


def iter_read_blocks(trace_path, block_size):
    for line in Path(trace_path).read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split(",")
        if len(parts) != 5:
            continue
        timestamp_ns, drive_id, op, offset, length = parts
        if op != "read":
            continue
        try:
            timestamp_ns = int(timestamp_ns)
            offset = int(offset)
            length = int(length)
        except ValueError:
            continue
        if length <= 0:
            continue
        start = offset // block_size
        end = (offset + length - 1) // block_size
        for block_id in range(start, end + 1):
            yield timestamp_ns, drive_id, block_id


def main():
    if len(sys.argv) < 9:
        print(
            "usage: collect_file_reads.py TRACE RUN_ID TASK_ID PHASE SUMMARY_CSV BLOCK_CSV UNMAPPED_CSV ATLAS_CSV [ATLAS_CSV ...] [BLOCK_SIZE]",
            file=sys.stderr,
        )
        return 2

    trace_path = Path(sys.argv[1])
    run_id = sys.argv[2]
    task_id = sys.argv[3]
    phase = sys.argv[4]
    summary_csv = Path(sys.argv[5])
    block_csv = Path(sys.argv[6])
    unmapped_csv = Path(sys.argv[7])

    block_size = 4096
    atlas_args = sys.argv[8:]
    if len(atlas_args) >= 2 and atlas_args[-1].isdigit():
        block_size = int(atlas_args[-1])
        atlas_args = atlas_args[:-1]
    block_indexes = load_block_indexes(atlas_args)

    seen_blocks = set()
    summary = {}
    block_rows = []
    unmapped_rows = []
    class_summary = {}

    for timestamp_ns, drive_id, block_id in iter_read_blocks(trace_path, block_size):
        key = (drive_id, block_id)
        if key in seen_blocks:
            continue
        seen_blocks.add(key)

        index = block_indexes.get(drive_id)
        if index is None:
            continue

        entries = lookup_entries(index, block_id)
        if not entries:
            unmapped_rows.append(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "phase": phase,
                    "timestamp_ns": str(timestamp_ns),
                    "drive_id": drive_id,
                    "block_id": str(block_id),
                }
            )
            continue

        class_seen = set()
        for block_class, path, inode, logical_block in entries:
            class_seen.add(block_class)
            block_rows.append(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "phase": phase,
                    "timestamp_ns": str(timestamp_ns),
                    "drive_id": drive_id,
                    "block_id": str(block_id),
                    "class": block_class,
                    "path": path,
                    "inode": inode,
                    "logical_block": logical_block,
                }
            )
            if block_class == "file_data":
                summary_key = (drive_id, path, inode)
                item = summary.get(summary_key)
                if item is None:
                    summary[summary_key] = {
                        "run_id": run_id,
                        "task_id": task_id,
                        "phase": phase,
                        "drive_id": drive_id,
                        "path": path,
                        "inode": inode,
                        "first_ts_ns": timestamp_ns,
                        "blocks_read": 1,
                    }
                else:
                    if timestamp_ns < item["first_ts_ns"]:
                        item["first_ts_ns"] = timestamp_ns
                    item["blocks_read"] += 1

        for block_class in class_seen:
            key = (drive_id, block_class)
            row = class_summary.get(key)
            if row is None:
                class_summary[key] = {
                    "run_id": run_id,
                    "task_id": task_id,
                    "phase": phase,
                    "drive_id": drive_id,
                    "class": block_class,
                    "blocks_read": 1,
                }
            else:
                row["blocks_read"] += 1

    summary_rows = [
        {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "phase": row["phase"],
            "drive_id": row["drive_id"],
            "path": row["path"],
            "inode": row["inode"],
            "first_ts_ns": str(row["first_ts_ns"]),
            "blocks_read": str(row["blocks_read"]),
        }
        for _key, row in sorted(summary.items(), key=lambda item: (item[1]["drive_id"], item[1]["path"], item[1]["inode"]))
    ]

    append_rows(
        summary_csv,
        ["run_id", "task_id", "phase", "drive_id", "path", "inode", "first_ts_ns", "blocks_read"],
        summary_rows,
    )
    append_rows(
        block_csv,
        ["run_id", "task_id", "phase", "timestamp_ns", "drive_id", "block_id", "class", "path", "inode", "logical_block"],
        block_rows,
    )
    append_rows(
        unmapped_csv,
        ["run_id", "task_id", "phase", "timestamp_ns", "drive_id", "block_id"],
        unmapped_rows,
    )
    append_rows(
        summary_csv.with_name("block_class_summary.csv"),
        ["run_id", "task_id", "phase", "drive_id", "class", "blocks_read"],
        [
            {
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "phase": row["phase"],
                "drive_id": row["drive_id"],
                "class": row["class"],
                "blocks_read": str(row["blocks_read"]),
            }
            for _key, row in sorted(class_summary.items(), key=lambda item: (item[1]["drive_id"], item[1]["class"]))
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
