from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from launch.firecracker_start import api_sock, cleanup, stop
from launch.uffd_start import uffd_sock


ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"


def config_path(name: str) -> Path:
    return CONFIG_DIR / name


def load_config(name: str, path_keys: list[str]) -> dict:
    with config_path(name).open() as f:
        cfg = json.load(f)
    for key in path_keys:
        cfg[key] = Path(cfg[key].format(root=ROOT))
    return cfg


def ensure_root() -> int | None:
    if os.geteuid():
        print("run with sudo", file=sys.stderr)
        return 1
    return None


def now_ms() -> float:
    return time.monotonic_ns() / 1_000_000


def parallel_map(count: int, fn):
    with cf.ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(fn, range(count)))


def run_parallel_workloads(count: int, netns, timeout: int, run_workload):
    with cf.ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(lambda x: run_workload(x, timeout), enumerate(netns)))


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as src, target.open("wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)


def cleanup_sandboxes(work_dir: Path, netns, fc_procs, uffd_procs=(), extra_procs=()) -> None:
    stop(fc_procs)
    stop(uffd_procs)
    stop(extra_procs)
    for n in reversed(netns):
        cleanup(n)
    for i in range(len(netns)):
        api_sock(work_dir, i).unlink(missing_ok=True)
        uffd_sock(work_dir, i).unlink(missing_ok=True)
