from __future__ import annotations

import json
import re
import subprocess as sp
import sys
import time
import urllib.request
from pathlib import Path

from launch.uffd_start import uffd_sock, wait_sock
from utils.run_work_dir import run_work_dir

COPIED_PAGES_RE = re.compile(rb"^COPIED_PAGES=(\d+)$")


def download_timed(url: str, target: Path) -> float:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    start = time.monotonic_ns()
    with urllib.request.urlopen(url, timeout=30) as src, tmp.open("wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    tmp.replace(target)
    return (time.monotonic_ns() - start) / 1_000_000


def count_uffd_copied_pages(work_dir: Path, count: int) -> int:
    total = 0
    for i in range(count):
        path = run_work_dir(work_dir, i) / f"uffd-{i}.log"
        if not path.exists():
            continue
        with path.open("rb") as f:
            summary = None
            fallback = 0
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                match = COPIED_PAGES_RE.match(line)
                if match:
                    summary = int(match.group(1))
                    continue
                fallback += 1
            total += summary if summary is not None else fallback
    return total


def start_remote_uffd(i: int, handler: Path, work_dir: Path, memory_blob_url: str):
    run_dir = run_work_dir(work_dir, i)
    uffd_sock(work_dir, i).unlink(missing_ok=True)
    log = (run_dir / f"uffd-{i}.log").open("wb")
    proc = sp.Popen(
        [str(handler), str(uffd_sock(work_dir, i)), memory_blob_url, str(i)],
        stdout=log,
        stderr=sp.STDOUT,
    )
    wait_sock(uffd_sock(work_dir, i))
    return proc


def start_remote_uffd_handlers(count: int, handler: Path, work_dir: Path, memory_blob_url: str):
    return [start_remote_uffd(i, handler, work_dir, memory_blob_url) for i in range(count)]


def start_proxy(cfg: dict, proxy_script: Path) -> sp.Popen:
    host = cfg.get("proxy_host", "127.0.0.1")
    port = int(cfg.get("proxy_port", 5100))
    cfg["proxy_url"] = f"http://{host}:{port}/memory"
    cfg["proxy_stats_url"] = f"http://{host}:{port}/__stats"
    log = (cfg["work_dir"] / "range-dedup-proxy.log").open("wb")
    proc = sp.Popen(
        [
            sys.executable,
            str(proxy_script),
            "--listen-host",
            host,
            "--port",
            str(port),
            "--remote-url",
            cfg["memory_blob_url"],
            "--client-timeout-sec",
            str(cfg.get("timeout", 120)),
            "--upstream-timeout-sec",
            str(cfg.get("upstream_timeout_sec", 30)),
        ],
        stdout=log,
        stderr=sp.STDOUT,
    )
    wait_proxy(cfg["proxy_stats_url"])
    return proc


def wait_proxy(stats_url: str, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(stats_url, timeout=1) as response:
                response.read()
            return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.05)
    raise TimeoutError(f"range dedup proxy did not become ready: {last_error}")


def read_proxy_stats(stats_url: str) -> dict:
    with urllib.request.urlopen(stats_url, timeout=5) as response:
        return json.loads(response.read().decode())
