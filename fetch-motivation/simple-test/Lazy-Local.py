#!/usr/bin/env python3
from __future__ import annotations

import json

from launch.firecracker_start import api_sock, start_sandboxes, stop
from launch.snapshot_start import load_snapshot
from launch.uffd_start import start_uffd_handlers, uffd_sock
from utils.common import cleanup_sandboxes, ensure_root, load_config, now_ms, parallel_map, run_parallel_workloads
from utils.remote_helpers import count_uffd_copied_pages
from utils.workload_run import run_workload


def main():
    root_error = ensure_root()
    if root_error is not None:
        return root_error

    cfg = load_config("lazy-local.json", ["snapshot_a", "snapshot_b", "snapshot_state", "firecracker", "firecracker_cwd", "uffd_handler", "work_dir"])
    count = int(cfg["count"])
    cfg["work_dir"].mkdir(parents=True, exist_ok=True)
    cfg["snapshot_b"].parent.mkdir(parents=True, exist_ok=True)

    # shutil.copyfile(cfg["snapshot_a"], cfg["snapshot_b"])
    # print(f"copied snapshot: {cfg['snapshot_a']} -> {cfg['snapshot_b']}")

    netns, fc_procs, uffd_procs = [], [], []
    try:
        start = now_ms()
        netns, fc_procs = start_sandboxes(count, cfg["firecracker"], cfg["firecracker_cwd"], cfg["work_dir"])
        print(f"started {count} netns firecracker processes")

        uffd_procs = start_uffd_handlers(count, cfg["uffd_handler"], cfg["work_dir"], cfg["snapshot_a"])
        print(f"started {count} uffd handlers")

        parallel_map(count, lambda i: load_snapshot(api_sock(cfg["work_dir"], i), cfg["snapshot_state"], uffd_sock(cfg["work_dir"], i), backend_type="Uffd"))
        print(f"loaded {count} snapshots")
        sandbox_start_ms = now_ms() - start

        start = now_ms()
        raw_results = run_parallel_workloads(count, netns, cfg["timeout"], run_workload)
        workload_run_ms = now_ms() - start
        results = [item[0] for item in raw_results]
        stop(uffd_procs)
        uffd_procs = []
        print(
            json.dumps(
                {
                    "workloads": results,
                    "snapshot_pull_ms": 0,
                    "sandbox_start_ms": sandbox_start_ms,
                    "workload_run_ms": workload_run_ms,
                    "copied_pages": count_uffd_copied_pages(cfg["work_dir"], count),
                },
                indent=2,
            )
        )
        return 0
    finally:
        cleanup_sandboxes(cfg["work_dir"], netns, fc_procs, uffd_procs)


if __name__ == "__main__":
    raise SystemExit(main())
