#!/usr/bin/env python3
"""Extract replayable Codex tool steps from rollout JSONL files."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOOL_OUTPUT_EXIT_RE = re.compile(r"(?:Process exited with code|\"exit_code\")[: ]+(-?\d+)")


@dataclass(frozen=True)
class JsonlEvent:
    source: Path
    line_no: int
    data: dict[str, Any]


def load_jsonl(paths: list[Path]) -> list[JsonlEvent]:
    events: list[JsonlEvent] = []
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc
                if isinstance(data, dict):
                    events.append(JsonlEvent(source=path, line_no=line_no, data=data))
    return events


def default_paths(args_paths: list[str]) -> list[Path]:
    if args_paths:
        paths = [Path(item) for item in args_paths]
    else:
        paths = sorted(Path.cwd().glob("*.jsonl"))
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SystemExit(f"missing input file(s): {', '.join(missing)}")
    if not paths:
        raise SystemExit("no JSONL files found; pass paths explicitly or run in a JSONL directory")
    return paths


def payload_of(event: JsonlEvent) -> dict[str, Any]:
    payload = event.data.get("payload")
    return payload if isinstance(payload, dict) else {}


def parse_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def session_cwd(events: list[JsonlEvent]) -> str | None:
    for event in events:
        if event.data.get("type") != "session_meta":
            continue
        payload = payload_of(event)
        cwd = payload.get("cwd")
        if isinstance(cwd, str):
            return cwd
    return None


def output_index(events: list[JsonlEvent]) -> dict[str, dict[str, Any]]:
    outputs: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.data.get("type") != "response_item":
            continue
        payload = payload_of(event)
        payload_type = payload.get("type")
        if payload_type not in {"function_call_output", "custom_tool_call_output"}:
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str):
            continue
        outputs[call_id] = normalize_output(payload.get("output"), event)
    return outputs


def normalize_output(raw: Any, event: JsonlEvent) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": str(event.source),
        "line": event.line_no,
        "timestamp": event.data.get("timestamp"),
    }
    if raw is None:
        return result

    result["raw"] = raw
    parsed = parse_json_object(raw)
    if parsed:
        text = parsed.get("output")
        metadata = parsed.get("metadata")
        if isinstance(text, str):
            result["text"] = text
        if isinstance(metadata, dict):
            result["metadata"] = metadata
            exit_code = metadata.get("exit_code")
            if isinstance(exit_code, int):
                result["exit_code"] = exit_code
        return result

    if isinstance(raw, str):
        match = TOOL_OUTPUT_EXIT_RE.search(raw)
        if match:
            result["exit_code"] = int(match.group(1))
        result["text"] = raw
    return result


def tool_short_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    return name.rsplit(".", 1)[-1]


def changed_files_from_patch(patch: str) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    current_update: str | None = None
    for line in patch.splitlines():
        if line.startswith("*** Update File: "):
            current_update = line.removeprefix("*** Update File: ").strip()
            files.append({"op": "update", "path": current_update})
        elif line.startswith("*** Add File: "):
            current_update = None
            files.append({"op": "add", "path": line.removeprefix("*** Add File: ").strip()})
        elif line.startswith("*** Delete File: "):
            current_update = None
            files.append({"op": "delete", "path": line.removeprefix("*** Delete File: ").strip()})
        elif line.startswith("*** Move to: "):
            new_path = line.removeprefix("*** Move to: ").strip()
            files.append({"op": "move", "from": current_update or "", "path": new_path})
    return files


def parse_patch(patch: str) -> list[dict[str, Any]]:
    def append_block(hunk: dict[str, Any], kind: str, text: str) -> None:
        blocks = hunk["blocks"]
        if blocks and blocks[-1]["type"] == kind:
            blocks[-1]["lines"].append(text)
        else:
            blocks.append({"type": kind, "lines": [text]})

    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch":
        raise ValueError("patch must start with '*** Begin Patch'")
    if lines[-1] != "*** End Patch":
        raise ValueError("patch must end with '*** End Patch'")

    ops: list[dict[str, Any]] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if line.startswith("*** Update File: "):
            path = line.removeprefix("*** Update File: ").strip()
            op: dict[str, Any] = {"type": "update", "path": path, "hunks": []}
            i += 1
            if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                op["move_to"] = lines[i].removeprefix("*** Move to: ").strip()
                i += 1

            current_hunk: dict[str, Any] | None = None
            while i < len(lines) - 1:
                line = lines[i]
                if line.startswith("*** Update File: ") or line.startswith("*** Add File: ") or line.startswith(
                    "*** Delete File: "
                ):
                    break
                if line == "*** End of File":
                    op["end_of_file"] = True
                    i += 1
                    continue
                if line.startswith("@@"):
                    current_hunk = {"header": line, "blocks": []}
                    op["hunks"].append(current_hunk)
                    i += 1
                    continue
                if line[:1] in {" ", "+", "-"}:
                    if current_hunk is None:
                        current_hunk = {"header": "@@", "blocks": []}
                        op["hunks"].append(current_hunk)
                    kind = {"+": "add", "-": "delete", " ": "context"}[line[0]]
                    append_block(current_hunk, kind, line[1:])
                    i += 1
                    continue
                raise ValueError(f"unexpected patch line: {line}")
            ops.append(op)
            continue

        if line.startswith("*** Add File: "):
            path = line.removeprefix("*** Add File: ").strip()
            op = {"type": "add", "path": path, "blocks": []}
            i += 1
            while i < len(lines) - 1:
                line = lines[i]
                if line.startswith("*** Update File: ") or line.startswith("*** Add File: ") or line.startswith(
                    "*** Delete File: "
                ):
                    break
                if line == "*** End of File":
                    op["end_of_file"] = True
                    i += 1
                    continue
                if not line.startswith("+"):
                    raise ValueError(f"unexpected add-file line: {line}")
                blocks = op["blocks"]
                if blocks and blocks[-1]["type"] == "add":
                    blocks[-1]["lines"].append(line[1:])
                else:
                    blocks.append({"type": "add", "lines": [line[1:]]})
                i += 1
            ops.append(op)
            continue

        if line.startswith("*** Delete File: "):
            path = line.removeprefix("*** Delete File: ").strip()
            ops.append({"type": "delete", "path": path})
            i += 1
            continue

        raise ValueError(f"unexpected patch header: {line}")

    return ops


def status_from_output(output: dict[str, Any] | None) -> str:
    if not output:
        return "unknown"
    exit_code = output.get("exit_code")
    if isinstance(exit_code, int):
        return "completed" if exit_code == 0 else "failed"
    text = output.get("text")
    if isinstance(text, str):
        if "Success." in text:
            return "completed"
        if "failed" in text.lower() or "error" in text.lower():
            return "failed"
    return "completed"


def compact_output(output: dict[str, Any] | None, include_output: bool) -> dict[str, Any] | None:
    if not output:
        return None
    keep = {key: value for key, value in output.items() if key in {"line", "timestamp", "exit_code", "metadata"}}
    if include_output:
        keep["raw"] = output.get("raw")
        keep["text"] = output.get("text")
    return keep


def make_command_action(
    event: JsonlEvent,
    call_id: str,
    name: str,
    args: dict[str, Any],
    output: dict[str, Any] | None,
    cwd: str | None,
    include_output: bool,
) -> dict[str, Any]:
    workdir = args.get("workdir")
    action = {
        "kind": "command",
        "source": str(event.source),
        "line": event.line_no,
        "timestamp": event.data.get("timestamp"),
        "call_id": call_id,
        "tool": name,
        "cwd": workdir if isinstance(workdir, str) else cwd,
        "command": args.get("cmd"),
        "arguments": args,
        "status": status_from_output(output),
    }
    exit_code = output.get("exit_code") if output else None
    if isinstance(exit_code, int):
        action["exit_code"] = exit_code
    packed_output = compact_output(output, include_output)
    if packed_output is not None:
        action["output"] = packed_output
    return action


def make_patch_action(
    event: JsonlEvent,
    call_id: str,
    name: str,
    patch: str,
    output: dict[str, Any] | None,
    cwd: str | None,
    include_output: bool,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "kind": "apply_patch",
        "source": str(event.source),
        "line": event.line_no,
        "timestamp": event.data.get("timestamp"),
        "call_id": call_id,
        "tool": name,
        "cwd": cwd,
        "changed_files": changed_files_from_patch(patch),
        "patch": patch,
        "status": status_from_output(output),
    }
    packed_output = compact_output(output, include_output)
    if packed_output is not None:
        action["output"] = packed_output
    return action


def extract_actions(events: list[JsonlEvent], include_output: bool = False) -> list[dict[str, Any]]:
    cwd = session_cwd(events)
    outputs = output_index(events)
    actions: list[dict[str, Any]] = []

    for event in events:
        if event.data.get("type") != "response_item":
            continue
        payload = payload_of(event)
        payload_type = payload.get("type")
        call_id = payload.get("call_id")
        if not isinstance(call_id, str):
            continue

        if payload_type == "function_call":
            name = tool_short_name(payload.get("name"))
            args = parse_json_object(payload.get("arguments"))
            if name == "exec_command" and isinstance(args.get("cmd"), str):
                actions.append(
                    make_command_action(
                        event=event,
                        call_id=call_id,
                        name=name,
                        args=args,
                        output=outputs.get(call_id),
                        cwd=cwd,
                        include_output=include_output,
                    )
                )
            elif name == "parallel":
                actions.extend(extract_parallel_actions(event, call_id, args, outputs, cwd, include_output))

        elif payload_type == "custom_tool_call":
            name = tool_short_name(payload.get("name"))
            patch = payload.get("input")
            if name == "apply_patch" and isinstance(patch, str):
                actions.append(
                    make_patch_action(
                        event=event,
                        call_id=call_id,
                        name=name,
                        patch=patch,
                        output=outputs.get(call_id),
                        cwd=cwd,
                        include_output=include_output,
                    )
                )

    for index, action in enumerate(actions, 1):
        action["index"] = index
    return actions


def extract_parallel_actions(
    event: JsonlEvent,
    call_id: str,
    args: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    cwd: str | None,
    include_output: bool,
) -> list[dict[str, Any]]:
    tool_uses = args.get("tool_uses")
    if not isinstance(tool_uses, list):
        return []
    actions: list[dict[str, Any]] = []
    parent_output = outputs.get(call_id)
    for sub_index, item in enumerate(tool_uses, 1):
        if not isinstance(item, dict):
            continue
        name = tool_short_name(item.get("recipient_name"))
        params = item.get("parameters")
        if name == "exec_command" and isinstance(params, dict) and isinstance(params.get("cmd"), str):
            action = make_command_action(event, call_id, name, params, parent_output, cwd, include_output)
            action["parallel_parent_call_id"] = call_id
            action["parallel_index"] = sub_index
            actions.append(action)
    return actions


def successful_only(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [action for action in actions if action.get("status") != "failed"]


def clean_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for action in actions:
        cwd = action.get("cwd")
        if action["kind"] == "command":
            cmd = action.get("command")
            if isinstance(cmd, str):
                item: dict[str, Any] = {"type": "command", "cmd": cmd}
                if isinstance(cwd, str) and cwd:
                    item["cwd"] = cwd
                exit_code = action.get("exit_code")
                if isinstance(exit_code, int):
                    item["expected_exit_code"] = exit_code
                status = action.get("status")
                if status == "failed" and "expected_exit_code" not in item:
                    item["expected_status"] = "failed"
                cleaned.append(item)
        elif action["kind"] == "apply_patch":
            patch = action.get("patch")
            if isinstance(patch, str):
                item = {"type": "apply_patch", "ops": parse_patch(patch)}
                if isinstance(cwd, str) and cwd:
                    item["cwd"] = cwd
                if action.get("status") == "failed":
                    item["expected_status"] = "failed"
                cleaned.append(item)
    return cleaned


def emit_json(actions: list[dict[str, Any]], out: Any) -> None:
    json.dump(actions, out, ensure_ascii=False, indent=2)
    out.write("\n")


def emit_jsonl(actions: list[dict[str, Any]], out: Any) -> None:
    for action in actions:
        out.write(json.dumps(action, ensure_ascii=False, sort_keys=True) + "\n")


def emit_markdown(actions: list[dict[str, Any]], out: Any) -> None:
    out.write("# Codex Replay Steps\n\n")
    for action in actions:
        status = action.get("status", "unknown")
        cwd = action.get("cwd") or "."
        if action["kind"] == "command":
            out.write(f"## {action['index']}. command ({status})\n\n")
            out.write(f"- source: `{action['source']}:{action['line']}`\n")
            out.write(f"- cwd: `{cwd}`\n")
            if "exit_code" in action:
                out.write(f"- exit_code: `{action['exit_code']}`\n")
            out.write("\n```bash\n")
            out.write(str(action.get("command", "")))
            out.write("\n```\n\n")
        elif action["kind"] == "apply_patch":
            out.write(f"## {action['index']}. apply_patch ({status})\n\n")
            out.write(f"- source: `{action['source']}:{action['line']}`\n")
            files = ", ".join(
                f"{item.get('op')}:{item.get('path')}" for item in action.get("changed_files", [])
            )
            out.write(f"- files: `{files}`\n\n")
            out.write("```patch\n")
            out.write(str(action.get("patch", "")))
            out.write("\n```\n\n")


def shell_quote(value: Any) -> str:
    return shlex.quote(str(value))


def emit_shell(actions: list[dict[str, Any]], out: Any) -> None:
    out.write("#!/usr/bin/env bash\n")
    out.write("set -u\n\n")
    out.write("# Generated from Codex rollout JSONL. Commands are checked against recorded exit codes when available.\n")
    out.write("# Patch steps require an `apply_patch` command compatible with Codex patch syntax.\n\n")
    for action in actions:
        index = action["index"]
        status = action.get("status", "unknown")
        cwd = action.get("cwd") or "."
        out.write(f"printf '\\n[{index}] {action['kind']} ({status})\\n'\n")
        out.write("(\n")
        out.write(f"  cd -- {shell_quote(cwd)}\n")
        if action["kind"] == "command":
            command = action.get("command") or ""
            expected = action.get("exit_code")
            out.write("  set +e\n")
            out.write(f"  bash -lc {shell_quote(command)}\n")
            out.write("  rc=$?\n")
            if isinstance(expected, int):
                out.write(f"  if [ \"$rc\" -ne {expected} ]; then\n")
                out.write(f"    echo \"step {index}: expected exit code {expected}, got $rc\" >&2\n")
                out.write("    exit \"$rc\"\n")
                out.write("  fi\n")
            else:
                out.write("  if [ \"$rc\" -ne 0 ]; then exit \"$rc\"; fi\n")
        elif action["kind"] == "apply_patch":
            marker = f"PATCH_STEP_{index}"
            out.write("  if ! command -v apply_patch >/dev/null 2>&1; then\n")
            out.write("    echo \"apply_patch command not found\" >&2\n")
            out.write("    exit 127\n")
            out.write("  fi\n")
            out.write("  set +e\n")
            out.write(f"  apply_patch <<'{marker}'\n")
            out.write(str(action.get("patch", "")))
            out.write(f"\n{marker}\n")
            out.write("  rc=$?\n")
            expected = 0 if status == "completed" else None
            if expected is not None:
                out.write(f"  if [ \"$rc\" -ne {expected} ]; then\n")
                out.write(f"    echo \"step {index}: expected exit code {expected}, got $rc\" >&2\n")
                out.write("    exit \"$rc\"\n")
                out.write("  fi\n")
        out.write(")\n")


def write_output(path: str | None, writer: Any, actions: list[dict[str, Any]]) -> None:
    if path:
        with Path(path).open("w", encoding="utf-8") as out:
            writer(actions, out)
    else:
        writer(actions, sys.stdout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse Codex rollout JSONL and extract ordered exec_command/apply_patch replay steps."
    )
    parser.add_argument("paths", nargs="*", help="JSONL files. Defaults to all *.jsonl in the current directory.")
    parser.add_argument(
        "--format",
        choices=["json", "jsonl", "markdown", "shell"],
        default="json",
        help="Output format.",
    )
    parser.add_argument("-o", "--output", help="Write output to this path instead of stdout.")
    parser.add_argument("--include-output", action="store_true", help="Include raw tool outputs in JSON/JSONL.")
    parser.add_argument("--successful-only", action="store_true", help="Omit steps whose recorded status is failed.")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Emit only replay-required fields and preserve expected outcomes, including failed steps.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    paths = default_paths(args.paths)
    events = load_jsonl(paths)
    actions = extract_actions(events, include_output=args.include_output)
    if args.successful_only:
        actions = successful_only(actions)
        for index, action in enumerate(actions, 1):
            action["index"] = index
    if args.clean:
        actions = clean_actions(actions)

    writers = {
        "json": emit_json,
        "jsonl": emit_jsonl,
        "markdown": emit_markdown,
        "shell": emit_shell,
    }
    write_output(args.output, writers[args.format], actions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
