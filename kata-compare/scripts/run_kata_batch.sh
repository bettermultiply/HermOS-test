#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)
CONFIG_TEMPLATE="${ROOT_DIR}/configs/podsandbox.json"
STATE_DIR="${ROOT_DIR}/state"
BATCH_DIR="${STATE_DIR}/batches"
mkdir -p "${BATCH_DIR}"

if ! command -v crictl >/dev/null 2>&1; then
  echo "crictl not found" >&2
  exit 1
fi

COUNT="${1:-${COUNT:-1}}"
if ! [[ "${COUNT}" =~ ^[1-9][0-9]*$ ]]; then
  echo "COUNT must be a positive integer" >&2
  exit 1
fi

RUNTIME_HANDLER="${RUNTIME_HANDLER:-kata}"
BATCH_ID="${BATCH_ID:-$(date +%Y%m%d-%H%M%S)}"
BATCH_PATH="${BATCH_DIR}/${BATCH_ID}"
mkdir -p "${BATCH_PATH}"

create_config() {
  local target_path=$1
  local run_id=$2
  local index=$3

  python3 - <<'PY' "${CONFIG_TEMPLATE}" "${target_path}" "${run_id}" "${index}"
import json
import pathlib
import sys

template = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
run_id = sys.argv[3]
index = sys.argv[4]

cfg = json.loads(template.read_text())
name = f"kata-compare-{run_id}"
cfg["metadata"]["name"] = name
cfg["metadata"]["uid"] = name
cfg["hostname"] = name
cfg["log_directory"] = f"/tmp/kata-compare/{run_id}"
cfg.setdefault("labels", {})
cfg["labels"]["run_id"] = run_id
cfg["labels"]["batch_id"] = run_id.rsplit("-", 1)[0]
cfg["labels"]["batch_index"] = index

target.write_text(json.dumps(cfg, indent=2) + "\n")
PY
}

run_one() {
  local index=$1
  local run_name
  printf -v run_name "%s-%03d" "${BATCH_ID}" "${index}"

  local run_path="${BATCH_PATH}/run_${index}"
  local config_path="${run_path}/podsandbox.json"
  local inspect_path="${run_path}/inspectp.json"
  local result_path="${run_path}/result.json"
  local stderr_path="${run_path}/runp.stderr"
  local inspect_stderr_path="${run_path}/inspectp.stderr"

  mkdir -p "${run_path}"

  local start_ns end_ns create_ms rc pod_id inspect_ok
  start_ns=$(date +%s%N)
  rc=0
  inspect_ok=0
  pod_id=""

  if pod_id="$(crictl runp --runtime="${RUNTIME_HANDLER}" "${config_path}" 2>"${stderr_path}")"; then
    :
  else
    rc=$?
  fi
  end_ns=$(date +%s%N)
  create_ms=$(( (end_ns - start_ns) / 1000000 ))

  if [[ ${rc} -eq 0 ]] && [[ -n "${pod_id}" ]]; then
    if crictl inspectp "${pod_id}" > "${inspect_path}" 2>"${inspect_stderr_path}"; then
      inspect_ok=1
    fi
  fi

  python3 - <<'PY' \
    "${result_path}" \
    "${run_name}" \
    "${index}" \
    "${RUNTIME_HANDLER}" \
    "${config_path}" \
    "${inspect_path}" \
    "${stderr_path}" \
    "${inspect_stderr_path}" \
    "${pod_id}" \
    "${start_ns}" \
    "${end_ns}" \
    "${create_ms}" \
    "${rc}" \
    "${inspect_ok}"
import json
import pathlib
import sys

result_path = pathlib.Path(sys.argv[1])
run_name = sys.argv[2]
index = int(sys.argv[3])
runtime_handler = sys.argv[4]
config_path = pathlib.Path(sys.argv[5])
inspect_path = pathlib.Path(sys.argv[6])
stderr_path = pathlib.Path(sys.argv[7])
inspect_stderr_path = pathlib.Path(sys.argv[8])
pod_id = sys.argv[9]
start_ns = int(sys.argv[10])
end_ns = int(sys.argv[11])
create_ms = int(sys.argv[12])
exit_code = int(sys.argv[13])
inspect_ok = sys.argv[14] == "1"

inspect_state = None
if inspect_ok and inspect_path.exists():
    try:
        inspect = json.loads(inspect_path.read_text())
        inspect_state = inspect.get("status", {}).get("state")
    except Exception:
        inspect_state = None

runp_stderr = stderr_path.read_text() if stderr_path.exists() else ""
inspect_stderr = inspect_stderr_path.read_text() if inspect_stderr_path.exists() else ""

result = {
    "run_id": run_name,
    "index": index,
    "runtime_handler": runtime_handler,
    "config_path": str(config_path),
    "inspect_path": str(inspect_path),
    "pod_id": pod_id,
    "start_ns": start_ns,
    "end_ns": end_ns,
    "sandbox_core_create_ms": create_ms,
    "inspect_state": inspect_state,
    "sandbox_ready": exit_code == 0 and bool(pod_id),
    "runp_exit_code": exit_code,
    "inspect_ok": inspect_ok,
    "runp_stderr": runp_stderr.strip(),
    "inspect_stderr": inspect_stderr.strip(),
    "cleanup_required": exit_code == 0 and bool(pod_id),
}
result_path.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
PY

  return "${rc}"
}

echo "batch_id=${BATCH_ID}"
echo "runtime_handler=${RUNTIME_HANDLER}"
echo "count=${COUNT}"
echo "batch_path=${BATCH_PATH}"

for i in $(seq 1 "${COUNT}"); do
  run_path="${BATCH_PATH}/run_${i}"
  mkdir -p "${run_path}"
  printf -v run_name "%s-%03d" "${BATCH_ID}" "${i}"
  create_config "${run_path}/podsandbox.json" "${run_name}" "${i}"
done

declare -a pids=()
for i in $(seq 1 "${COUNT}"); do
  run_one "${i}" > "${BATCH_PATH}/run_${i}.stdout" &
  pids+=("$!")
done

failures=0
for pid in "${pids[@]}"; do
  if wait "${pid}"; then
    :
  else
    failures=$((failures + 1))
  fi
done

SUMMARY_PATH="${BATCH_PATH}/summary.json"
CLEANUP_PATH="${BATCH_PATH}/cleanup.sh"

python3 - <<'PY' "${BATCH_PATH}" "${SUMMARY_PATH}" "${CLEANUP_PATH}" "${BATCH_ID}" "${RUNTIME_HANDLER}" "${COUNT}"
import json
import math
import pathlib
import sys

batch_path = pathlib.Path(sys.argv[1])
summary_path = pathlib.Path(sys.argv[2])
cleanup_path = pathlib.Path(sys.argv[3])
batch_id = sys.argv[4]
runtime_handler = sys.argv[5]
count = int(sys.argv[6])

results = []
for path in sorted(batch_path.glob("run_*/result.json")):
    results.append(json.loads(path.read_text()))

def nearest_rank(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]

starts = [item["start_ns"] for item in results]
ends = [item["end_ns"] for item in results]
successes = [item for item in results if item.get("sandbox_ready")]
durations = [item["sandbox_core_create_ms"] for item in successes]
pod_ids = [item["pod_id"] for item in successes if item.get("pod_id")]

cleanup_lines = [
    "#!/usr/bin/env bash",
    "set -euo pipefail",
]
for pod_id in pod_ids:
    cleanup_lines.append(f"crictl stopp {pod_id} || true")
    cleanup_lines.append(f"crictl rmp {pod_id} || true")
cleanup_path.write_text("\n".join(cleanup_lines) + "\n")

summary = {
    "batch_id": batch_id,
    "runtime_handler": runtime_handler,
    "count": count,
    "run_count": len(results),
    "success_count": len(successes),
    "failure_count": len(results) - len(successes),
    "batch_start_ns": min(starts) if starts else None,
    "batch_end_ns": max(ends) if ends else None,
    "batch_complete_ms": ((max(ends) - min(starts)) // 1_000_000) if starts and ends else None,
    "sandbox_core_create_ms_min": min(durations) if durations else None,
    "sandbox_core_create_ms_p50": nearest_rank(durations, 50),
    "sandbox_core_create_ms_p95": nearest_rank(durations, 95),
    "sandbox_core_create_ms_p99": nearest_rank(durations, 99),
    "sandbox_core_create_ms_max": max(durations) if durations else None,
    "cleanup_path": str(cleanup_path),
    "runs": results,
}
summary_path.write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
PY

chmod +x "${CLEANUP_PATH}"

echo
echo "summary written to ${SUMMARY_PATH}"
echo "cleanup script: ${CLEANUP_PATH}"

if [[ "${failures}" -ne 0 ]]; then
  echo "warning: ${failures} run(s) failed" >&2
  exit 1
fi
