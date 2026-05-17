from __future__ import annotations

import csv
import os
import uuid
from datetime import datetime, timezone

from utils.common import ROOT
from utils.workload_run import default_workload_id


CSV_FIELDS = [
    "run_id",
    "group_name",
    "concurrency",
    "repeat_idx",
    "workload_id",
    "workload_ms_avg",
    "memory_pull_ms",
    "snapshot_state_pull_ms",
    "sandbox_start_ms",
    "workload_run_ms",
    "roundtrip_ms_max",
    "copied_pages_total",
    "total_time_ms",
]

DATA_DIR = ROOT / "fetch-motivation" / "data"
CSV_PATH = DATA_DIR / "experiment_runs.csv"


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def make_run_id(group_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{group_name}-{stamp}-{uuid.uuid4().hex[:8]}"


def repeat_idx_from_config(cfg: dict) -> int:
    return _as_int(cfg.get("repeat_idx", os.environ.get("REPEAT_IDX", 0)))


def workload_id_from_results(results: list[dict], fallback: str = "") -> str:
    for result in results:
        workload = result.get("workload")
        if workload:
            return str(workload)
    return str(fallback or "")


def workload_ms_avg(results: list[dict]) -> float:
    values = [
        _as_float(result.get("workload_ms"))
        for result in results
        if result.get("workload_ms") is not None
    ]
    if not values:
        return 0.0
    return sum(values) / len(values)


def roundtrip_ms_max(results: list[dict]) -> float:
    values = [_as_float(result.get("client_round_trip_ms")) for result in results]
    return max(values, default=0.0)


def append_experiment_run(
    *,
    group_name: str,
    cfg: dict,
    results: list[dict],
    memory_pull_ms=0,
    snapshot_state_pull_ms=0,
    sandbox_start_ms=0,
    workload_run_ms=0,
    copied_pages_total=0,
) -> dict:
    record = {
        "run_id": make_run_id(group_name),
        "group_name": group_name,
        "concurrency": _as_int(cfg.get("count")),
        "repeat_idx": repeat_idx_from_config(cfg),
        "workload_id": workload_id_from_results(results, default_workload_id()),
        "workload_ms_avg": workload_ms_avg(results),
        "memory_pull_ms": _as_float(memory_pull_ms),
        "snapshot_state_pull_ms": _as_float(snapshot_state_pull_ms),
        "sandbox_start_ms": _as_float(sandbox_start_ms),
        "workload_run_ms": _as_float(workload_run_ms),
        "roundtrip_ms_max": roundtrip_ms_max(results),
        "copied_pages_total": _as_int(copied_pages_total),
    }
    record["total_time_ms"] = (
        record["memory_pull_ms"]
        + record["snapshot_state_pull_ms"]
        + record["sandbox_start_ms"]
        + record["workload_run_ms"]
    )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(record)
    return record
