#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FC_SRC="${FC_SRC:-$ROOT/../firecracker-build-scripts/firecracker_src}"
TARGET_DIR="${CARGO_TARGET_DIR:-/tmp/rootfs-predict-fc-target}"

mkdir -p "$ROOT/bin"

(
    cd "$FC_SRC"
    CARGO_TARGET_DIR="$TARGET_DIR" cargo build --release --bin firecracker --offline
)

cp "$TARGET_DIR/release/firecracker" "$ROOT/bin/firecracker-block-trace"
