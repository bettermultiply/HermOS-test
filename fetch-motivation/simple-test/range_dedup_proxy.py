#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import socketserver
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FetchState:
    event: threading.Event = field(default_factory=threading.Event)
    data: bytes | None = None
    error: BaseException | None = None


class RangeStore:
    def __init__(self, remote_url: str, upstream_timeout_sec: float):
        self.remote_url = remote_url
        self.remote = HttpUrl.parse(remote_url)
        self.upstream_timeout_sec = upstream_timeout_sec
        self.lock = threading.Lock()
        self.local = threading.local()
        self.cache: dict[tuple[int, int], bytes] = {}
        self.inflight: dict[tuple[int, int], FetchState] = {}
        self.client_requests = 0
        self.remote_requests = 0
        self.cache_hits = 0
        self.inflight_hits = 0
        self.bytes_served = 0
        self.bytes_from_remote = 0
        self.cache_bytes = 0
        self.remote_fetch_ns_total = 0
        self.remote_fetch_ns_max = 0
        self.remote_errors = 0

    def get_range(self, start: int, end: int) -> bytes:
        key = (start, end - start + 1)
        owner = False

        with self.lock:
            self.client_requests += 1
            data = self.cache.get(key)
            if data is not None:
                self.cache_hits += 1
                self.bytes_served += len(data)
                return data

            state = self.inflight.get(key)
            if state is None:
                state = FetchState()
                self.inflight[key] = state
                self.remote_requests += 1
                owner = True
            else:
                self.inflight_hits += 1

        if owner:
            try:
                start_ns = time.monotonic_ns()
                data = self.fetch_remote(start, end)
                fetch_ns = time.monotonic_ns() - start_ns
                with self.lock:
                    self.cache[key] = data
                    self.cache_bytes += len(data)
                    self.bytes_from_remote += len(data)
                    self.bytes_served += len(data)
                    self.remote_fetch_ns_total += fetch_ns
                    self.remote_fetch_ns_max = max(self.remote_fetch_ns_max, fetch_ns)
                    state.data = data
                    self.inflight.pop(key, None)
                state.event.set()
                return data
            except BaseException as exc:
                with self.lock:
                    self.remote_errors += 1
                    state.error = exc
                    self.inflight.pop(key, None)
                state.event.set()
                raise

        state.event.wait()
        if state.error is not None:
            raise state.error
        if state.data is None:
            raise RuntimeError(f"range fetch completed without data: {start}-{end}")

        with self.lock:
            self.bytes_served += len(state.data)
        return state.data

    def fetch_remote(self, start: int, end: int) -> bytes:
        client = getattr(self.local, "client", None)
        if client is None:
            client = UpstreamRangeClient(self.remote, self.upstream_timeout_sec)
            self.local.client = client
        return client.fetch_range(start, end)

    def stats(self) -> dict[str, Any]:
        with self.lock:
            client_requests = self.client_requests
            remote_requests = self.remote_requests
            bytes_served = self.bytes_served
            bytes_from_remote = self.bytes_from_remote
            return {
                "bandwidth_saving": 1 - (bytes_from_remote / bytes_served)
                if bytes_served
                else 0,
                "bytes_from_remote": bytes_from_remote,
                "bytes_served": bytes_served,
                "cache_bytes": self.cache_bytes,
                "cache_entries": len(self.cache),
                "cache_hits": self.cache_hits,
                "client_requests": client_requests,
                "dedup_ratio": 1 - (remote_requests / client_requests)
                if client_requests
                else 0,
                "inflight": len(self.inflight),
                "inflight_hits": self.inflight_hits,
                "remote_errors": self.remote_errors,
                "remote_fetch_avg_ms": (self.remote_fetch_ns_total / remote_requests / 1_000_000)
                if remote_requests
                else 0,
                "remote_fetch_max_ms": self.remote_fetch_ns_max / 1_000_000,
                "remote_requests": remote_requests,
                "remote_url": self.remote_url,
            }


class DedupServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 1024

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[socketserver.StreamRequestHandler],
        store: RangeStore,
        client_timeout_sec: float,
    ):
        super().__init__(server_address, handler_class)
        self.store = store
        self.client_timeout_sec = client_timeout_sec


class RangeProxyHandler(socketserver.StreamRequestHandler):
    server: DedupServer
    rbufsize = 64 * 1024

    def setup(self) -> None:
        super().setup()
        self.request.settimeout(self.server.client_timeout_sec)
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def handle(self) -> None:
        while True:
            try:
                request = self.read_request()
            except (ConnectionResetError, BrokenPipeError, TimeoutError):
                return
            if request is None:
                return

            method, path, _version, headers = request
            close = headers.get("connection", "").lower() == "close"
            try:
                if method != "GET":
                    self.send_error(405, "method not allowed")
                    return
                if path == "/__stats":
                    self.send_json(self.server.store.stats(), close=close)
                    if close:
                        return
                    continue

                range_header = headers.get("range")
                if not range_header:
                    self.send_error(400, "missing Range header")
                    return
                start, end = parse_range_header(range_header)
                data = self.server.store.get_range(start, end)
                self.send_range(start, end, data, close=close)
                if close:
                    return
            except Exception as exc:
                self.send_error(502, str(exc))
                return

    def read_request(self) -> tuple[str, str, str, dict[str, str]] | None:
        while True:
            line = self.rfile.readline(65536)
            if not line:
                return None
            if line not in (b"\r\n", b"\n"):
                break

        try:
            method, path, version = line.decode("iso-8859-1").strip().split(" ", 2)
        except ValueError as exc:
            raise RuntimeError("bad HTTP request line") from exc

        headers: dict[str, str] = {}
        while True:
            line = self.rfile.readline(65536)
            if not line:
                raise RuntimeError("unexpected EOF while reading headers")
            if line in (b"\r\n", b"\n"):
                break
            try:
                key, value = line.decode("iso-8859-1").split(":", 1)
            except ValueError as exc:
                raise RuntimeError("bad HTTP header line") from exc
            headers[key.strip().lower()] = value.strip()

        return method, path, version, headers

    def send_range(self, start: int, end: int, data: bytes, *, close: bool) -> None:
        connection = "close" if close else "keep-alive"
        head = (
            "HTTP/1.1 206 Partial Content\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Content-Range: bytes {start}-{end}/*\r\n"
            "Accept-Ranges: bytes\r\n"
            f"Connection: {connection}\r\n"
            "\r\n"
        ).encode("ascii")
        self.request.sendall(head + data)

    def send_json(self, payload: dict[str, Any], *, close: bool) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        connection = "close" if close else "keep-alive"
        head = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Connection: {connection}\r\n"
            "\r\n"
        ).encode("ascii")
        self.wfile.write(head + data)
        self.wfile.flush()

    def send_error(self, status: int, message: str) -> None:
        data = message.encode("utf-8", errors="replace")
        head = (
            f"HTTP/1.1 {status} Error\r\n"
            "Content-Type: text/plain\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        self.wfile.write(head + data)
        self.wfile.flush()


def parse_range_header(value: str) -> tuple[int, int]:
    prefix = "bytes="
    if not value.startswith(prefix):
        raise ValueError(f"unsupported Range header: {value}")
    spec = value[len(prefix) :]
    if "," in spec:
        raise ValueError(f"multiple ranges are not supported: {value}")
    start_s, end_s = spec.split("-", 1)
    if not start_s or not end_s:
        raise ValueError(f"open-ended ranges are not supported: {value}")
    start = int(start_s)
    end = int(end_s)
    if start < 0 or end < start:
        raise ValueError(f"invalid Range header: {value}")
    return start, end


class HttpUrl:
    def __init__(self, host: str, port: int, path: str, host_header: str):
        self.host = host
        self.port = port
        self.path = path
        self.host_header = host_header

    @classmethod
    def parse(cls, url: str) -> "HttpUrl":
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http":
            raise ValueError(f"only http:// upstream URLs are supported: {url}")
        if not parsed.hostname:
            raise ValueError(f"missing upstream host: {url}")
        port = parsed.port or 80
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        host_header = parsed.netloc
        return cls(parsed.hostname, port, path, host_header)


class UpstreamRangeClient:
    def __init__(self, url: HttpUrl, timeout_sec: float):
        self.url = url
        self.timeout_sec = timeout_sec
        self.stream: socket.socket | None = None
        self.reader = None

    def close(self) -> None:
        if self.reader is not None:
            try:
                self.reader.close()
            except OSError:
                pass
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
        self.reader = None
        self.stream = None

    def connect(self) -> None:
        stream = socket.create_connection((self.url.host, self.url.port), timeout=self.timeout_sec)
        stream.settimeout(self.timeout_sec)
        try:
            stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.stream = stream
        self.reader = stream.makefile("rb", buffering=64 * 1024)

    def fetch_range(self, start: int, end: int) -> bytes:
        try:
            return self.fetch_range_once(start, end)
        except OSError:
            self.close()
            return self.fetch_range_once(start, end)

    def fetch_range_once(self, start: int, end: int) -> bytes:
        if self.stream is None or self.reader is None:
            self.connect()
        assert self.stream is not None
        assert self.reader is not None

        expected_len = end - start + 1
        request = (
            f"GET {self.url.path} HTTP/1.1\r\n"
            f"Host: {self.url.host_header}\r\n"
            f"Range: bytes={start}-{end}\r\n"
            "Connection: keep-alive\r\n"
            "\r\n"
        ).encode("ascii")
        self.stream.sendall(request)

        status_line = self.reader.readline(65536)
        if not status_line:
            raise OSError("upstream closed connection")
        try:
            status = int(status_line.decode("iso-8859-1").split()[1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"bad upstream HTTP status line: {status_line!r}") from exc

        content_len = None
        while True:
            line = self.reader.readline(65536)
            if not line:
                raise OSError("upstream closed while reading headers")
            if line in (b"\r\n", b"\n"):
                break
            key, _, value = line.decode("iso-8859-1").partition(":")
            if key.lower() == "content-length":
                content_len = int(value.strip())

        if status != 206:
            raise RuntimeError(f"upstream returned HTTP {status}, expected 206")
        if content_len != expected_len:
            raise RuntimeError(
                f"upstream Content-Length mismatch for bytes={start}-{end}: "
                f"{content_len} != {expected_len}"
            )

        data = self.reader.read(expected_len)
        if len(data) != expected_len:
            raise OSError(
                f"upstream body length mismatch for bytes={start}-{end}: "
                f"{len(data)} != {expected_len}"
            )
        return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Single-host HTTP Range dedup proxy.")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--remote-url", required=True)
    parser.add_argument("--client-timeout-sec", type=float, default=120)
    parser.add_argument("--upstream-timeout-sec", type=float, default=30)
    args = parser.parse_args()

    store = RangeStore(args.remote_url, args.upstream_timeout_sec)
    with DedupServer(
        (args.listen_host, args.port),
        RangeProxyHandler,
        store,
        args.client_timeout_sec,
    ) as server:
        print(
            json.dumps(
                {
                    "event": "range-dedup-proxy-listening",
                    "listen_host": args.listen_host,
                    "port": args.port,
                    "remote_url": args.remote_url,
                    "upstream": "keep-alive-socket",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
