# Registry snapshot upload

This directory uploads Firecracker snapshot files to a Docker Registry using
the Registry HTTP API directly. It stores the memory file and snapshot file as
two OCI manifest layers under one tag.

Defaults for the current snapshot:

```bash
cd registry-snapshot
./upload_snapshot.sh
```

That publishes:

- registry: `http://localhost:5000`
- repository: `motivation`
- tag: `json`
- files: `../json_mem_file` and `../json_snapshot_file`

To pull the files back through the registry API:

```bash
cd registry-snapshot
./pull_snapshot.sh --out ./pulled-json
```

To upload another snapshot:

```bash
cd registry-snapshot
./upload_snapshot.sh \
  --repo motivation \
  --tag my-snapshot \
  --mem ../other_mem_file \
  --snapshot ../other_snapshot_file
```

The resulting registry path is:

```text
http://localhost:5000/v2/motivation/manifests/<tag>
```

For Docker CLI image pulls, the layer payloads would need to be valid image
layer tar archives. These scripts are intended for storing and retrieving raw
snapshot blobs through a Docker Registry.
