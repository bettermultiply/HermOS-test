#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from launch.firecracker_start import api_sock, start_sandboxes, stop
from launch.snapshot_start import load_snapshot
from launch.uffd_start import uffd_sock
from utils.common import cleanup_sandboxes, download, ensure_root, load_config, now_ms, parallel_map, run_parallel_workloads
from utils.remote_helpers import count_uffd_copied_pages, read_proxy_stats, start_proxy, start_remote_uffd_handlers
from utils.workload_run import run_workload


PROXY = Path(__file__).with_name("range_dedup_proxy.py")


def main():
    root_error = ensure_root()
    if root_error is not None:
        return root_error

    cfg = load_config("lazy-remote-dedup.json", ["snapshot_state", "firecracker", "firecracker_cwd", "uffd_handler", "work_dir"])
    count = int(cfg["count"])
    cfg["work_dir"].mkdir(parents=True, exist_ok=True)

    snapshot_pull_ms = 0
    if cfg.get("snapshot_state_url"):
        start = now_ms()
        download(cfg["snapshot_state_url"], cfg["snapshot_state"])
        snapshot_pull_ms = now_ms() - start
        print(f"downloaded snapshot state: {cfg['snapshot_state']}")

    proxy_proc, netns, fc_procs, uffd_procs = None, [], [], []
    try:
        proxy_proc = start_proxy(cfg, PROXY)
        print(f"started range dedup proxy: {cfg['proxy_url']} -> {cfg['memory_blob_url']}")

        start = now_ms()
        netns, fc_procs = start_sandboxes(count, cfg["firecracker"], cfg["firecracker_cwd"], cfg["work_dir"])
        print(f"started {count} netns firecracker processes")

        uffd_procs = start_remote_uffd_handlers(count, cfg["uffd_handler"], cfg["work_dir"], cfg["proxy_url"])
        print(f"started {count} dedup remote uffd handlers")

        parallel_map(count, lambda i: load_snapshot(api_sock(cfg["work_dir"], i), cfg["snapshot_state"], uffd_sock(cfg["work_dir"], i), backend_type="Uffd"))
        print(f"loaded {count} snapshots")
        sandbox_start_ms = now_ms() - start

        start = now_ms()
        raw_results = run_parallel_workloads(count, netns, cfg["timeout"], run_workload)
        workload_run_ms = now_ms() - start
        results = [item[0] for item in raw_results]
        stop(uffd_procs)
        uffd_procs = []
        proxy_stats = read_proxy_stats(cfg["proxy_stats_url"])
        print(
            json.dumps(
                {
                    "snapshot_pull_ms": snapshot_pull_ms,
                    "sandbox_start_ms": sandbox_start_ms,
                    "workload_run_ms": workload_run_ms,
                    "copied_pages": count_uffd_copied_pages(cfg["work_dir"], count),
                    "dedup_proxy": proxy_stats,
                    "workloads": results,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        if cfg.get("proxy_stats_url"):
            try:
                print(
                    json.dumps(
                        {
                            "dedup_proxy_on_error": read_proxy_stats(cfg["proxy_stats_url"]),
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"failed to read dedup proxy stats on error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        cleanup_sandboxes(cfg["work_dir"], netns, fc_procs, uffd_procs, [proxy_proc] if proxy_proc is not None else [])


if __name__ == "__main__":
    raise SystemExit(main())
