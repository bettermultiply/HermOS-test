#!/usr/bin/env python3
import csv
import itertools
from collections import defaultdict
from pathlib import Path


DATA_DIR = Path("data")
SUMMARY_DIR = DATA_DIR / "summary"
LOWER_DRIVE_IDS = {"rootfs", "workspace"}
SHARED_METADATA_CLASSES = {
    "inode_table",
    "block_bitmap",
    "inode_bitmap",
    "superblock_or_gdt",
    "journal",
}


def ensure_summary_dir():
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)


def read_csv(path):
    if not Path(path).exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def block_id(row, block_size):
    start = int(row["offset"]) // block_size
    end = (int(row["offset"]) + int(row["length"]) - 1) // block_size
    return {str(block) for block in range(start, end + 1)}


def repo_from_task(task_id):
    return task_id.rsplit("-", 1)[0]


def safe_ratio(numerator, denominator):
    if denominator == 0:
        return 0.0
    return numerator / denominator


def format_float(value):
    return f"{value:.6f}"


def set_stats(items):
    counts = [len(item_set) for item_set in items]
    if not counts:
        return {
            "run_count": 0,
            "mean_count": 0.0,
            "min_count": 0,
            "max_count": 0,
            "core_count": 0,
            "union_count": 0,
            "core_ratio": 0.0,
            "core_ratio_min": 0.0,
        }

    union_set = set().union(*items)
    core_set = set(items[0]).intersection(*items[1:]) if len(items) > 1 else set(items[0])
    min_count = min(counts)
    return {
        "run_count": len(items),
        "mean_count": sum(counts) / len(counts),
        "min_count": min_count,
        "max_count": max(counts),
        "core_count": len(core_set),
        "union_count": len(union_set),
        "core_ratio": safe_ratio(len(core_set), len(union_set)),
        "core_ratio_min": safe_ratio(len(core_set), min_count),
    }


def build_pair_row(relation, run_a, run_b, set_a, set_b, meta):
    intersection = len(set_a & set_b)
    count_a = len(set_a)
    count_b = len(set_b)
    union_count = len(set_a | set_b)
    coverage_a_by_b = safe_ratio(intersection, count_a)
    coverage_b_by_a = safe_ratio(intersection, count_b)
    return {
        **meta,
        "_relation": relation,
        "run_a": run_a,
        "run_b": run_b,
        "count_a": count_a,
        "count_b": count_b,
        "intersection": intersection,
        "union_count": union_count,
        "coverage_a_by_b": format_float(coverage_a_by_b),
        "coverage_b_by_a": format_float(coverage_b_by_a),
        "high_cover": format_float(max(coverage_a_by_b, coverage_b_by_a)),
        "low_cover": format_float(min(coverage_a_by_b, coverage_b_by_a)),
        "jaccard": format_float(safe_ratio(intersection, union_count)),
    }


def summarize_pair_rows(pair_rows):
    pair_count = len(pair_rows)
    if pair_count == 0:
        return {
            "pair_count": 0,
            "mean_intersection": 0.0,
            "min_intersection": 0,
            "max_intersection": 0,
            "mean_coverage_a_by_b": 0.0,
            "min_coverage_a_by_b": 0.0,
            "max_coverage_a_by_b": 0.0,
            "mean_coverage_b_by_a": 0.0,
            "min_coverage_b_by_a": 0.0,
            "max_coverage_b_by_a": 0.0,
            "mean_jaccard": 0.0,
            "min_jaccard": 0.0,
            "max_jaccard": 0.0,
        }

    intersections = [row["intersection"] for row in pair_rows]
    coverage_a = [float(row["coverage_a_by_b"]) for row in pair_rows]
    coverage_b = [float(row["coverage_b_by_a"]) for row in pair_rows]
    jaccards = [float(row["jaccard"]) for row in pair_rows]
    return {
        "pair_count": pair_count,
        "mean_intersection": sum(intersections) / pair_count,
        "min_intersection": min(intersections),
        "max_intersection": max(intersections),
        "mean_coverage_a_by_b": sum(coverage_a) / pair_count,
        "min_coverage_a_by_b": min(coverage_a),
        "max_coverage_a_by_b": max(coverage_a),
        "mean_coverage_b_by_a": sum(coverage_b) / pair_count,
        "min_coverage_b_by_a": min(coverage_b),
        "max_coverage_b_by_a": max(coverage_b),
        "mean_jaccard": sum(jaccards) / pair_count,
        "min_jaccard": min(jaccards),
        "max_jaccard": max(jaccards),
    }


def load_grouped_sets(path, key_fields, value_fn, filters=None):
    filters = filters or {}
    grouped = defaultdict(set)
    for row in read_csv(path):
        drive_id = row.get("drive_id")
        if drive_id is not None and drive_id not in LOWER_DRIVE_IDS:
            continue
        if any(row.get(field) != expected for field, expected in filters.items()):
            continue
        values = value_fn(row)
        if not values:
            continue
        grouped[tuple(row[field] for field in key_fields)].update(values)
    return grouped


def load_metric_data(block_size):
    block_phased = DATA_DIR / "block_phased.csv"
    summary_path = DATA_DIR / "file_read_summary.csv"
    block_detail_path = DATA_DIR / "file_read_blocks.csv"

    metrics = []
    metric_specs = [
        (
            "block",
            block_phased,
            lambda row: block_id(row, block_size),
        ),
        (
            "file",
            summary_path,
            lambda row: {row["path"]},
        ),
        (
            "file_data_block",
            block_detail_path,
            lambda row: {row["block_id"]} if row.get("class") == "file_data" else set(),
        ),
        (
            "dir_data_block",
            block_detail_path,
            lambda row: {row["block_id"]} if row.get("class") == "dir_data" else set(),
        ),
        (
            "shared_metadata_block",
            block_detail_path,
            lambda row: {row["block_id"]} if row.get("class") in SHARED_METADATA_CLASSES else set(),
        ),
    ]

    for metric_kind, path, value_fn in metric_specs:
        grouped = load_grouped_sets(
            path,
            ("task_id", "run_id", "phase", "drive_id"),
            value_fn,
        )
        metrics.append(
            {
                "metric_kind": metric_kind,
                "items_by_task_run": regroup_by_task_run(grouped),
            }
        )
    return metrics


def regroup_by_task_run(grouped):
    result = defaultdict(dict)
    for (task_id, run_id, phase, drive_id), item_set in grouped.items():
        result[(task_id, phase, drive_id)][run_id] = item_set
    return result


def build_same_task_outputs(metrics):
    scale_rows = []
    pair_rows = []

    for metric in metrics:
        metric_kind = metric["metric_kind"]
        for (task_id, phase, drive_id), runs in sorted(metric["items_by_task_run"].items()):
            item_sets = [runs[run_id] for run_id in sorted(runs)]
            stats = set_stats(item_sets)
            scale_rows.append(
                {
                    "task_id": task_id,
                    "drive_id": drive_id,
                    "phase": phase,
                    "metric_kind": metric_kind,
                    "run_count": stats["run_count"],
                    "mean_count": format_float(stats["mean_count"]),
                    "min_count": stats["min_count"],
                    "max_count": stats["max_count"],
                    "core_count": stats["core_count"],
                    "union_count": stats["union_count"],
                    "core_ratio": format_float(stats["core_ratio"]),
                    "core_ratio_min": format_float(stats["core_ratio_min"]),
                }
            )

            meta = {
                "task_a": task_id,
                "task_b": task_id,
                "drive_id": drive_id,
                "phase": phase,
                "metric_kind": metric_kind,
            }
            for run_a, run_b in itertools.combinations(sorted(runs), 2):
                pair_rows.append(
                    build_pair_row(
                        "same_task",
                        run_a,
                        run_b,
                        runs[run_a],
                        runs[run_b],
                        meta,
                    )
                )

    return scale_rows, pair_rows


def build_cross_task_outputs(metrics):
    summary_rows = []
    pair_rows = []

    for metric in metrics:
        metric_kind = metric["metric_kind"]
        grouped = metric["items_by_task_run"]
        task_keys = sorted(grouped)

        for left_key, right_key in itertools.combinations(task_keys, 2):
            task_a, phase_a, drive_a = left_key
            task_b, phase_b, drive_b = right_key
            if phase_a != phase_b or drive_a != drive_b:
                continue

            runs_a = grouped[left_key]
            runs_b = grouped[right_key]
            relation = "same_repo" if repo_from_task(task_a) == repo_from_task(task_b) else "other_repo"
            meta = {
                "task_a": task_a,
                "task_b": task_b,
                "drive_id": drive_a,
                "phase": phase_a,
                "metric_kind": metric_kind,
            }

            task_pair_rows = []
            for run_a, set_a in sorted(runs_a.items()):
                for run_b, set_b in sorted(runs_b.items()):
                    row = build_pair_row(
                        relation,
                        run_a,
                        run_b,
                        set_a,
                        set_b,
                        meta,
                    )
                    task_pair_rows.append(row)
                    pair_rows.append(row)

            stats = summarize_pair_rows(task_pair_rows)
            summary_rows.append(
                {
                    **meta,
                    "_relation": relation,
                    "relation": relation,
                    "run_count_a": len(runs_a),
                    "run_count_b": len(runs_b),
                    "pair_count": stats["pair_count"],
                    "mean_intersection": format_float(stats["mean_intersection"]),
                    "min_intersection": stats["min_intersection"],
                    "max_intersection": stats["max_intersection"],
                    "mean_high_cover": format_float(
                        max(stats["mean_coverage_a_by_b"], stats["mean_coverage_b_by_a"])
                    ),
                    "min_high_cover": format_float(
                        max(stats["min_coverage_a_by_b"], stats["min_coverage_b_by_a"])
                    ),
                    "max_high_cover": format_float(
                        max(stats["max_coverage_a_by_b"], stats["max_coverage_b_by_a"])
                    ),
                    "mean_low_cover": format_float(
                        min(stats["mean_coverage_a_by_b"], stats["mean_coverage_b_by_a"])
                    ),
                    "min_low_cover": format_float(
                        min(stats["min_coverage_a_by_b"], stats["min_coverage_b_by_a"])
                    ),
                    "max_low_cover": format_float(
                        min(stats["max_coverage_a_by_b"], stats["max_coverage_b_by_a"])
                    ),
                    "mean_jaccard": format_float(stats["mean_jaccard"]),
                    "min_jaccard": format_float(stats["min_jaccard"]),
                    "max_jaccard": format_float(stats["max_jaccard"]),
                }
            )

    return summary_rows, pair_rows


SAME_TASK_SCALE_FIELDS = [
    "task_id",
    "drive_id",
    "phase",
    "metric_kind",
    "run_count",
    "mean_count",
    "min_count",
    "max_count",
    "core_count",
    "union_count",
    "core_ratio",
    "core_ratio_min",
]

PAIR_FIELDS = [
    "task_a",
    "run_a",
    "task_b",
    "run_b",
    "drive_id",
    "phase",
    "metric_kind",
    "count_a",
    "count_b",
    "intersection",
    "union_count",
    "high_cover",
    "low_cover",
    "jaccard",
]

CROSS_TASK_SUMMARY_FIELDS = [
    "task_a",
    "task_b",
    "drive_id",
    "phase",
    "metric_kind",
    "run_count_a",
    "run_count_b",
    "pair_count",
    "mean_intersection",
    "min_intersection",
    "max_intersection",
    "mean_high_cover",
    "min_high_cover",
    "max_high_cover",
    "mean_low_cover",
    "min_low_cover",
    "max_low_cover",
    "mean_jaccard",
    "min_jaccard",
    "max_jaccard",
]
