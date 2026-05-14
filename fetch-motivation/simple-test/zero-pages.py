#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


PAGE_SIZE = 4096


def scan(src: Path, dense: Path | None) -> dict[str, float | int | str]:
    zero = b"\0" * PAGE_SIZE
    total_pages = 0
    zero_pages = 0

    out = dense.open("wb") if dense else None
    try:
        with src.open("rb") as f:
            while True:
                page = f.read(PAGE_SIZE)
                if not page:
                    break
                total_pages += 1
                if page == zero:
                    zero_pages += 1
                elif out:
                    out.write(page)
    finally:
        if out:
            out.close()

    dense_pages = total_pages - zero_pages
    return {
        "snapshot": str(src),
        "page_size": PAGE_SIZE,
        "total_pages": total_pages,
        "zero_pages": zero_pages,
        "dense_pages": dense_pages,
        "zero_ratio": zero_pages / total_pages if total_pages else 0,
        "dense_path": str(dense) if dense else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--dense", type=Path)
    args = parser.parse_args()

    print(json.dumps(scan(args.snapshot, args.dense), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
