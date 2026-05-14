#!/usr/bin/env bash

set -euo pipefail

READLIST_PATH="/dev/shm/read-list.bin"
READLIST_SIZE_MB="${READLIST_SIZE_MB:-128}"

info()  { echo "[setup] $*"; }

info "generating read-list.bin (${READLIST_SIZE_MB}MB, deterministic LCG)..."
python3 - "${READLIST_PATH}" "${READLIST_SIZE_MB}" << 'PYEOF'
import sys
path  = sys.argv[1]
size  = int(sys.argv[2]) * 1024 * 1024
CHUNK = 65536

state   = 12345678
A, C, M = 1664525, 1013904223, 2**32

with open(path, "wb") as f:
    written = 0
    while written < size:
        n = min(CHUNK, size - written)
        buf = bytearray(n)
        for i in range(n):
            state = (A * state + C) & (M - 1)
            buf[i] = state >> 24
        f.write(buf)
        written += n
print(f"  wrote {written} bytes to {path}")
PYEOF