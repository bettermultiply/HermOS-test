#!/usr/bin/env python3
import json
import os
import socket
import sys
from pathlib import Path


def load_instances(dataset_name, split):
    from datasets import load_dataset, load_from_disk

    dataset_path = Path(dataset_name)
    local_suffixes = {".json", ".jsonl", ".parquet"}
    if dataset_path.suffix in local_suffixes and not dataset_path.exists():
        raise FileNotFoundError(f"local dataset file does not exist: {dataset_path}")

    if dataset_path.exists():
        if dataset_path.is_dir():
            split_path = dataset_path / split
            if split_path.exists():
                return load_from_disk(str(split_path))
            return load_from_disk(str(dataset_path))
        if dataset_path.suffix == ".json":
            return json.loads(dataset_path.read_text(encoding="utf-8"))
        if dataset_path.suffix == ".jsonl":
            return [
                json.loads(line)
                for line in dataset_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        if dataset_path.suffix == ".parquet":
            return load_dataset("parquet", data_files=str(dataset_path), split="train")

    return load_dataset(dataset_name, split=split)


def is_network_error(exc):
    text = str(exc)
    return any(
        marker in text
        for marker in (
            "Network is unreachable",
            "Name or service not known",
            "Temporary failure in name resolution",
            "Cannot send a request, as the client has been closed",
        )
    )


def print_dataset_load_error(dataset_name, split, exc):
    print(
        f"failed to load dataset {dataset_name!r} split {split!r}: {exc}",
        file=sys.stderr,
    )
    if isinstance(exc, FileNotFoundError):
        print(
            "Set DATASET_NAME to an existing local .json/.jsonl/.parquet file, "
            "a datasets.save_to_disk directory, or a Hugging Face dataset name.",
            file=sys.stderr,
        )
    elif is_network_error(exc):
        print(
            "\nThis run needs the SWE-bench dataset on the host before the VM starts. "
            "The default DATASET_NAME uses Hugging Face, but the host has no network "
            "route right now.",
            file=sys.stderr,
        )
        print(
            "Fix by enabling host network access, pre-populating the Hugging Face "
            "cache, or pointing DATASET_NAME at a local .json/.jsonl/.parquet file "
            "or a datasets.save_to_disk directory.",
            file=sys.stderr,
        )


def main():
    if len(sys.argv) != 5:
        print("usage: get_instance.py DATASET_NAME SPLIT INSTANCE_ID OUT_DIR", file=sys.stderr)
        return 2

    dataset_name, split, instance_id, out_dir = sys.argv[1:]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if os.environ.get("HF_DATASETS_OFFLINE") == "1" and not Path(dataset_name).exists():
        print(
            f"HF_DATASETS_OFFLINE=1 is set but DATASET_NAME is not a local path: {dataset_name}",
            file=sys.stderr,
        )
        return 1

    try:
        dataset = load_instances(dataset_name, split)
    except (ConnectionError, OSError, RuntimeError, socket.gaierror) as exc:
        print_dataset_load_error(dataset_name, split, exc)
        return 1

    matches = [row for row in dataset if row["instance_id"] == instance_id]
    if not matches:
        print(f"instance not found: {instance_id}", file=sys.stderr)
        return 1

    row = dict(matches[0])
    (out / "task.json").write_text(json.dumps(row, indent=2), encoding="utf-8")

    prompt = "\n".join(
        [
            "Solve this SWE-bench task in the checked-out repository.",
            "",
            f"Instance: {row['instance_id']}",
            f"Repository: {row['repo']}",
            f"Base commit: {row['base_commit']}",
            "",
            "Problem:",
            row["problem_statement"],
            "",
            "Modify the repository to fix the problem.",
        ]
    )
    (out / "prompt.txt").write_text(prompt, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
