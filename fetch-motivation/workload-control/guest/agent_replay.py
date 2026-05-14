#!/usr/bin/env python3
"""
agent_replay.py <replay.json> <workspace>

Replays a recorded coding-agent session deterministically.
Each entry in replay.json is one of:

  {"type": "command", "cmd": "...", "cwd": "...", "expected_exit_code": 0}
  {"type": "apply_patch", "ops": [<op>, ...]}

Where each op is one of:

  {"type": "add",    "path": "...", "blocks": [{"type":"add", "lines":[...]}]}
  {"type": "delete", "path": "..."}
  {"type": "update", "path": "...", "hunks": [
      {"header": "@@", "blocks": [
          {"type": "context", "lines": [...]},
          {"type": "add",     "lines": [...]},
          {"type": "delete",  "lines": [...]}
      ]}
  ]}

Commands run via /bin/sh -c with the given cwd.
Patches modify files on disk for real (truly write/delete).

The recorded `cwd` is mapped into <workspace> by stripping its leading '/'.
If the resulting path does not exist, we fall back to <workspace> root.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


# ─── patch application ────────────────────────────────────────

class PatchError(Exception):
    pass


def apply_add(workspace: Path, op: dict[str, Any]) -> None:
    """Create a new file from the lines in 'add' blocks."""
    target = workspace / op["path"]
    target.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for block in op.get("blocks", []):
        if block.get("type") != "add":
            raise PatchError(
                f"unexpected block type {block.get('type')!r} in add op"
            )
        lines.extend(block.get("lines", []))

    content = "\n".join(lines)
    if lines:
        content += "\n"
    target.write_text(content, encoding="utf-8")


def apply_delete(workspace: Path, op: dict[str, Any]) -> None:
    """Delete a file (no-op if it doesn't exist)."""
    target = workspace / op["path"]
    if target.exists() or target.is_symlink():
        target.unlink()


def apply_update(workspace: Path, op: dict[str, Any]) -> None:
    """
    Apply hunks to an existing file.

    Codex-style patches do not carry line numbers — context blocks are the
    locator. For each hunk we build the pre-image (context + delete lines)
    and search for it in the current file content, then splice in the
    post-image (context + add lines).
    """
    target = workspace / op["path"]
    if not target.exists():
        raise PatchError(f"update target does not exist: {target}")

    original = target.read_text(encoding="utf-8")
    had_trailing_newline = original.endswith("\n")
    lines = original.split("\n")
    if had_trailing_newline:
        lines.pop()  # drop the empty string after final \n

    cursor = 0
    for hunk_idx, hunk in enumerate(op.get("hunks", [])):
        cursor = apply_hunk(lines, hunk, cursor, op["path"], hunk_idx)

    new_content = "\n".join(lines)
    if had_trailing_newline and new_content and not new_content.endswith("\n"):
        new_content += "\n"
    elif had_trailing_newline and not new_content:
        new_content = ""
    target.write_text(new_content, encoding="utf-8")


def apply_hunk(
    lines: list[str],
    hunk: dict[str, Any],
    cursor: int,
    path: str,
    hunk_idx: int,
) -> int:
    """Apply one hunk in place. Returns the new cursor (post-hunk index)."""
    blocks = hunk.get("blocks", [])

    pre_image: list[str] = []
    post_image: list[str] = []
    for block in blocks:
        bt = block.get("type")
        blines = block.get("lines", [])
        if bt == "context":
            pre_image.extend(blines)
            post_image.extend(blines)
        elif bt == "delete":
            pre_image.extend(blines)
        elif bt == "add":
            post_image.extend(blines)
        else:
            raise PatchError(f"unknown block type {bt!r}")

    if not pre_image:
        # Pure-add hunk: insert at cursor
        for i, line in enumerate(post_image):
            lines.insert(cursor + i, line)
        return cursor + len(post_image)

    match_idx = find_subsequence(lines, pre_image, cursor)
    if match_idx < 0:
        # Fallback: search from start (some hunks may be reordered)
        match_idx = find_subsequence(lines, pre_image, 0)
    if match_idx < 0:
        raise PatchError(
            f"hunk {hunk_idx} of {path}: cannot locate context "
            f"(first line: {pre_image[0]!r})"
        )

    lines[match_idx : match_idx + len(pre_image)] = post_image
    return match_idx + len(post_image)


def find_subsequence(haystack: list[str], needle: list[str], start: int) -> int:
    """Return index where needle begins in haystack[start:], or -1."""
    if not needle:
        return start
    n = len(needle)
    last = len(haystack) - n
    for i in range(start, last + 1):
        if haystack[i : i + n] == needle:
            return i
    return -1


def apply_patch_entry(workspace: Path, entry: dict[str, Any]) -> None:
    for op in entry.get("ops", []):
        t = op.get("type")
        if t == "add":
            apply_add(workspace, op)
        elif t == "delete":
            apply_delete(workspace, op)
        elif t == "update":
            apply_update(workspace, op)
        else:
            raise PatchError(f"unknown op type: {t!r}")


# ─── command execution ────────────────────────────────────────

def map_cwd(workspace: Path, recorded_cwd: str) -> Path:
    """Map the recorded absolute cwd into the workspace; fallback to root."""
    if not recorded_cwd:
        return workspace
    candidate = workspace / recorded_cwd.lstrip("/")
    return candidate if candidate.is_dir() else workspace


def run_command(workspace: Path, entry: dict[str, Any]) -> None:
    cmd = entry["cmd"]
    cwd = map_cwd(workspace, entry.get("cwd", ""))

    proc = subprocess.run(
        cmd, shell=True, cwd=str(cwd),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # Two ways the trace can express the expected outcome:
    #   1. expected_exit_code: <int>  — exact match required
    #   2. expected_status: "success" | "failed"
    #        "success" → exit code must be 0
    #        "failed"  → exit code must be non-zero
    # If both are present, expected_exit_code takes precedence.
    if "expected_exit_code" in entry:
        expected = entry["expected_exit_code"]
        ok = (proc.returncode == expected)
        msg = f"expected {expected}"
    elif "expected_status" in entry:
        status = entry["expected_status"]
        if status == "success":
            ok  = (proc.returncode == 0)
            msg = "expected success"
        elif status == "failed":
            ok  = (proc.returncode != 0)
            msg = "expected failure"
        else:
            ok  = True   # unknown status — don't second-guess
            msg = f"unknown status {status!r}"
    else:
        ok  = (proc.returncode == 0)
        msg = "expected 0 (default)"

    if not ok:
        sys.stderr.write(
            f"[replay] cmd exit={proc.returncode} ({msg}): {cmd!r}\n"
        )


# ─── main ─────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <replay.json> <workspace>", file=sys.stderr)
        return 2

    replay_path = Path(argv[1])
    workspace   = Path(argv[2]).resolve()

    if not workspace.is_dir():
        print(f"workspace does not exist: {workspace}", file=sys.stderr)
        return 2

    entries = json.loads(replay_path.read_text(encoding="utf-8"))

    n_cmd = n_patch = n_err = 0
    for idx, entry in enumerate(entries):
        et = entry.get("type")
        print(idx, et)
        try:
            if et == "command":
                run_command(workspace, entry)
                n_cmd += 1
            elif et == "apply_patch":
                apply_patch_entry(workspace, entry)
                n_patch += 1
            else:
                sys.stderr.write(f"[replay] entry {idx}: unknown type {et!r}\n")
        except PatchError as e:
            sys.stderr.write(f"[replay] entry {idx}: {e}\n")
            n_err += 1

    print(f"commands={n_cmd} patches={n_patch} errors={n_err}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))