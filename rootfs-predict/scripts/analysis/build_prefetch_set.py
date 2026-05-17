#!/usr/bin/env python3
"""Build tiered prefetch range files from historical block_phased.csv."""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from analyze_common import DATA_DIR, LOWER_DRIVE_IDS, repo_from_task


DRIVE_PRIORITY = {"rootfs": 0, "workspace": 1}
PHASE_PRIORITY = {"preworkload": 0, "workload": 1}


def block_from_row(row, block_size):
    return int(row["offset"]) // block_size


def row_run_key(row):
    return (row["task_id"], row["run_id"])


def read_block_rows(path):
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def percentile(values, percent):
    if not values:
        return sys.maxsize
    ordered = sorted(values)
    index = int((len(ordered) - 1) * percent)
    return ordered[index]


def median(values):
    return percentile(values, 0.5)


def p90(values):
    return percentile(values, 0.9)


def task_matches_scope(task_id, instance_id, scope):
    if scope == "same_task":
        return task_id == instance_id
    if scope == "same_repo":
        return repo_from_task(task_id) == repo_from_task(instance_id)
    if scope == "all":
        return True
    raise ValueError(f"unknown scope: {scope}")


def build_phase_drive_sets(rows, block_size, phase):
    grouped = defaultdict(dict)
    for row in rows:
        if row.get("phase") != phase:
            continue
        drive_id = row.get("drive_id")
        if drive_id not in LOWER_DRIVE_IDS:
            continue
        key = row_run_key(row)
        grouped[key].setdefault(drive_id, []).append(block_from_row(row, block_size))
    return grouped


def warn_if_preworkload_differs(grouped):
    for drive_id in sorted(LOWER_DRIVE_IDS):
        sets = {
            run_key: set(drives.get(drive_id, []))
            for run_key, drives in grouped.items()
            if drives.get(drive_id)
        }
        if len(sets) <= 1:
            continue

        unique_sets = {frozenset(blocks) for blocks in sets.values()}
        if len(unique_sets) == 1:
            continue

        union_size = len(set().union(*sets.values()))
        intersection_size = len(set.intersection(*(set(blocks) for blocks in sets.values())))
        counts = ", ".join(
            f"{task_id}/{run_id}={len(blocks)}"
            for (task_id, run_id), blocks in sorted(sets.items())
        )
        print(
            f"WARNING: preworkload {drive_id} block sets differ; "
            f"using union. union={union_size} intersection={intersection_size} runs=[{counts}]",
            file=sys.stderr,
        )


def block_stats(rows, block_size, instance_id, scope, phase, drive_id):
    matching_rows = [
        row
        for row in rows
        if row.get("phase") == phase
        and row.get("drive_id") == drive_id
        and task_matches_scope(row.get("task_id", ""), instance_id, scope)
    ]
    total_runs = len({row_run_key(row) for row in matching_rows})
    if total_runs == 0:
        return {}

    ranks_by_run = defaultdict(int)
    seen_by_run = defaultdict(set)
    ranks_by_block = defaultdict(list)
    runs_by_block = defaultdict(set)

    for row in matching_rows:
        run_key = row_run_key(row)
        block = block_from_row(row, block_size)
        if block in seen_by_run[run_key]:
            continue
        rank = ranks_by_run[run_key]
        ranks_by_run[run_key] += 1
        seen_by_run[run_key].add(block)
        ranks_by_block[block].append(rank)
        runs_by_block[block].add(run_key)

    return {
        block: {
            "frequency": len(run_keys) / total_runs,
            "median_rank": median(ranks_by_block[block]),
            "p90_rank": p90(ranks_by_block[block]),
        }
        for block, run_keys in runs_by_block.items()
    }


def selected_blocks(stats, threshold):
    return {block for block, values in stats.items() if values["frequency"] >= threshold}


def ordered_blocks(blocks, stats, phase, drive_id):
    return sorted(
        blocks,
        key=lambda block: (
            PHASE_PRIORITY[phase],
            DRIVE_PRIORITY[drive_id],
            stats.get(block, {}).get("median_rank", sys.maxsize),
            stats.get(block, {}).get("p90_rank", sys.maxsize),
            -stats.get(block, {}).get("frequency", 0.0),
            block,
        ),
    )


def range_rows(drive_id, ordered):
    if not ordered:
        return []

    rows = []
    start = prev = ordered[0]
    for block in ordered[1:]:
        if block == prev + 1:
            prev = block
            continue
        rows.append({"DriveId": drive_id, "StartPage": start, "EndPage": prev})
        start = prev = block
    rows.append({"DriveId": drive_id, "StartPage": start, "EndPage": prev})
    return rows


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def preworkload_union_rows(rows, block_size, instance_id):
    matching_rows = [
        row
        for row in rows
        if row.get("phase") == "preworkload"
        and row.get("drive_id") in LOWER_DRIVE_IDS
        and row.get("task_id") == instance_id
    ]
    grouped = build_phase_drive_sets(matching_rows, block_size, "preworkload")
    warn_if_preworkload_differs(grouped)

    output_rows = []
    selected_by_drive = {}
    for drive_id in sorted(LOWER_DRIVE_IDS, key=lambda drive: DRIVE_PRIORITY[drive]):
        stats = block_stats(matching_rows, block_size, instance_id, "same_task", "preworkload", drive_id)
        blocks = set()
        for drives in grouped.values():
            blocks.update(drives.get(drive_id, []))
        selected_by_drive[drive_id] = blocks
        output_rows.extend(range_rows(drive_id, ordered_blocks(blocks, stats, "preworkload", drive_id)))
    return output_rows, selected_by_drive


def workload_rows(rows, block_size, instance_id, scope, thresholds, already_selected):
    output_rows = []
    selected_by_drive = {}
    for drive_id in sorted(LOWER_DRIVE_IDS, key=lambda drive: DRIVE_PRIORITY[drive]):
        stats = block_stats(rows, block_size, instance_id, scope, "workload", drive_id)
        threshold = thresholds[drive_id]
        blocks = selected_blocks(stats, threshold)
        blocks -= already_selected.get(drive_id, set())
        selected_by_drive[drive_id] = blocks
        output_rows.extend(range_rows(drive_id, ordered_blocks(blocks, stats, "workload", drive_id)))
    return output_rows, selected_by_drive


def merge_selected(*selected_maps):
    merged = defaultdict(set)
    for selected in selected_maps:
        for drive_id, blocks in selected.items():
            merged[drive_id].update(blocks)
    return merged


def main():
    parser = argparse.ArgumentParser(
        description="Build tiered prefetch range files from historical block traces."
    )
    parser.add_argument("instance_id", help="Task/instance ID to build prefetch tiers for")
    parser.add_argument("--block-size", type=int, default=4096)
    parser.add_argument("--threshold", type=float, default=0.8)
    parser.add_argument("--rootfs-common-threshold", type=float)
    parser.add_argument("--rootfs-same-repo-threshold", type=float)
    parser.add_argument("--rootfs-same-task-threshold", type=float)
    parser.add_argument("--workspace-same-repo-threshold", type=float)
    parser.add_argument("--workspace-same-task-threshold", type=float)
    args = parser.parse_args()

    if args.block_size <= 0:
        raise SystemExit("--block-size must be positive")

    rows = read_block_rows(DATA_DIR / "block_phased.csv")
    if not rows:
        raise SystemExit(f"no rows found in {DATA_DIR / 'block_phased.csv'}")

    rootfs_common_threshold = (
        args.rootfs_common_threshold
        if args.rootfs_common_threshold is not None
        else args.threshold
    )
    same_repo_thresholds = {
        "rootfs": (
            args.rootfs_same_repo_threshold
            if args.rootfs_same_repo_threshold is not None
            else args.threshold
        ),
        "workspace": (
            args.workspace_same_repo_threshold
            if args.workspace_same_repo_threshold is not None
            else args.threshold
        ),
    }
    same_task_thresholds = {
        "rootfs": (
            args.rootfs_same_task_threshold
            if args.rootfs_same_task_threshold is not None
            else args.threshold
        ),
        "workspace": (
            args.workspace_same_task_threshold
            if args.workspace_same_task_threshold is not None
            else args.threshold
        ),
    }

    tier0_rows, preworkload_selected = preworkload_union_rows(rows, args.block_size, args.instance_id)
    common_rootfs_stats = block_stats(
        rows,
        args.block_size,
        args.instance_id,
        "all",
        "workload",
        "rootfs",
    )
    common_rootfs_blocks = selected_blocks(common_rootfs_stats, rootfs_common_threshold)
    common_rootfs_blocks -= preworkload_selected.get("rootfs", set())
    tier0_rows.extend(
        range_rows(
            "rootfs",
            ordered_blocks(common_rootfs_blocks, common_rootfs_stats, "workload", "rootfs"),
        )
    )
    tier0_selected = merge_selected(preworkload_selected, {"rootfs": common_rootfs_blocks})

    tier1_rows, tier1_selected = workload_rows(
        rows,
        args.block_size,
        args.instance_id,
        "same_repo",
        same_repo_thresholds,
        tier0_selected,
    )
    tier01_selected = merge_selected(tier0_selected, tier1_selected)

    tier2_rows, tier2_selected = workload_rows(
        rows,
        args.block_size,
        args.instance_id,
        "same_task",
        same_task_thresholds,
        tier01_selected,
    )

    out_dir = DATA_DIR / "prefetch_sets" / args.instance_id
    outputs = [
        ("prefetch_tier0_common.jsonl", tier0_rows, tier0_selected),
        ("prefetch_tier1_same_repo.jsonl", tier1_rows, tier1_selected),
        ("prefetch_tier2_same_task.jsonl", tier2_rows, tier2_selected),
    ]
    for name, output_rows, selected in outputs:
        path = out_dir / name
        write_jsonl(path, output_rows)
        counts = ", ".join(
            f"{drive_id}={len(selected.get(drive_id, set()))}"
            for drive_id in sorted(LOWER_DRIVE_IDS, key=lambda drive: DRIVE_PRIORITY[drive])
        )
        print(f"{name}: {len(output_rows)} ranges ({counts}) -> {path}")


if __name__ == "__main__":
    raise SystemExit(main())
