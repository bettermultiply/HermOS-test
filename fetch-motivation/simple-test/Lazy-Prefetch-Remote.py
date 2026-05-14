#!/usr/bin/env python3
from __future__ import annotations

import sys


def main() -> int:
    # TODO: Start one uffd handler per sandbox.
    # TODO: Start prefetch before load_snapshot/resume.
    # TODO: Keep the output shape the same as Lazy-Remote.py:
    # snapshot_pull_ms, sandbox_start_ms, workload_run_ms, copied_pages.
    print("Lazy+Prefetch-Remote is intentionally left empty for now.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
