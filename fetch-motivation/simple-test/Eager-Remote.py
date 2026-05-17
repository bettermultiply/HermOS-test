#!/usr/bin/env python3
from __future__ import annotations

import json

from launch.firecracker_start import api_sock, start_sandboxes
from launch.snapshot_start import load_snapshot
from utils.common import cleanup_sandboxes, ensure_root, load_config, now_ms, parallel_map, run_parallel_workloads
from utils.remote_helpers import download_timed
from utils.result_csv import append_experiment_run
from utils.workload_run import run_workload


def main():
    root_error = ensure_root()
    if root_error is not None:
        return root_error

    cfg = load_config("eager-remote.json", ["memory_snapshot", "snapshot_state", "firecracker", "firecracker_cwd", "work_dir"])
    count = int(cfg["count"])
    cfg["work_dir"].mkdir(parents=True, exist_ok=True)

    memory_pull_ms = download_timed(cfg["memory_snapshot_url"], cfg["memory_snapshot"])
    print(f"downloaded memory snapshot: {cfg['memory_snapshot_url']} -> {cfg['memory_snapshot']}")

    snapshot_state_pull_ms = download_timed(cfg["snapshot_state_url"], cfg["snapshot_state"])
    print(f"downloaded snapshot state: {cfg['snapshot_state_url']} -> {cfg['snapshot_state']}")

    snapshot_pull_ms = memory_pull_ms + snapshot_state_pull_ms

    netns, procs = [], []
    try:
        start = now_ms()
        netns, procs = start_sandboxes(count, cfg["firecracker"], cfg["firecracker_cwd"], cfg["work_dir"])
        print(f"started {count} netns firecracker processes")

        parallel_map(count, lambda i: load_snapshot(api_sock(cfg["work_dir"], i), cfg["snapshot_state"], cfg["memory_snapshot"]))
        print(f"loaded {count} snapshots")
        sandbox_start_ms = now_ms() - start

        start = now_ms()
        raw_results = run_parallel_workloads(count, netns, cfg["timeout"], run_workload)
        workload_run_ms = now_ms() - start
        results = [item[0] for item in raw_results]
        record = append_experiment_run(
            group_name="eager-remote",
            cfg=cfg,
            results=results,
            memory_pull_ms=memory_pull_ms,
            snapshot_state_pull_ms=snapshot_state_pull_ms,
            sandbox_start_ms=sandbox_start_ms,
            workload_run_ms=workload_run_ms,
            copied_pages_total=0,
        )
        print(
            json.dumps(
                {
                    **record,
                    "memory_pull_ms": memory_pull_ms,
                    "snapshot_state_pull_ms": snapshot_state_pull_ms,
                    "snapshot_pull_ms": snapshot_pull_ms,
                    "sandbox_start_ms": sandbox_start_ms,
                    "workload_run_ms": workload_run_ms,
                    "workloads": results,
                },
                indent=2,
            )
        )

        return 0
    finally:
        cleanup_sandboxes(cfg["work_dir"], netns, procs)


if __name__ == "__main__":
    raise SystemExit(main())
