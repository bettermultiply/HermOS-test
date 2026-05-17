#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage:
  pull_snapshot.sh [options]

Options:
  --registry URL       Docker Registry base URL. Default: http://localhost:5000
  --repo NAME          Registry repository name. Default: motivation
  --tag TAG            Manifest tag. Default: json
  --out DIR            Output directory. Default: ./pulled-json
  -h, --help           Show this help.

Example:
  ./pull_snapshot.sh --repo motivation --tag json --out ./pulled-json
EOF
}

registry="${REGISTRY:-http://localhost:5000}"
repo="${REPO:-motivation}"
tag="${TAG:-json}"
out_dir="./pulled-json"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --registry)
            registry="$2"
            shift 2
            ;;
        --repo)
            repo="$2"
            shift 2
            ;;
        --tag)
            tag="$2"
            shift 2
            ;;
        --out)
            out_dir="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "missing required command: $1" >&2
        exit 127
    fi
}

need_cmd curl
need_cmd python3
need_cmd sha256sum

registry="${registry%/}"
base_url="$registry/v2/$repo"
mkdir -p "$out_dir"

manifest_file="$out_dir/manifest.json"
layers_file="$out_dir/layers.tsv"

curl -fsS \
    -H "Accept: application/vnd.oci.image.manifest.v1+json" \
    "$base_url/manifests/$tag" \
    -o "$manifest_file"

python3 - "$manifest_file" "$layers_file" <<'PY'
import json
import sys

manifest_path, layers_path = sys.argv[1:3]
with open(manifest_path, "r", encoding="utf-8") as f:
    manifest = json.load(f)

with open(layers_path, "w", encoding="utf-8") as f:
    for i, layer in enumerate(manifest.get("layers", [])):
        digest = layer["digest"]
        size = str(layer["size"])
        annotations = layer.get("annotations") or {}
        title = annotations.get("org.opencontainers.image.title") or f"layer-{i}"
        title = title.replace("/", "_")
        f.write("\t".join([title, digest, size]) + "\n")
PY

while IFS=$'\t' read -r title digest expected_size; do
    target="$out_dir/$title"
    echo "downloading blob: $digest -> $target"
    curl -fL "$base_url/blobs/$digest" -o "$target"

    actual_digest="sha256:$(sha256sum "$target" | awk '{print $1}')"
    if [[ "$actual_digest" != "$digest" ]]; then
        echo "digest mismatch for $target: expected $digest, got $actual_digest" >&2
        exit 1
    fi

    actual_size="$(wc -c < "$target" | tr -d ' ')"
    if [[ "$actual_size" != "$expected_size" ]]; then
        echo "size mismatch for $target: expected $expected_size, got $actual_size" >&2
        exit 1
    fi
done < "$layers_file"

echo "pulled files into: $out_dir"
