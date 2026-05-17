#!/usr/bin/env python3
from __future__ import annotations

import json

from launch.firecracker_start import api_sock, start_sandboxes, stop
from launch.snapshot_start import load_snapshot
from launch.uffd_start import uffd_sock
from utils.common import cleanup_sandboxes, download, ensure_root, load_config, now_ms, parallel_map, run_parallel_workloads
from utils.remote_helpers import count_uffd_copied_pages, start_remote_uffd_handlers
from utils.result_csv import append_experiment_run
from utils.workload_run import run_workload


def main():
    root_error = ensure_root()
    if root_error is not None:
        return root_error

    cfg = load_config("lazy-remote.json", ["snapshot_state", "firecracker", "firecracker_cwd", "uffd_handler", "work_dir"])
    count = int(cfg["count"])
    cfg["work_dir"].mkdir(parents=True, exist_ok=True)

    memory_pull_ms = 0
    snapshot_state_pull_ms = 0
    snapshot_pull_ms = 0
    if cfg.get("snapshot_state_url"):
        start = now_ms()
        download(cfg["snapshot_state_url"], cfg["snapshot_state"])
        snapshot_state_pull_ms = now_ms() - start
        snapshot_pull_ms = snapshot_state_pull_ms
        print(f"downloaded snapshot state: {cfg['snapshot_state']}")

    netns, fc_procs, uffd_procs = [], [], []
    try:
        start = now_ms()
        netns, fc_procs = start_sandboxes(count, cfg["firecracker"], cfg["firecracker_cwd"], cfg["work_dir"])
        print(f"started {count} netns firecracker processes")

        uffd_procs = start_remote_uffd_handlers(count, cfg["uffd_handler"], cfg["work_dir"], cfg["memory_blob_url"])
        print(f"started {count} remote uffd handlers")

        parallel_map(count, lambda i: load_snapshot(api_sock(cfg["work_dir"], i), cfg["snapshot_state"], uffd_sock(cfg["work_dir"], i), backend_type="Uffd"))
        print(f"loaded {count} snapshots")
        sandbox_start_ms = now_ms() - start

        start = now_ms()
        raw_results = run_parallel_workloads(count, netns, cfg["timeout"], run_workload)
        workload_run_ms = now_ms() - start
        results = [item[0] for item in raw_results]
        stop(uffd_procs)
        uffd_procs = []
        copied_pages_total = count_uffd_copied_pages(cfg["work_dir"], count)
        record = append_experiment_run(
            group_name="lazy-remote",
            cfg=cfg,
            results=results,
            memory_pull_ms=memory_pull_ms,
            snapshot_state_pull_ms=snapshot_state_pull_ms,
            sandbox_start_ms=sandbox_start_ms,
            workload_run_ms=workload_run_ms,
            copied_pages_total=copied_pages_total,
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
                    "copied_pages": copied_pages_total,
                    "workloads": results,
                },
                indent=2,
            )
        )
        return 0
    finally:
        cleanup_sandboxes(cfg["work_dir"], netns, fc_procs, uffd_procs)


if __name__ == "__main__":
    raise SystemExit(main())
