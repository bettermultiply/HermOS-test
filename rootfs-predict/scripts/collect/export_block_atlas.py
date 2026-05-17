#!/usr/bin/env python3
import csv
import re
import subprocess
import sys
from collections import deque
from pathlib import Path, PurePosixPath


GROUP_RE = re.compile(r"^Group \d+: \(Blocks (\d+)-(\d+)\)")
SINGLE_RE = re.compile(r"^\s+(Primary superblock|Backup superblock) at (\d+)")
RANGE_RE = re.compile(r"^\s+(Group descriptors|Reserved GDT blocks|Inode table) at (\d+)-(\d+)")
ITEM_RE = re.compile(r"^\s+(Block bitmap|Inode bitmap) at (\d+)")
STAT_RE = re.compile(r"^Inode:\s+(\d+)\s+Type:\s+(\w+)")


def run_text(cmd, input_text=None):
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
    ).stdout


def debugfs(image_path, command):
    return run_text(["debugfs", "-R", command, str(image_path)]).splitlines()


def debugfs_quote(path):
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def split_ls_line(line):
    parts = line.strip().strip("/").split("/")
    if len(parts) < 5:
        return None
    inode, mode, _uid, _gid, name = parts[:5]
    try:
        return int(inode), mode, name
    except ValueError:
        return None


def guest_path(path_prefix, image_root, image_path):
    rel = PurePosixPath("/") if image_path == image_root else PurePosixPath(image_path).relative_to(image_root)
    base = PurePosixPath(path_prefix)
    return str((base / rel) if str(base) != "/" else (PurePosixPath("/") / rel)).replace("//", "/")


def should_skip(path, skip_prefixes):
    return any(path == prefix or path.startswith(prefix + "/") for prefix in skip_prefixes)


def walk_entries(image_path, image_root, path_prefix, skip_prefixes):
    queue = deque([image_root])
    entries = []
    seen_dirs = set()

    while queue:
        current = queue.popleft()
        if current in seen_dirs:
            continue
        seen_dirs.add(current)
        for line in debugfs(image_path, f"ls -p {debugfs_quote(current)}"):
            parsed = split_ls_line(line)
            if parsed is None:
                continue
            inode, mode, name = parsed
            if name in {".", ".."}:
                continue
            child = str(PurePosixPath(current) / name)
            path = guest_path(path_prefix, image_root, child)
            if should_skip(path, skip_prefixes):
                continue
            kind = None
            if mode.startswith("04"):
                kind = "dir"
                queue.append(child)
            elif mode.startswith("10"):
                kind = "file"
            else:
                continue
            entries.append((inode, path, kind))
    return entries


def parse_extent_line(line):
    text = line.strip()
    if not text.startswith("(") or "):" not in text:
        return None
    logical_text, physical_text = text.split("):", 1)
    logical_text = logical_text[1:]
    physical_text = physical_text.strip().split()[0]
    if "-" in logical_text:
        logical_start, logical_end = logical_text.split("-", 1)
    else:
        logical_start = logical_end = logical_text
    if "-" in physical_text:
        physical_start, physical_end = physical_text.split("-", 1)
    else:
        physical_start = physical_end = physical_text
    try:
        return int(logical_start), int(logical_end), int(physical_start), int(physical_end)
    except ValueError:
        return None


def load_inode_stats(image_path, inodes):
    if not inodes:
        return {}
    commands = [f"stat <{inode}>" for inode in sorted(inodes)]
    output = run_text(["debugfs", "-f", "-", str(image_path)], input_text="\n".join(commands) + "\n")
    stats = {}
    current = None
    collecting = False

    for line in output.splitlines():
        if line.startswith("debugfs: stat <") and line.endswith(">"):
            current = int(line[len("debugfs: stat <"):-1])
            stats[current] = {"type": "", "extents": []}
            collecting = False
            continue
        if current is None:
            continue
        match = STAT_RE.match(line.strip())
        if match:
            stats[current]["type"] = match.group(2).lower()
            continue
        if line.strip() == "EXTENTS:":
            collecting = True
            continue
        if collecting:
            parsed = parse_extent_line(line)
            if parsed is not None:
                stats[current]["extents"].append(parsed)
    return stats


def iter_range(start, end):
    for block_id in range(start, end + 1):
        yield block_id


def mark_range(rows_by_block, drive_id, block_start, block_end, block_class, path="", inode="", logical_block=""):
    for block_id in iter_range(block_start, block_end):
        if block_id not in rows_by_block:
            rows_by_block[block_id] = {
                "drive_id": drive_id,
                "block_id": str(block_id),
                "class": block_class,
                "path": path,
                "inode": inode,
                "logical_block": logical_block if logical_block != "" else "",
            }


def add_shared_metadata(rows_by_block, image_path, drive_id):
    text = run_text(["dumpe2fs", str(image_path)])
    for line in text.splitlines():
        line = line.rstrip()
        match = GROUP_RE.match(line)
        if match:
            continue
        match = SINGLE_RE.match(line)
        if match:
            label = "superblock_or_gdt"
            mark_range(rows_by_block, drive_id, int(match.group(2)), int(match.group(2)), label)
            continue
        match = RANGE_RE.match(line)
        if match:
            label = "superblock_or_gdt" if match.group(1) != "Inode table" else "inode_table"
            mark_range(rows_by_block, drive_id, int(match.group(2)), int(match.group(3)), label)
            continue
        match = ITEM_RE.match(line)
        if match:
            label = "block_bitmap" if match.group(1) == "Block bitmap" else "inode_bitmap"
            mark_range(rows_by_block, drive_id, int(match.group(2)), int(match.group(2)), label)


def add_entries(rows_by_block, entries, stats, drive_id):
    for inode, path, kind in entries:
        info = stats.get(inode)
        if info is None:
            continue
        block_class = "dir_data" if kind == "dir" else "file_data"
        for logical_start, logical_end, phys_start, phys_end in info["extents"]:
            for offset, block_id in enumerate(iter_range(phys_start, phys_end)):
                rows_by_block[block_id] = {
                    "drive_id": drive_id,
                    "block_id": str(block_id),
                    "class": block_class,
                    "path": path,
                    "inode": str(inode),
                    "logical_block": str(logical_start + offset),
                }


def add_journal(rows_by_block, stats, drive_id):
    journal = stats.get(8)
    if journal is None:
        return
    for _logical_start, _logical_end, phys_start, phys_end in journal["extents"]:
        mark_range(rows_by_block, drive_id, phys_start, phys_end, "journal")


def write_rows(output_path, rows_by_block):
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["drive_id", "block_id", "class", "path", "inode", "logical_block"],
        )
        writer.writeheader()
        for block_id in sorted(rows_by_block):
            writer.writerow(rows_by_block[block_id])


def main():
    if len(sys.argv) < 6:
        print(
            "usage: export_block_atlas.py IMAGE_PATH OUTPUT_CSV DRIVE_ID PATH_PREFIX IMAGE_ROOT [SKIP_PREFIX ...]",
            file=sys.stderr,
        )
        return 2

    image_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    drive_id = sys.argv[3]
    path_prefix = sys.argv[4]
    image_root = str(PurePosixPath(sys.argv[5]))
    skip_prefixes = tuple(sorted({str(PurePosixPath(item)) for item in sys.argv[6:]}))

    entries = walk_entries(image_path, image_root, path_prefix, skip_prefixes)
    inodes = {inode for inode, _path, _kind in entries}
    if drive_id == "rootfs":
        inodes.add(8)
    stats = load_inode_stats(image_path, inodes)

    rows_by_block = {}
    add_shared_metadata(rows_by_block, image_path, drive_id)
    if drive_id == "rootfs":
        add_journal(rows_by_block, stats, drive_id)
    add_entries(rows_by_block, entries, stats, drive_id)
    write_rows(output_path, rows_by_block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
