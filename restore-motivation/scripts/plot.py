#!/usr/bin/env python3
"""
Usage: python3 plot.py <csv_file> [csv_file2 ...] [-o output.png]

Reads per-op CSV files and generates:
1. Timeline charts for each op (one row per sorted instance)
2. Summary stats printed to stdout
"""
import sys
import os
import csv
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch


def read_csv(path):
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            start_us = int(row.get("start_us", 0))
            elapsed_us = int(row["elapsed_us"])
            end_us = int(row.get("end_us", start_us + elapsed_us))
            instance_id = int(row.get("instance_id", idx))
            rows.append(
                {
                    "thread_id": int(row["thread_id"]),
                    "iteration": int(row["iteration"]),
                    "instance_id": instance_id,
                    "stage": row["stage"],
                    "start_us": start_us,
                    "end_us": end_us,
                    "elapsed_us": elapsed_us,
                }
            )
    return rows


def summarize(rows):
    elapsed = np.array([row["elapsed_us"] for row in rows], dtype=np.int64)
    start_min = min(row["start_us"] for row in rows)
    end_max = max(row["end_us"] for row in rows)
    return {
        "count": len(elapsed),
        "start_min": start_min,
        "end_max": end_max,
        "mean_us": float(elapsed.mean()),
        "p50_us": float(np.median(elapsed)),
        "p99_us": float(np.percentile(elapsed, 99)),
        "max_us": int(elapsed.max()),
    }


def build_stage_colors(ops):
    stages = []
    for rows in ops.values():
        for row in rows:
            if row["stage"] not in stages:
                stages.append(row["stage"])
    cmap = plt.get_cmap("tab20")
    return {stage: cmap(idx % cmap.N) for idx, stage in enumerate(stages)}


def group_instances(rows):
    instances = {}
    for row in rows:
        instances.setdefault(row["instance_id"], []).append(row)

    ordered = sorted(
        instances.items(),
        key=lambda item: (
            min(stage["start_us"] for stage in item[1]),
            min(stage["thread_id"] for stage in item[1]),
            item[0],
        ),
    )
    return ordered


def plot_timelines(data, out_path):
    labels = list(data.keys())
    if not labels:
        raise ValueError("no csv data to plot")

    stage_colors = build_stage_colors(data)
    height_ratios = [max(2.5, 1.2 + 0.18 * len(group_instances(data[label]))) for label in labels]
    fig_height = sum(height_ratios)
    fig, axes = plt.subplots(
        nrows=len(labels),
        ncols=1,
        figsize=(14, fig_height),
        gridspec_kw={"height_ratios": height_ratios},
    )

    if len(labels) == 1:
        axes = [axes]

    used_stages = []
    for ax, label in zip(axes, labels):
        rows = data[label]
        ordered_instances = group_instances(rows)
        row_height = 0.8

        for sorted_idx, (_, spans) in enumerate(ordered_instances):
            for span in sorted(spans, key=lambda item: (item["start_us"], item["end_us"])):
                stage = span["stage"]
                if stage not in used_stages:
                    used_stages.append(stage)
                ax.broken_barh(
                    [(span["start_us"], span["end_us"] - span["start_us"])],
                    (sorted_idx - row_height / 2, row_height),
                    facecolors=stage_colors[stage],
                    edgecolors="none",
                    alpha=0.95,
                )

        tick_step = max(1, len(ordered_instances) // 12)
        ax.set_yticks(np.arange(0, len(ordered_instances), tick_step))
        ax.set_yticks(np.arange(len(ordered_instances)), minor=True)
        ax.set_ylim(-0.8, max(0.8, len(ordered_instances) - 0.2))
        ax.set_xlim(0, max(row["end_us"] for row in rows) * 1.02 if rows else 1)
        ax.set_title(label)
        ax.set_ylabel("Sorted Instance Index")
        ax.grid(axis="x", linestyle=":", linewidth=0.8, alpha=0.6)
        ax.grid(which="minor", axis="y", linestyle=":", linewidth=0.5, alpha=0.25)
        ax.set_axisbelow(True)

    axes[-1].set_xlabel("Timeline (us from run start)")
    if used_stages:
        handles = [Patch(facecolor=stage_colors[stage], label=stage) for stage in used_stages]
        fig.legend(handles=handles, loc="upper center", ncol=min(4, len(handles)), frameon=False)

    fig.suptitle("Per-op Timeline View", y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out_path, dpi=150)


def main():
    args = sys.argv[1:]
    out_path = "bench_results.png"

    # Parse -o flag
    csv_files = []
    i = 0
    while i < len(args):
        if args[i] == "-o":
            i += 1
            out_path = args[i]
        else:
            csv_files.append(args[i])
        i += 1

    if not csv_files:
        print("Usage: python3 plot.py <csv1> [csv2 ...] [-o output.png]")
        sys.exit(1)

    # Read data
    data = {}
    for path in csv_files:
        name = os.path.splitext(os.path.basename(path))[0]
        data[name] = read_csv(path)

    # Print summary
    print(
        f"{'op':<30} {'count':>6} {'start_us':>12} {'end_us':>12} "
        f"{'mean_us':>10} {'p50_us':>10} {'p99_us':>10} {'max_us':>10}"
    )
    print("-" * 112)
    for name, rows in data.items():
        stats = summarize(rows)
        print(
            f"{name:<30} {stats['count']:>6} {stats['start_min']:>12} {stats['end_max']:>12} "
            f"{stats['mean_us']:>10.0f} {stats['p50_us']:>10.0f} "
            f"{stats['p99_us']:>10.0f} {stats['max_us']:>10}"
        )

    plot_timelines(data, out_path)
    print(f"\nPlot saved to {out_path}")


if __name__ == "__main__":
    main()
