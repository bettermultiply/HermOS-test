#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)
CONFIG_TEMPLATE="${ROOT_DIR}/configs/podsandbox.json"
STATE_DIR="${ROOT_DIR}/state"
RUN_DIR="${STATE_DIR}/runs"
mkdir -p "${RUN_DIR}"

if ! command -v crictl >/dev/null 2>&1; then
  echo "crictl not found" >&2
  exit 1
fi

RUNTIME_HANDLER="${RUNTIME_HANDLER:-kata}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
RUN_PATH="${RUN_DIR}/${RUN_ID}"
mkdir -p "${RUN_PATH}"

CONFIG_PATH="${RUN_PATH}/podsandbox.json"
RESULT_PATH="${RUN_PATH}/result.json"
INSPECT_PATH="${RUN_PATH}/inspectp.json"

python3 - <<'PY' "${CONFIG_TEMPLATE}" "${CONFIG_PATH}" "${RUN_ID}"
import json
import pathlib
import sys

template = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
run_id = sys.argv[3]

cfg = json.loads(template.read_text())
name = f"kata-compare-{run_id}"
cfg["metadata"]["name"] = name
cfg["metadata"]["uid"] = name
cfg["hostname"] = name
cfg["log_directory"] = f"/tmp/kata-compare/{run_id}"
cfg.setdefault("labels", {})
cfg["labels"]["run_id"] = run_id

target.write_text(json.dumps(cfg, indent=2) + "\n")
PY

echo "run_id=${RUN_ID}"
echo "config=${CONFIG_PATH}"

start_ns=$(date +%s%N)
pod_id=$(crictl runp --runtime="${RUNTIME_HANDLER}" "${CONFIG_PATH}")
end_ns=$(date +%s%N)

create_ms=$(( (end_ns - start_ns) / 1000000 ))

crictl inspectp "${pod_id}" > "${INSPECT_PATH}"

python3 - <<'PY' "${RESULT_PATH}" "${RUN_ID}" "${RUNTIME_HANDLER}" "${CONFIG_PATH}" "${INSPECT_PATH}" "${pod_id}" "${start_ns}" "${end_ns}" "${create_ms}"
import json
import pathlib
import sys

result_path = pathlib.Path(sys.argv[1])
run_id = sys.argv[2]
runtime_handler = sys.argv[3]
config_path = sys.argv[4]
inspect_path = sys.argv[5]
pod_id = sys.argv[6]
start_ns = int(sys.argv[7])
end_ns = int(sys.argv[8])
create_ms = int(sys.argv[9])

inspect = json.loads(pathlib.Path(inspect_path).read_text())
state = inspect.get("status", {}).get("state")

result = {
    "run_id": run_id,
    "runtime_handler": runtime_handler,
    "config_path": config_path,
    "inspect_path": inspect_path,
    "pod_id": pod_id,
    "start_ns": start_ns,
    "end_ns": end_ns,
    "sandbox_core_create_ms": create_ms,
    "inspect_state": state,
    "sandbox_ready": bool(pod_id),
    "cleanup_required": True,
}
result_path.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
PY

echo
echo "sandbox is intentionally left running"
echo "manual cleanup:"
echo "  crictl stopp ${pod_id}"
echo "  crictl rmp ${pod_id}"
