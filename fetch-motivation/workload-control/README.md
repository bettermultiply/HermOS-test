# workload-control

This directory contains a minimal workload-id channel for restored Firecracker sandboxes.
The intended setup is:

1. Install `guest/motivation_daemon.c` as `/usr/local/bin/motivation-daemon`, or install `guest/motivation_agent.py` when Python-side debugging is needed.
2. Enable the systemd unit before creating the snapshot.
3. Take the snapshot after the agent has printed its ready marker.
4. After restore, use `host/workload_client.py id` to send a semantic workload id and wait for the result.

The agent or daemon should be part of the snapshotted guest state. In that case systemd startup is paid before snapshot creation, not during restore. The measured post-restore overhead is only the fixed id round trip plus the workload itself.

## Recommended Transport

Use TCP for the current tap-only snapshot configuration. Switch to `vsock` later only after the Firecracker config includes a vsock device and the guest kernel/rootfs supports it.

`vsock` is still the cleaner long-term transport because it avoids guest IP allocation, ARP, routing, and host firewall variability. It is not available when the snapshot was created with `"vsock": null`.

## Guest Install

Inside the rootfs, the preferred lightweight path is:

```bash
cc -O2 -static -s guest/motivation_daemon.c -o /usr/local/bin/motivation-daemon
install -m 0644 guest/motivation-daemon-tcp.service /etc/systemd/system/motivation-agent.service
systemctl enable motivation-agent.service
```

If static linking is unavailable, build without `-static`. For the Python agent path:

```bash
install -m 0755 guest/motivation_agent.py /usr/local/bin/motivation-agent
install -m 0644 guest/motivation-agent-tcp.service /etc/systemd/system/motivation-agent.service
systemctl enable motivation-agent.service
```

For future vsock-based snapshots, install `guest/motivation-agent-vsock.service` instead.

## Host Usage

Ping the guest agent over TCP:

```bash
python3 host/workload_client.py ping --transport tcp --host 172.16.0.2 --port 5000
```

Run a workload id over TCP:

```bash
python3 host/workload_client.py id \
  --transport tcp \
  --host 172.16.0.2 \
  --port 5000 \
  --timeout-sec 120 \
  agent-tool-trace
```

Future vsock usage:

```bash
python3 host/workload_client.py id \
  --transport vsock \
  --cid 3 \
  --port 5000 \
  --timeout-sec 120 \
  agent-tool-trace
```

The client exits with the guest workload's exit code. The id path suppresses workload stdout/stderr in the guest daemon/agent, so result transport does not become part of the measured target workload.

The same agent also exposes a minimal HTTP health endpoint:

```bash
curl -i http://172.16.0.2:5000/health
```

A healthy agent returns `HTTP/1.1 204 No Content`. This can be used as the smallest possible post-restore workload when the only thing being measured is whether the restored guest can receive and answer a host request.

Example `config.local.toml` wiring for the current tap-only A group. Concurrent restores automatically run the host client inside the per-sandbox netns, so each guest can keep using `172.16.0.2:5000` inside its own namespace:

```toml
[experiment]
control_transport = "tcp"
control_host = "172.16.0.2"
control_port = 5000
workload_timeout_sec = 120

[tools]
workload_client = "workload-control/host/workload_client.py"

[[workloads]]
name = "agent-tool-trace"
workload_command = "agent-tool-trace"
```

With the default A-group template, `workload_round_trip_ms` is the host-side duration of sending the workload id, waiting for guest execution, and receiving the result. `load_snapshot_ms` is tracked separately.

## Workloads

Install all workload scripts into `/opt/workloads`:

```bash
install -m 0755 workloads/*.py /opt/workloads/
```

The current Agent-centered suite is:

| Name | Workload id |
|------|---------|
| `health-check` | `health-daemon` |
| `python-json-tool` | `python-json-tool` |
| `cli-pipeline` | `cli-pipeline` |
| `random-rg-scan-fixed` | `random-rg-scan-fixed` |
| `random-rg-scan-random` | `random-rg-scan-random` |
| `read-list` | `read-list` |
| `agent-tool-trace` | `agent-tool-trace` |

Before taking the snapshot, prepare the shared fixture and the tmpfs-backed read-list file inside the guest:

```bash
/usr/local/bin/python3 /opt/workloads/python_json_tool.py prepare \
  --fixture-dir /opt/workloads/agent-fixture \
  --records 20000

/usr/local/bin/python3 /opt/workloads/read_list.py prepare \
  --path /dev/shm/read-list.bin \
  --size-mib 512 \
  --pattern random
```

The `rg`-based workloads require ripgrep by default so the measured behavior matches common code-agent tool use. Use each script's `--allow-grep` option only for manual debugging.

Before taking the snapshot, run the full workload check inside the guest:

```bash
/usr/local/bin/python3 -B /opt/workloads/check_workloads.py \
  --fixture-dir /opt/workloads/agent-fixture \
  --read-list-path /dev/shm/read-list.bin \
  --min-read-list-mib 512 \
  --require-rg
```

Expected behavior:

- The local agent health endpoint returns HTTP `204`.
- `agent_tool_trace.py check` validates the trace file, repo, `rg`, and search matches.
- Every workload `run` command returns a `WORKLOAD_DONE` JSON line.
- The checker compares `/opt/workloads` and `/opt/workloads/agent-fixture` before and after the run to ensure no files were created or modified.
- On success, the final JSON line contains `"workload": "check-workloads"` and `"marker": "WORKLOAD_DONE"` with exit code 0.
- On failure, it prints `"marker": "WORKLOAD_CHECK_FAILED"` and exits nonzero.

Future vsock wiring:

```toml
[experiment]
control_transport = "vsock"
control_cid = 3
control_port = 5000
```

That requires a Firecracker vsock device in the VM configuration before the snapshot is created.

## Protocol

The experiment protocol is one workload id line over a single connection.

Request:

```text
agent-tool-trace
```

Response:

```json
{"ok":true,"id":"agent-tool-trace","rc":0,"returncode":0,"duration_ms":1.2}
```

The Python agent still keeps the older newline-delimited JSON command protocol for manual debugging.

Request:

```json
{"op":"run","argv":["/bin/echo","ok"],"timeout_sec":5}
```

Response:

```json
{"ok":true,"returncode":0,"stdout":"ok\n","stderr":"","duration_ms":1.2}
```

For experiments, prefer the id protocol. It keeps command output off the control channel and leaves the workload implementation in the guest-side fixed mapping.
