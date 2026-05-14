#!/usr/bin/env bash
# setup.sh — prepare all bench-daemon data files
#
# Designed to run inside a chroot (no systemd/dbus available).
# Safe to re-run (idempotent).

set -euo pipefail

# ── config ────────────────────────────────────────────────────
DATA_DIR="/opt/bench/data"
ASTROPY_DIR="${DATA_DIR}/astropy"
FILELIST_PATH="${DATA_DIR}/rg-filelist.txt"
JSON_PATH="${DATA_DIR}/data.json"
READLIST_PATH="/dev/shm/read-list.bin"
READLIST_SIZE_MB="${READLIST_SIZE_MB:-256}"
JSON_SIZE_MB="${JSON_SIZE_MB:-16}"

BENCH_BIN="/usr/local/bin/bench-daemon"
JSON_TOOL="/opt/bench/json_tool.py"

SERVICE_SRC="./bench-daemon.service"
SERVICE_DST="/etc/systemd/system/bench-daemon.service"
SERVICE_LINK="/etc/systemd/system/multi-user.target.wants/bench-daemon.service"

ASTROPY_GIT="https://github.com/astropy/astropy.git"
ASTROPY_COMMIT="d16bfe05a744909de4b27f5875fe0d4ed41ce607"

# ── helpers ───────────────────────────────────────────────────
info()  { echo "[setup] $*"; }
die()   { echo "[setup] ERROR: $*" >&2; exit 1; }

require_cmd() {
    for cmd in "$@"; do
        command -v "$cmd" &>/dev/null || die "required command not found: $cmd"
    done
}

# ── 0. preflight ───────────────────────────────────────────────
info "checking dependencies..."
require_cmd git python3 rg find sort head

mkdir -p "${DATA_DIR}"

# ── 1. astropy source tree ─────────────────────────────────────
if [ ! -d "${ASTROPY_DIR}/.git" ]; then
    info "cloning astropy and checking out ${ASTROPY_COMMIT}..."
    git clone "${ASTROPY_GIT}" "${ASTROPY_DIR}"
    git -C "${ASTROPY_DIR}" checkout "${ASTROPY_COMMIT}"
else
    info "astropy already present, ensuring correct commit..."
    git -C "${ASTROPY_DIR}" checkout "${ASTROPY_COMMIT}"
fi

# ── 2. rg filelist ────────────────────────────────────────────
info "generating rg-filelist.txt..."
find "${ASTROPY_DIR}" -type f -name '*.py' | sort > "${FILELIST_PATH}"
N_FILES=$(wc -l < "${FILELIST_PATH}")
info "  found ${N_FILES} .py files"
[ "${N_FILES}" -gt 0 ] || die "no .py files found in ${ASTROPY_DIR}"

# ── 3. read-list.bin ──────────────────────────────────────────

# ── 4. JSON data file ─────────────────────────────────────────
info "generating data.json (~${JSON_SIZE_MB}MB)..."
python3 - "${JSON_PATH}" "${JSON_SIZE_MB}" << 'PYEOF'
import sys, json, os, random

path      = sys.argv[1]
target_mb = int(sys.argv[2])
target_b  = target_mb * 1024 * 1024

rng = random.Random(42)

records = {}
i = 0
approx_bytes = 2

while approx_bytes < target_b:
    key = f"record_{i:08d}"
    value = {
        "id":    i,
        "name":  f"item_{rng.randint(0, 999999):06d}",
        "score": round(rng.uniform(0, 100), 4),
        "tags":  [rng.choice(["a","b","c","d","e"]) for _ in range(3)],
        "meta":  {"active": rng.choice([True, False]), "version": rng.randint(1, 10)},
    }
    records[key] = value
    approx_bytes += 120
    i += 1

with open(path, "w") as f:
    json.dump(records, f)

actual = os.path.getsize(path)
print(f"  wrote {actual / 1024 / 1024:.1f}MB ({i} records) to {path}")
PYEOF

# ── 5. install bench-daemon binary ────────────────────────────
if [ -f "./bench-daemon" ]; then
    info "installing bench-daemon binary..."
    install -m 755 ./bench-daemon "${BENCH_BIN}"
else
    info "WARNING: bench-daemon binary not found in current directory"
    info "         run 'make' first, then re-run setup.sh"
fi

# ── 6. install json_tool.py ───────────────────────────────────
if [ -f "./json_tool.py" ]; then
    info "installing json_tool.py..."
    mkdir -p "$(dirname "${JSON_TOOL}")"
    install -m 755 ./json_tool.py "${JSON_TOOL}"
else
    info "WARNING: json_tool.py not found in current directory"
fi

# ── 6b. install agent_replay.py ───────────────────────────────
AGENT_REPLAY="/opt/bench/agent_replay.py"
REPLAY_JSON="/opt/bench/data/replay.json"

if [ -f "./agent_replay.py" ]; then
    info "installing agent_replay.py..."
    install -m 755 ./agent_replay.py "${AGENT_REPLAY}"
else
    info "WARNING: agent_replay.py not found in current directory"
fi

# ── 6c. replay.json (user-provided) ───────────────────────────
# The replay workspace is the astropy clone itself (ASTROPY_DIR).
if [ -f "./replay.json" ]; then
    info "installing replay.json..."
    install -m 644 ./replay.json "${REPLAY_JSON}"
else
    info "NOTE: replay.json not found in current directory"
    info "      provide your own replay.json before running agent-tool-replay"
fi

# ── 7. install systemd service (file-based, no systemctl) ─────
if [ -f "${SERVICE_SRC}" ]; then
    info "installing systemd service files..."
    mkdir -p "$(dirname "${SERVICE_DST}")"
    install -m 644 "${SERVICE_SRC}" "${SERVICE_DST}"

    # Equivalent of `systemctl enable`: create the wants/ symlink manually.
    # systemd will pick this up on next boot without daemon-reload.
    mkdir -p "$(dirname "${SERVICE_LINK}")"
    ln -sf "${SERVICE_DST}" "${SERVICE_LINK}"
    info "  service installed and enabled (will start on boot)"
else
    info "WARNING: ${SERVICE_SRC} not found, skipping service install"
fi

# ── 8. verify ─────────────────────────────────────────────────
info "verifying..."
errors=0

check_file() {
    local f="$1" min_mb="$2"
    if [ ! -f "$f" ]; then
        echo "  MISSING: $f" >&2
        errors=$((errors + 1))
        return
    fi
    local size_mb
    size_mb=$(( $(stat -c%s "$f") / 1024 / 1024 ))
    if [ "$size_mb" -lt "$min_mb" ]; then
        echo "  TOO SMALL: $f (${size_mb}MB < ${min_mb}MB)" >&2
        errors=$((errors + 1))
    else
        echo "  OK: $f (${size_mb}MB)"
    fi
}

check_file "${FILELIST_PATH}"  0
check_file "${JSON_PATH}"      $(( JSON_SIZE_MB - 10 ))
check_file "${READLIST_PATH}"  $(( READLIST_SIZE_MB - 1 ))

if [ "$errors" -gt 0 ]; then
    die "$errors file(s) failed verification"
fi

info "setup complete."
info "  service will auto-start on next boot via the wants/ symlink."
info "  after the VM boots and bench-daemon is running, take the snapshot."