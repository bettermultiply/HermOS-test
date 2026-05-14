from __future__ import annotations

import subprocess as sp
from pathlib import Path

from utils.run_work_dir import prepare_run_work_dir, socket_dir


VETH_BASE = 200


def sh(cmd, **kw):
    return sp.run(cmd, text=True, check=kw.pop("check", True), **kw)


def quiet(cmd):
    sp.run(cmd, stdout=sp.DEVNULL, stderr=sp.DEVNULL)


def upstream():
    words = sh(["ip", "route", "list", "default"], check=False, stdout=sp.PIPE, stderr=sp.DEVNULL).stdout.split()
    return words[words.index("dev") + 1] if "dev" in words else ""


def nsinfo(i, up):
    a, b, ns = VETH_BASE + i // 256, i % 256, f"fc{i}"
    return {
        "ns": ns,
        "hv": f"{ns}-veth",
        "host": f"10.{a}.{b}.1",
        "guest": f"10.{a}.{b}.2",
        "subnet": f"10.{a}.{b}.0/30",
        "up": up,
    }


def cleanup(n):
    if n.get("up"):
        quiet(["iptables", "-t", "nat", "-D", "POSTROUTING", "-s", n["subnet"], "-o", n["up"], "-j", "MASQUERADE"])
    quiet(["ip", "link", "del", n["hv"]])
    quiet(["ip", "netns", "del", n["ns"]])


def setup(n):
    ns, hv = n["ns"], n["hv"]
    cleanup(n)
    cmds = [
        ["ip", "netns", "add", ns],
        ["ip", "netns", "exec", ns, "ip", "tuntap", "add", "dev", "tap0", "mode", "tap"],
        ["ip", "netns", "exec", ns, "ip", "addr", "add", "172.16.0.1/30", "dev", "tap0"],
        ["ip", "netns", "exec", ns, "ip", "link", "set", "tap0", "up"],
        ["ip", "link", "add", "name", hv, "type", "veth", "peer", "name", "veth0", "netns", ns],
        ["ip", "netns", "exec", ns, "ip", "addr", "add", f"{n['guest']}/30", "dev", "veth0"],
        ["ip", "netns", "exec", ns, "ip", "link", "set", "veth0", "up"],
        ["ip", "addr", "add", f"{n['host']}/30", "dev", hv],
        ["ip", "link", "set", hv, "up"],
        ["ip", "netns", "exec", ns, "ip", "route", "add", "default", "via", n["host"]],
        ["ip", "netns", "exec", ns, "iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "172.16.0.1/30", "-o", "veth0", "-j", "MASQUERADE"],
        ["ip", "netns", "exec", ns, "iptables", "-P", "FORWARD", "ACCEPT"],
    ]
    if n["up"]:
        cmds.append(["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", n["subnet"], "-o", n["up"], "-j", "MASQUERADE"])
    try:
        for cmd in cmds:
            sh(cmd)
        return n
    except Exception:
        cleanup(n)
        raise


def api_sock(work_dir: Path, i: int) -> Path:
    base_sock_dir = socket_dir(work_dir)
    base_sock_dir.mkdir(parents=True, exist_ok=True)
    return base_sock_dir / f"fc{i}.sock"


def start_firecracker(i, n, firecracker, _firecracker_cwd, work_dir):
    sandbox_work_dir = prepare_run_work_dir(work_dir, i)
    api_path = api_sock(work_dir, i)
    api_path.unlink(missing_ok=True)
    log = (sandbox_work_dir / f"firecracker-{i}.log").open("ab")
    return sp.Popen(
        ["ip", "netns", "exec", n["ns"], str(firecracker), "--api-sock", str(api_path)],
        cwd=sandbox_work_dir,
        stdout=log,
        stderr=sp.STDOUT,
    )


def start_sandboxes(count, firecracker, firecracker_cwd, work_dir):
    up = upstream()
    netns = [setup(nsinfo(i, up)) for i in range(count)]
    procs = [start_firecracker(i, n, firecracker, firecracker_cwd, work_dir) for i, n in enumerate(netns)]
    return netns, procs


def stop(procs):
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(3)
        except sp.TimeoutExpired:
            p.kill()
