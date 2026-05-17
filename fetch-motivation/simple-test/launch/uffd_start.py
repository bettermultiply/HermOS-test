from __future__ import annotations

import subprocess as sp
import time
from pathlib import Path

from utils.run_work_dir import run_work_dir, socket_dir


def uffd_sock(work_dir: Path, i: int) -> Path:
    base_sock_dir = socket_dir(work_dir)
    base_sock_dir.mkdir(parents=True, exist_ok=True)
    return base_sock_dir / f"uf{i}.sock"


def wait_sock(path: Path, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return
        time.sleep(0.01)
    raise TimeoutError(path)


def start_uffd(i, handler, work_dir, memory_snapshot):
    run_dir = run_work_dir(work_dir, i)
    uffd_sock(work_dir, i).unlink(missing_ok=True)
    log = (run_dir / f"uffd-{i}.log").open("wb")
    proc = sp.Popen([str(handler), str(uffd_sock(work_dir, i)), str(memory_snapshot)], stdout=log, stderr=sp.STDOUT)
    wait_sock(uffd_sock(work_dir, i))
    return proc


def start_uffd_handlers(count, handler, work_dir, memory_snapshot):
    return [start_uffd(i, handler, work_dir, memory_snapshot) for i in range(count)]
