#!/usr/bin/env python3
"""
Usage: python3 plot_combined.py <results_dir> [-o combined.png]

Generates a combined breakdown timeline chart for the full concurrent restore
pipeline, styled like the per-op timeline charts (Y = instance index, X = time).

Each row corresponds to one instance.  The 19 operations are concatenated along
the X axis in pipeline order, each operation occupying a contiguous x-band.

Within each operation's band, times are normalized so that the operation's
earliest-starting instance begins at t=0 (i.e. start_us - min_start_us).
The band's width equals that operation's maximum normalized end_us.

For each instance inside a band:
  - hatched bar  : from t=0 of that band to the instance's normalized start
                   (delay before this instance began, same color as solid bar)
  - solid bar    : the actual operation span
"""

import sys
import os
import csv
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
from matplotlib.patches import Patch

COMBINED_OPS_ORDER = [
    "kick_virtio_full_netns",
    "restore_vcpu_states",
    "restore_memory_regions",
    "device_restore_block",
    "device_restore_net_full_netns",
    "create_vcpus",
    "resume_vcpus_full_netns",
    "create_firecracker_full_netns",
    "netns_full_shell",
    # "snapshot_load",
    # "guest_memory",
    # "kvm_create_vm",
    # "restore_state",
    # "device_restore_balloon",
    # "device_restore_vsock",
    # "device_restore_entropy",
    # "device_restore_pmem",
    # "device_restore_virtio_mem",
]


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


def group_instances(rows):
    instances = {}
    for row in rows:
        instances.setdefault(row["instance_id"], []).append(row)
    ordered = sorted(
        instances.items(),
        key=lambda item: (
            min(s["start_us"] for s in item[1]),
            min(s["thread_id"] for s in item[1]),
            item[0],
        ),
    )
    return ordered


def plot_combined_breakdown(data, ops_order, out_path):
    available_ops = [op for op in ops_order if op in data]
    n_ops = len(available_ops)
    if n_ops == 0:
        raise ValueError("No matching operations found in data")

    cmap = plt.get_cmap("tab20")
    op_colors = {op: cmap(2*i % cmap.N) for i, op in enumerate(available_ops)}

    # Per-operation normalization: earliest instance → t=0
    op_min_start = {op: min(r["start_us"] for r in data[op]) for op in available_ops}

    # Build per-op iid→rows mapping
    op_iid_rows = {}
    for op in available_ops:
        iid_rows = {}
        for row in data[op]:
            iid_rows.setdefault(row["instance_id"], []).append(row)
        op_iid_rows[op] = iid_rows

    # Per-operation row mapping: each op sorts its instances by norm_end independently
    op_iid_to_row = {}
    op_row_to_iid = {}
    n_instances = 0
    for op in available_ops:
        offset = op_min_start[op]
        iid_norm_end = {
            iid: max(r["end_us"] for r in rows) - offset
            for iid, rows in op_iid_rows[op].items()
        }
        reverse = op != "netns_full_shell" and op != "device_restore_block" and op != "resume_vm_full_netns"
        sorted_iids_op = sorted(iid_norm_end, key=iid_norm_end.__getitem__, reverse=reverse)
        op_iid_to_row[op] = {iid: i for i, iid in enumerate(sorted_iids_op)}
        op_row_to_iid[op] = {i: iid for i, iid in enumerate(sorted_iids_op)}
        n_instances = max(n_instances, len(sorted_iids_op))

    # Per-row cumulative x positions across ops:
    # row r's bar in op k+1 starts exactly where row r's bar in op k ended.
    # (row r may map to a different instance in each op — that's fine.)
    SCALE = 1e-3   # μs → ms
    X_OFFSET = 1   # shift all x by 1 ms so log scale avoids log(0)
    row_op_cum = {}  # row -> {op -> x_start (ms)}
    for r in range(n_instances):
        cum = X_OFFSET
        row_op_cum[r] = {}
        for op in available_ops:
            row_op_cum[r][op] = cum
            iid = op_row_to_iid[op].get(r)
            if iid is not None:
                rows = op_iid_rows[op][iid]
                norm_end = (max(row["end_us"] for row in rows) - op_min_start[op]) * SCALE
                cum += norm_end

    total_x = max(
        max(row_op_cum[r].values()) +
        ((max(row["end_us"] for row in op_iid_rows[available_ops[-1]][op_row_to_iid[available_ops[-1]][r]])
          - op_min_start[available_ops[-1]]) * SCALE
         if op_row_to_iid[available_ops[-1]].get(r) is not None else 0)
        for r in range(n_instances)
    )

    # Figure – wide and compact
    fig_height = max(4, 3.2 + 0.055 * n_instances)
    fig, ax = plt.subplots(figsize=(30, fig_height))
    row_height = 0.6

    for op in available_ops:
        color = op_colors[op]
        c = mcolors.to_rgba(color)
        min_start = op_min_start[op]

        for iid, spans in op_iid_rows[op].items():
            row = op_iid_to_row[op].get(iid)
            if row is None:
                continue

            x0 = row_op_cum[row][op]
            norm_start = (min(s["start_us"] for s in spans) - min_start) * SCALE
            norm_end = (max(s["end_us"] for s in spans) - min_start) * SCALE
            yrange = (row * row_height, row_height)

            # Hatched delay bar (this op's band start → instance's normalized start)
            if norm_start > 0:
                ax.broken_barh(
                    [(x0, norm_start)],
                    yrange,
                    facecolors=[(*c[:3], 0.20)],
                    edgecolors="none",
                )
                ax.broken_barh(
                    [(x0, norm_start)],
                    yrange,
                    facecolors="none",
                    edgecolors=[(*c[:3], 0.55)],
                    hatch="////",
                    linewidth=0.25,
                )

            # Solid active bar
            ax.broken_barh(
                [(x0 + norm_start, norm_end - norm_start)],
                yrange,
                facecolors=[color],
                edgecolors="none",
                alpha=0.92,
            )

    # Vertical separators: median x_start of each op across rows
    op_median_x = {
        op: float(np.median([row_op_cum[r][op] for r in range(n_instances)
                             if op_row_to_iid[op].get(r) is not None]))
        for op in available_ops
    }
    for op in available_ops[1:]:
        ax.axvline(op_median_x[op], color="gray", linewidth=0.6,
                   linestyle="--", alpha=0.45)

    # Operation name labels above each band (at median midpoint)
    ax.set_ylim(0, n_instances * row_height)
    label_y = n_instances * row_height
    for op in available_ops:
        mids = []
        for r in range(n_instances):
            iid = op_row_to_iid[op].get(r)
            if iid is None:
                continue
            rows = op_iid_rows[op][iid]
            norm_end = (max(row["end_us"] for row in rows) - op_min_start[op]) * SCALE
            mids.append(row_op_cum[r][op] + norm_end / 2)
        mid_x = float(np.median(mids)) if mids else op_median_x[op]
        ax.text(
            mid_x, label_y, op,
            ha="center", va="bottom", fontsize=26,
            rotation=45, color=op_colors[op], clip_on=True,
        )

    # Y axis
    tick_step = max(1, n_instances // 12)
    major_ticks = np.arange(0, n_instances, tick_step)
    ax.set_yticks(major_ticks * row_height + row_height / 2)
    ax.set_yticklabels(major_ticks)
    ax.set_yticks(np.arange(n_instances) * row_height + row_height / 2, minor=True)
    ax.set_ylabel("Sorted Instance Index", fontsize=28)
    ax.tick_params(axis="y", labelsize=26)

    ax.set_xscale("log")
    ax.set_xlim(X_OFFSET, total_x * 1.005)
    ax.set_xlabel("Normalized timeline (ms per operation, concatenated per instance, log scale)", fontsize=28)
    ax.tick_params(axis="x", labelsize=26)
    ax.set_title("Concurrent Restore Pipeline – Combined Operation Timeline Breakdown", fontsize=30)
    ax.grid(axis="x", linestyle=":", linewidth=0.8, alpha=0.6)
    ax.grid(which="minor", axis="y", linestyle=":", linewidth=0.5, alpha=0.25)
    ax.set_axisbelow(True)

    # Legend: one entry per operation + one for hatching
    handles = [Patch(facecolor=op_colors[op], label=op) for op in available_ops]
    handles.append(
        Patch(facecolor="gray", alpha=0.25, hatch="////", edgecolor="gray",
              label="Delay before instance start")
    )
    fig.tight_layout(rect=(0, 0, 0.72, 1.0))
    fig.legend(
        handles=handles,
        loc="center left",
        ncol=1,
        frameon=False,
        fontsize=26,
        bbox_to_anchor=(0.73, 0.5),
    )
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Combined breakdown plot saved to {out_path}")


def main():
    args = sys.argv[1:]
    out_path = "combined_breakdown.png"
    results_dir = None

    i = 0
    while i < len(args):
        if args[i] == "-o":
            i += 1
            out_path = args[i]
        else:
            results_dir = args[i]
        i += 1

    if results_dir is None:
        print("Usage: python3 plot_combined.py <results_dir> [-o output.png]")
        sys.exit(1)

    data = {}
    missing = []
    for op in COMBINED_OPS_ORDER:
        path = os.path.join(results_dir, f"{op}.csv")
        if os.path.exists(path):
            data[op] = read_csv(path)
        else:
            missing.append(op)

    if missing:
        print(f"Warning: missing CSV files for: {', '.join(missing)}")

    print(
        f"{'op':<40} {'n':>5} {'min_start':>10} {'max_end':>12} "
        f"{'mean_us':>10} {'p50_us':>10} {'p99_us':>10}"
    )
    print("-" * 107)
    for op in COMBINED_OPS_ORDER:
        if op not in data:
            continue
        rows = data[op]
        elapsed = np.array([r["elapsed_us"] for r in rows])
        print(
            f"{op:<40} {len(rows):>5} {min(r['start_us'] for r in rows):>10} "
            f"{max(r['end_us'] for r in rows):>12} "
            f"{elapsed.mean():>10.0f} {np.median(elapsed):>10.0f} "
            f"{np.percentile(elapsed, 99):>10.0f}"
        )

    plot_combined_breakdown(data, COMBINED_OPS_ORDER, out_path)


if __name__ == "__main__":
    main()
