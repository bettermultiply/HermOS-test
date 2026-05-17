#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess as sp
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIMPLE_TEST = ROOT / "simple-test"
LOG_DIR = ROOT / "data" / "batch-runs"
RUN_DIR_RE = re.compile(r"^run_(\d+)$")
AGENT_TOOL_REPLAY = "agent-tool-replay"
DEFAULT_WORKLOADS = [
    "health-daemon",
    "health-exec",
    "read-list",
]

GROUPS = [
    {
        "name": "eager-local",
        "script": SIMPLE_TEST / "Eager-Local.py",
        "config": SIMPLE_TEST / "configs" / "eager-local.json",
    },
    {
        "name": "eager-remote",
        "script": SIMPLE_TEST / "Eager-Remote.py",
        "config": SIMPLE_TEST / "configs" / "eager-remote.json",
    },
    {
        "name": "lazy-local",
        "script": SIMPLE_TEST / "Lazy-Local.py",
        "config": SIMPLE_TEST / "configs" / "lazy-local.json",
    },
    {
        "name": "lazy-remote-dedup",
        "script": SIMPLE_TEST / "Lazy-Remote-Dedup.py",
        "config": SIMPLE_TEST / "configs" / "lazy-remote-dedup.json",
    },
]

OPTIONAL_GROUPS = [
    {
        "name": "lazy-remote",
        "script": SIMPLE_TEST / "Lazy-Remote.py",
        "config": SIMPLE_TEST / "configs" / "lazy-remote.json",
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the four default fetch-motivation experiment groups with one concurrency."
    )
    parser.add_argument("--count", type=int, required=True, help="Sandbox concurrency to write into each config before running.")
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="How many rounds to run for this concurrency.",
    )
    parser.add_argument(
        "--log-dir",
        default=str(LOG_DIR),
        help="Directory for per-group stdout/stderr logs.",
    )
    parser.add_argument(
        "--with-lazy-remote",
        action="store_true",
        help="Also run lazy-remote as an optional fifth group.",
    )
    parser.add_argument(
        "--with-agent-tool-replay",
        action="store_true",
        help="Append agent-tool-replay to the workload list.",
    )
    parser.add_argument(
        "--workloads",
        nargs="+",
        default=None,
        help="Workload ids to run in sequence for every round. Defaults to health-daemon health-exec read-list.",
    )
    return parser.parse_args()


def resolve_workloads(args: argparse.Namespace) -> list[str]:
    workloads = list(args.workloads) if args.workloads is not None else list(DEFAULT_WORKLOADS)
    if args.with_agent_tool_replay and AGENT_TOOL_REPLAY not in workloads:
        workloads.append(AGENT_TOOL_REPLAY)
    return workloads


def read_json(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def write_json(path: Path, data: dict) -> None:
    with path.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def count_log_dir(base_log_dir: Path, count: int) -> Path:
    return base_log_dir / f"count_{count}"


def next_run_index(parent: Path) -> int:
    used = set()
    if parent.exists():
        for child in parent.iterdir():
            if not child.is_dir():
                continue
            match = RUN_DIR_RE.fullmatch(child.name)
            if match:
                used.add(int(match.group(1)))

    index = 1
    while index in used:
        index += 1
    return index


def prepare_log_dir(base_log_dir: Path, count: int) -> tuple[Path, int]:
    count_dir = count_log_dir(base_log_dir, count)
    count_dir.mkdir(parents=True, exist_ok=True)
    run_idx = next_run_index(count_dir)
    run_dir = count_dir / f"run_{run_idx}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir, run_idx


def update_count(path: Path, count: int) -> tuple[int | None, bool]:
    cfg = read_json(path)
    original = cfg.get("count")
    changed = original != count
    if changed:
        cfg["count"] = count
        write_json(path, cfg)
    return (int(original) if isinstance(original, int) else None), changed


def restore_count(path: Path, original_count: int | None) -> None:
    if original_count is None:
        return
    cfg = read_json(path)
    cfg["count"] = original_count
    write_json(path, cfg)


def workload_log_name(group_name: str, workload_id: str) -> str:
    safe_workload = workload_id.replace("/", "_")
    return f"{group_name}--{safe_workload}.log"


def run_group(group: dict, *, log_dir: Path, repeat_idx: int, workload_id: str) -> int:
    log_path = log_dir / workload_log_name(group["name"], workload_id)
    env = os.environ.copy()
    env["REPEAT_IDX"] = str(repeat_idx)
    env["WORKLOAD_ID"] = workload_id
    with log_path.open("w") as log:
        proc = sp.run(
            [sys.executable, str(group["script"])],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=sp.STDOUT,
            text=True,
        )
    print(
        f"[repeat {repeat_idx}] [workload {workload_id}] "
        f"[{group['name']}] exit={proc.returncode} log={log_path}"
    )
    return proc.returncode


def main() -> int:
    args = parse_args()
    if args.count <= 0:
        print("--count must be > 0", file=sys.stderr)
        return 2
    if args.repeats <= 0:
        print("--repeats must be > 0", file=sys.stderr)
        return 2
    workloads = resolve_workloads(args)
    if not workloads:
        print("--workloads must not be empty", file=sys.stderr)
        return 2

    groups = GROUPS + OPTIONAL_GROUPS if args.with_lazy_remote else GROUPS

    originals: list[tuple[Path, int | None, bool]] = []
    try:
        for group in groups:
            original_count, changed = update_count(group["config"], args.count)
            originals.append((group["config"], original_count, changed))

        for _ in range(args.repeats):
            log_dir, repeat_idx = prepare_log_dir(Path(args.log_dir), args.count)
            for workload_id in workloads:
                failures = []
                for group in groups:
                    rc = run_group(
                        group,
                        log_dir=log_dir,
                        repeat_idx=repeat_idx,
                        workload_id=workload_id,
                    )
                    if rc != 0:
                        failures.append((group["name"], rc))

                if failures:
                    for name, rc in failures:
                        print(
                            f"FAILED repeat {repeat_idx} workload {workload_id} "
                            f"{name}: exit {rc}",
                            file=sys.stderr,
                        )
                    return 1
        return 0
    finally:
        for path, original_count, changed in originals:
            if changed:
                restore_count(path, original_count)


if __name__ == "__main__":
    raise SystemExit(main())
