#!/usr/bin/env python3
import csv
import sys
from pathlib import Path

LOWER_DRIVE_IDS = {"rootfs", "workspace"}


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


def iter_blocks(trace_path, block_size):
    seen = set()
    for line in Path(trace_path).read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split(",")
        if len(parts) != 5:
            continue
        _timestamp_ns, drive_id, op, offset, length = parts
        if op != "read":
            continue
        if drive_id not in LOWER_DRIVE_IDS:
            continue
        try:
            offset = int(offset)
            length = int(length)
        except ValueError:
            continue
        if length <= 0:
            continue
        start = offset // block_size
        end = (offset + length - 1) // block_size
        for block_id in range(start, end + 1):
            key = (drive_id, block_id)
            if key in seen:
                continue
            seen.add(key)
            yield drive_id, block_id


def main():
    if len(sys.argv) not in (6, 8):
        print(
            "usage: collect_first_read_blocks.py TRACE RUN_ID TASK_ID OUT_CSV|- BLOCK_SIZE [PHASED_OUT_CSV PHASE]",
            file=sys.stderr,
        )
        return 2

    trace_path = Path(sys.argv[1])
    run_id = sys.argv[2]
    task_id = sys.argv[3]
    out_csv_arg = sys.argv[4]
    out_csv = None if out_csv_arg == "-" else Path(out_csv_arg)
    block_size = int(sys.argv[5])
    phased_out = Path(sys.argv[6]) if len(sys.argv) == 8 else None
    phase = sys.argv[7] if len(sys.argv) == 8 else None

    rows = [
        {
            "run_id": run_id,
            "task_id": task_id,
            "drive_id": drive_id,
            "op": "read",
            "offset": str(block_id * block_size),
            "length": str(block_size),
        }
        for drive_id, block_id in iter_blocks(trace_path, block_size)
    ]
    if out_csv is not None:
        append_rows(out_csv, ["run_id", "task_id", "drive_id", "op", "offset", "length"], rows)

    if phased_out is not None and phase is not None:
        phased_rows = []
        for row in rows:
            phased_rows.append(
                {
                    "run_id": row["run_id"],
                    "task_id": row["task_id"],
                    "phase": phase,
                    "drive_id": row["drive_id"],
                    "op": row["op"],
                    "offset": row["offset"],
                    "length": row["length"],
                }
            )
        append_rows(
            phased_out,
            ["run_id", "task_id", "phase", "drive_id", "op", "offset", "length"],
            phased_rows,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
