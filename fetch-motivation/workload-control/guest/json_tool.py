#!/usr/bin/env python3
"""
json_tool.py <path>

Reads a JSON file from disk, parses it, and counts total keys
across all objects. Tests large sequential disk read + CPU (JSON parse).
"""
import json
import sys


def count_keys(obj) -> int:
    """Recursively count all dict keys in the object."""
    if isinstance(obj, dict):
        total = len(obj)
        for v in obj.values():
            total += count_keys(v)
        return total
    if isinstance(obj, list):
        return sum(count_keys(item) for item in obj)
    return 0


def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <json-file>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    with open(path, "r") as f:
        data = json.load(f)

    total_keys = count_keys(data)
    print(f"keys={total_keys}")


if __name__ == "__main__":
    main()