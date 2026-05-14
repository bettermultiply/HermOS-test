#!/usr/bin/env bash
# cleanup.sh — undo everything setup.sh did
#
# Designed to run inside a chroot (no systemd/dbus available).
# Safe to re-run (idempotent).

set -euo pipefail

# ── config (must match setup.sh) ──────────────────────────────
DATA_DIR="/opt/bench/data"
BENCH_ROOT="/opt/bench"
READLIST_PATH="/dev/shm/read-list.bin"

BENCH_BIN="/usr/local/bin/bench-daemon"
JSON_TOOL="/opt/bench/json_tool.py"

SERVICE_DST="/etc/systemd/system/bench-daemon.service"
SERVICE_LINK="/etc/systemd/system/multi-user.target.wants/bench-daemon.service"

# ── helpers ───────────────────────────────────────────────────
info() { echo "[cleanup] $*"; }

remove_path() {
    local p="$1"
    if [ -e "$p" ] || [ -L "$p" ]; then
        rm -rf -- "$p"
        info "  removed: $p"
    else
        info "  skip (not present): $p"
    fi
}

# ── 1. systemd service files ──────────────────────────────────
info "removing systemd service files..."
remove_path "${SERVICE_LINK}"
remove_path "${SERVICE_DST}"

# ── 2. installed binaries / scripts ───────────────────────────
info "removing installed binaries..."
remove_path "${BENCH_BIN}"
remove_path "${JSON_TOOL}"

# ── 3. data files ─────────────────────────────────────────────
info "removing data files..."
remove_path "${READLIST_PATH}"
remove_path "${DATA_DIR}"

# ── 4. /opt/bench (only if empty after removing data) ─────────
if [ -d "${BENCH_ROOT}" ]; then
    if [ -z "$(ls -A "${BENCH_ROOT}" 2>/dev/null)" ]; then
        rmdir "${BENCH_ROOT}"
        info "  removed empty: ${BENCH_ROOT}"
    else
        info "  keeping ${BENCH_ROOT} (not empty)"
    fi
fi

info "cleanup complete."