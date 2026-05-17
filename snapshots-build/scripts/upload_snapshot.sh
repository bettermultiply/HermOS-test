#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage:
  upload_snapshot.sh [options]

Options:
  --registry URL       Docker Registry base URL. Default: http://localhost:5000
  --repo NAME          Registry repository name. Default: motivation
  --tag TAG            Manifest tag. Default: json
  --mem PATH           Memory file path. Default: ../json_mem_file
  --snapshot PATH      Snapshot file path. Default: ../json_snapshot_file
  -h, --help           Show this help.

Example:
  ./upload_snapshot.sh --repo motivation --tag json \
    --mem ../json_mem_file --snapshot ../json_snapshot_file
EOF
}

registry="${REGISTRY:-http://localhost:5000}"
repo="${REPO:-motivation}"
tag="${TAG:-json}"
mem_file="../json_mem_file"
snapshot_file="../json_snapshot_file"

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
        --mem)
            mem_file="$2"
            shift 2
            ;;
        --snapshot)
            snapshot_file="$2"
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
need_cmd stat

if [[ ! -f "$mem_file" ]]; then
    echo "memory file not found: $mem_file" >&2
    exit 1
fi

if [[ ! -f "$snapshot_file" ]]; then
    echo "snapshot file not found: $snapshot_file" >&2
    exit 1
fi

registry="${registry%/}"
base_url="$registry/v2/$repo"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

digest_of() {
    sha256sum "$1" | awk '{print "sha256:" $1}'
}

size_of() {
    stat -c '%s' "$1"
}

upload_blob() {
    local file="$1"
    local digest="$2"
    local headers="$tmpdir/upload.headers"
    local location
    local upload_url
    local sep

    if curl -fsS -I "$base_url/blobs/$digest" >/dev/null 2>&1; then
        echo "blob exists: $digest"
        return
    fi

    curl -fsS -X POST -D "$headers" -o /dev/null "$base_url/blobs/uploads/"
    location="$(awk 'tolower($1) == "location:" {print $2}' "$headers" | tail -n 1 | tr -d '\r')"

    if [[ -z "$location" ]]; then
        echo "registry did not return a Location header" >&2
        cat "$headers" >&2
        exit 1
    fi

    case "$location" in
        http://*|https://*)
            upload_url="$location"
            ;;
        /*)
            upload_url="$registry$location"
            ;;
        *)
            upload_url="$registry/$location"
            ;;
    esac

    if [[ "$upload_url" == *\?* ]]; then
        sep='&'
    else
        sep='?'
    fi

    echo "uploading blob: $file -> $digest"
    curl -fsS -X PUT \
        -H "Content-Type: application/octet-stream" \
        --upload-file - \
        -o /dev/null \
        "${upload_url}${sep}digest=$digest" \
        < "$file"
}

mem_digest="$(digest_of "$mem_file")"
mem_size="$(size_of "$mem_file")"
snapshot_digest="$(digest_of "$snapshot_file")"
snapshot_size="$(size_of "$snapshot_file")"

config_file="$tmpdir/config.json"
python3 - "$config_file" "$repo" "$tag" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, repo, tag = sys.argv[1:4]
config = {
    "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "architecture": "amd64",
    "os": "linux",
    "config": {
        "Labels": {
            "org.opencontainers.image.title": repo,
            "org.opencontainers.image.ref.name": tag,
            "org.hermos.snapshot.format": "firecracker-snapshot",
        }
    },
    "rootfs": {"type": "layers", "diff_ids": []},
    "history": [{"created_by": "registry-snapshot/upload_snapshot.sh"}],
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(config, f, separators=(",", ":"), sort_keys=True)
PY

config_digest="$(digest_of "$config_file")"
config_size="$(size_of "$config_file")"

upload_blob "$config_file" "$config_digest"
upload_blob "$mem_file" "$mem_digest"
upload_blob "$snapshot_file" "$snapshot_digest"

write_manifest() {
    local manifest_file="$1"
    local manifest_kind="$2"

    python3 - "$manifest_file" "$manifest_kind" \
        "$config_digest" "$config_size" \
        "$mem_digest" "$mem_size" "$(basename "$mem_file")" \
        "$snapshot_digest" "$snapshot_size" "$(basename "$snapshot_file")" <<'PY'
import json
import sys

(
    path,
    manifest_kind,
    config_digest,
    config_size,
    mem_digest,
    mem_size,
    mem_title,
    snapshot_digest,
    snapshot_size,
    snapshot_title,
) = sys.argv[1:11]

if manifest_kind == "oci":
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": int(config_size),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": mem_digest,
                "size": int(mem_size),
                "annotations": {
                    "org.opencontainers.image.title": mem_title,
                    "org.hermos.snapshot.role": "mem",
                },
            },
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": snapshot_digest,
                "size": int(snapshot_size),
                "annotations": {
                    "org.opencontainers.image.title": snapshot_title,
                    "org.hermos.snapshot.role": "snapshot",
                },
            },
        ],
    }
elif manifest_kind == "docker":
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "digest": config_digest,
            "size": int(config_size),
        },
        "layers": [
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "digest": mem_digest,
                "size": int(mem_size),
            },
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "digest": snapshot_digest,
                "size": int(snapshot_size),
            },
        ],
    }
else:
    raise SystemExit(f"unknown manifest kind: {manifest_kind}")

with open(path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write("\n")
PY
}

upload_manifest() {
    local manifest_file="$1"
    local content_type="$2"
    local body_file="$tmpdir/manifest-upload.body"
    local headers_file="$tmpdir/manifest-upload.headers"

    : > "$body_file"
    : > "$headers_file"

    curl -sS -X PUT \
        -D "$headers_file" \
        -H "Content-Type: $content_type" \
        --data-binary "@$manifest_file" \
        -o "$body_file" \
        "$base_url/manifests/$tag"
}

manifest_file="$tmpdir/manifest.json"
manifest_content_type="application/vnd.oci.image.manifest.v1+json"
write_manifest "$manifest_file" "oci"

echo "uploading manifest: $repo:$tag"
if ! upload_manifest "$manifest_file" "$manifest_content_type"; then
    echo "registry rejected OCI manifest, retrying with Docker schema 2 manifest" >&2
    manifest_content_type="application/vnd.docker.distribution.manifest.v2+json"
    write_manifest "$manifest_file" "docker"

    if ! upload_manifest "$manifest_file" "$manifest_content_type"; then
        echo "manifest upload failed" >&2
        if [[ -s "$tmpdir/manifest-upload.headers" ]]; then
            echo "--- response headers ---" >&2
            cat "$tmpdir/manifest-upload.headers" >&2
        fi
        if [[ -s "$tmpdir/manifest-upload.body" ]]; then
            echo "--- response body ---" >&2
            cat "$tmpdir/manifest-upload.body" >&2
        fi
        exit 1
    fi
fi

manifest_digest="$(digest_of "$manifest_file")"

cat <<EOF
uploaded:
  registry: $registry
  repo:     $repo
  tag:      $tag
  manifest: $manifest_digest
  mem:      $mem_digest ($mem_size bytes)
  snapshot: $snapshot_digest ($snapshot_size bytes)
EOF
