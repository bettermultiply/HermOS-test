from __future__ import annotations

import json
import socket
import time
from pathlib import Path


def wait_api(api_sock: Path, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if api_sock.exists():
            return
        time.sleep(0.01)
    raise TimeoutError(api_sock)


def load_snapshot(api_sock: Path, snapshot_state: Path, memory_snapshot: Path, timeout: float = 30, backend_type: str = "File") -> None:
    wait_api(api_sock, timeout)
    body = json.dumps(
        {
            "snapshot_path": str(snapshot_state),
            "mem_backend": {"backend_type": backend_type, "backend_path": str(memory_snapshot)},
            "resume_vm": True,
        }
    ).encode()
    req = (
        b"PUT /snapshot/load HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(str(api_sock))
        s.sendall(req)
        data = b""
        while b"\r\n\r\n" not in data:
            data += s.recv(65536)
        head, _, rest = data.partition(b"\r\n\r\n")
        length = next((int(x.split(b":", 1)[1]) for x in head.splitlines()[1:] if x.lower().startswith(b"content-length:")), 0)
        while len(rest) < length:
            rest += s.recv(65536)
    status = head.decode(errors="replace").splitlines()[0]
    if " 2" not in status:
        raise RuntimeError(status + " " + rest.decode(errors="replace"))
