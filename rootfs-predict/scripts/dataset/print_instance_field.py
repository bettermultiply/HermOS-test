#!/usr/bin/env python3
import json
import sys


def main():
    if len(sys.argv) != 3:
        print("usage: print_instance_field.py TASK_JSON FIELD", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        row = json.load(f)
    print(row[sys.argv[2]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
