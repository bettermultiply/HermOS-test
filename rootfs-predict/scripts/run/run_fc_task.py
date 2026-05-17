#!/usr/bin/env python3
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT.parent
FC_ASSETS = PROJECT / "firecracker-build-scripts"


def env_path(name, default):
    return Path(os.environ.get(name, str(default))).expanduser().resolve()


FIRECRACKER = env_path("FIRECRACKER_BIN", FC_ASSETS / "firecracker")
PATCHED_FIRECRACKER = ROOT / "bin/firecracker-block-trace"
if "FIRECRACKER_BIN" not in os.environ and PATCHED_FIRECRACKER.exists():
    FIRECRACKER = PATCHED_FIRECRACKER.resolve()
KERNEL = env_path("FC_KERNEL", FC_ASSETS / "vmlinux-6.1.155")
ROOTFS = env_path("FC_ROOTFS", FC_ASSETS / "rootfs_file_codex")
SSH_KEY = env_path("FC_SSH_KEY", FC_ASSETS / "ubuntu-24.04.id_rsa")
DEFAULT_BLOCK_SIZE = 4096
SNAPSHOT_DIR = ROOT / "snapshots"
LOCK_DIR = env_path(
    "FC_LOCK_DIR",
    Path("/tmp") / f"rootfs-predict-{hashlib.sha1(str(ROOT).encode('utf-8')).hexdigest()[:12]}.locks",
)


def run(cmd, **kwargs):
    return subprocess.run(cmd, text=True, check=True, **kwargs)


def quiet(cmd):
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() not in {"0", "false", "no", "off"}


@contextlib.contextmanager
def file_lock(name):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()
    with (LOCK_DIR / f"{digest}.lock").open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def upstream_dev():
    proc = subprocess.run(
        ["ip", "route", "list", "default"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    words = proc.stdout.split()
    return words[words.index("dev") + 1] if "dev" in words else ""


def short_ifname(prefix, *parts):
    digest = hashlib.sha1("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:12]}"


def host_link_prefix(name):
    configured = os.environ.get("FC_HOST_LINK_PREFIX")
    if configured:
        return configured.rstrip(".")
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    return f"10.{digest[0]}.{digest[1]}"


def setup_netns(name, host_veth, host_prefix):
    host_ip = os.environ.get("FC_HOST_IP", f"{host_prefix}.1")
    ns_ip = os.environ.get("FC_NS_IP", f"{host_prefix}.2")
    host_cidr = os.environ.get("FC_HOST_CIDR", f"{host_prefix}.0/30")
    guest_tap_ip = os.environ.get("FC_TAP_IP", "172.16.0.1")
    up = upstream_dev()

    quiet(["sudo", "ip", "netns", "del", name])
    quiet(["sudo", "ip", "link", "del", host_veth])

    run(["sudo", "ip", "netns", "add", name])
    run(["sudo", "ip", "netns", "exec", name, "ip", "tuntap", "add", "dev", "tap0", "mode", "tap"])
    run(["sudo", "ip", "netns", "exec", name, "ip", "addr", "add", f"{guest_tap_ip}/30", "dev", "tap0"])
    run(["sudo", "ip", "netns", "exec", name, "ip", "link", "set", "tap0", "up"])
    run(["sudo", "ip", "link", "add", "name", host_veth, "type", "veth", "peer", "name", "veth0", "netns", name])
    run(["sudo", "ip", "netns", "exec", name, "ip", "addr", "add", f"{ns_ip}/30", "dev", "veth0"])
    run(["sudo", "ip", "netns", "exec", name, "ip", "link", "set", "veth0", "up"])
    run(["sudo", "ip", "addr", "add", f"{host_ip}/30", "dev", host_veth])
    run(["sudo", "ip", "link", "set", host_veth, "up"])
    run(["sudo", "ip", "netns", "exec", name, "ip", "route", "add", "default", "via", host_ip])
    run(["sudo", "ip", "netns", "exec", name, "iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "172.16.0.1/30", "-o", "veth0", "-j", "MASQUERADE"])
    run(["sudo", "ip", "netns", "exec", name, "iptables", "-P", "FORWARD", "ACCEPT"])
    if up:
        run(["sudo", "iptables", "-P", "FORWARD", "ACCEPT"])
        run(["sudo", "iptables", "-t", "nat", "-A", "POSTROUTING", "-s", host_cidr, "-o", up, "-j", "MASQUERADE"])
    return up


def guest_gateway():
    return os.environ.get("FC_GUEST_GATEWAY", os.environ.get("FC_TAP_IP", "172.16.0.1"))


def guest_dns_servers():
    configured = os.environ.get("FC_GUEST_DNS")
    if configured:
        return configured.replace(",", " ").split()

    servers = []
    resolv_conf = Path("/etc/resolv.conf")
    if resolv_conf.exists():
        for line in resolv_conf.read_text(encoding="utf-8", errors="ignore").splitlines():
            words = line.split()
            if len(words) >= 2 and words[0] == "nameserver" and not words[1].startswith("127."):
                servers.append(words[1])
    return servers or ["1.1.1.1", "8.8.8.8"]


def configure_guest_network(netns):
    nameservers = "\n".join(f"nameserver {server}" for server in guest_dns_servers())
    resolv_conf = nameservers + "\n"
    guest_run(netns, f"ip route replace default via {shlex.quote(guest_gateway())}")
    guest_run(netns, f"printf %s {shlex.quote(resolv_conf)} > /etc/resolv.conf")


def cleanup_netns(name, up, host_veth, host_prefix):
    host_cidr = os.environ.get("FC_HOST_CIDR", f"{host_prefix}.0/30")
    if up:
        quiet(["sudo", "iptables", "-t", "nat", "-D", "POSTROUTING", "-s", host_cidr, "-o", up, "-j", "MASQUERADE"])
    quiet(["sudo", "ip", "link", "del", host_veth])
    quiet(["sudo", "ip", "netns", "del", name])


def ssh_base(netns):
    return [
        "sudo",
        "ip",
        "netns",
        "exec",
        netns,
        "ssh",
        "-i",
        str(SSH_KEY),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=3",
        f"{os.environ.get('FC_GUEST_USER', 'root')}@{os.environ.get('FC_GUEST_IP', '172.16.0.2')}",
    ]


def scp_base(netns):
    return [
        "sudo",
        "ip",
        "netns",
        "exec",
        netns,
        "scp",
        "-i",
        str(SSH_KEY),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
    ]


def wait_for_ssh(netns, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = subprocess.run(ssh_base(netns) + ["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if proc.returncode == 0:
            return
        time.sleep(1)
    raise TimeoutError("guest ssh is not ready")


def firecracker_boot_args(root_overlay):
    boot_args = os.environ.get("FC_BOOT_ARGS", "console=ttyS0 reboot=k panic=1 pci=off")
    if root_overlay:
        words = boot_args.split()
        if not any(word.startswith("init=") for word in words):
            boot_args += " init=/sbin/overlay-init"
        if not any(word.startswith("overlay_root=") for word in words):
            boot_args += " overlay_root=vdc"
    return boot_args


def write_fc_config(path, rootfs_img, workspace_img, upper_img=None, root_overlay=False, rootfs_read_only=False):
    drives = [
        {
            "drive_id": "rootfs",
            "is_root_device": True,
            "is_read_only": rootfs_read_only,
            "cache_type": "Unsafe",
            "path_on_host": str(rootfs_img),
            "io_engine": "Sync",
        },
        {
            "drive_id": "workspace",
            "is_root_device": False,
            "is_read_only": False,
            "cache_type": "Unsafe",
            "path_on_host": str(workspace_img),
            "io_engine": "Sync",
        },
    ]
    if upper_img is not None:
        drives.append(
            {
                "drive_id": "upper",
                "is_root_device": False,
                "is_read_only": False,
                "cache_type": "Unsafe",
                "path_on_host": str(upper_img),
                "io_engine": "Sync",
            }
        )

    config = {
        "boot-source": {
            "kernel_image_path": str(KERNEL),
            "boot_args": firecracker_boot_args(root_overlay),
            "initrd_path": None,
        },
        "drives": drives,
        "machine-config": {
            "vcpu_count": int(os.environ.get("FC_VCPU", "2")),
            "mem_size_mib": int(os.environ.get("FC_MEM_MIB", "2048")),
            "smt": False,
            "track_dirty_pages": False,
            "huge_pages": "None",
        },
        "network-interfaces": [
            {
                "iface_id": "net1",
                "guest_mac": os.environ.get("FC_GUEST_MAC", "06:00:AC:10:00:02"),
                "host_dev_name": "tap0",
            }
        ],
    }
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def agent_cmd():
    return os.environ.get(
        "AGENT_CMD",
        "codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -",
    )


def start_firecracker(netns, run_dir, config_path, sock, block_trace_path=None):
    sock.unlink(missing_ok=True)
    log = (run_dir / "firecracker.log").open("wb")
    trace_path = block_trace_path or run_dir / "block_trace.csv"
    env_cmd = (
        f"FC_BLOCK_TRACE_PATH={shlex.quote(str(trace_path))} "
        f"{shlex.quote(str(FIRECRACKER))} "
        f"--api-sock {shlex.quote(str(sock))}"
    )
    if config_path is not None:
        env_cmd += f" --config-file {shlex.quote(str(config_path))}"
    cmd = [
        "sudo",
        "ip",
        "netns",
        "exec",
        netns,
        "env",
        "bash",
        "-lc",
        env_cmd,
    ]
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)


def api_call(sock, method, path, body=None):
    cmd = ["curl", "--unix-socket", str(sock), "-fsS", "-X", method, f"http://localhost{path}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    run(cmd, stdout=subprocess.DEVNULL)


def pause_vm(sock):
    api_call(sock, "PATCH", "/vm", {"state": "Paused"})


def create_snapshot(sock, snapshot_path, mem_path):
    api_call(
        sock,
        "PUT",
        "/snapshot/create",
        {"snapshot_type": "Full", "snapshot_path": str(snapshot_path), "mem_file_path": str(mem_path)},
    )


def load_snapshot(sock, snapshot_path, mem_path):
    api_call(
        sock,
        "PUT",
        "/snapshot/load",
        {
            "snapshot_path": str(snapshot_path),
            "mem_backend": {"backend_type": "File", "backend_path": str(mem_path)},
            "resume_vm": True,
        },
    )


def guest_run(netns, command):
    return run(ssh_base(netns) + [command])


def copy_to_guest(netns, local, remote):
    target = f"{os.environ.get('FC_GUEST_USER', 'root')}@{os.environ.get('FC_GUEST_IP', '172.16.0.2')}:{remote}"
    run(scp_base(netns) + [str(local), target])


def copy_from_guest(netns, remote, local):
    source = f"{os.environ.get('FC_GUEST_USER', 'root')}@{os.environ.get('FC_GUEST_IP', '172.16.0.2')}:{remote}"
    return subprocess.run(
        scp_base(netns) + [source, str(local)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def prepare_workspace_direct(netns, repo, base_commit):
    guest_run(netns, "mkdir -p /workspace")
    guest_run(netns, "mountpoint -q /workspace || mount /dev/vdb /workspace")
    guest_run(netns, f"rm -rf /workspace/repo && git clone https://github.com/{shlex.quote(repo)}.git /workspace/repo")
    guest_run(netns, f"git -C /workspace/repo checkout {shlex.quote(base_commit)}")


def mount_workspace_overlay(netns):
    guest_run(netns, "mkdir -p /workspace-lower /workspace /overlay")
    guest_run(netns, "mountpoint -q /workspace-lower || mount /dev/vdb /workspace-lower")
    overlay_base = (
        "overlay_base=/overlay; "
        "if mountpoint -q /rom/overlay; then "
        "overlay_base=/rom/overlay; "
        "else "
        "mkdir -p /overlay; "
        "mountpoint -q /overlay || mount /dev/vdc /overlay; "
        "fi"
    )
    guest_run(netns, "sync")
    guest_run(netns, "mount -o remount,ro /workspace-lower")
    guest_run(netns, f"{overlay_base}; rm -rf \"$overlay_base/workspace-upper\" \"$overlay_base/workspace-work\"")
    guest_run(netns, f"{overlay_base}; mkdir -p \"$overlay_base/workspace-upper\" \"$overlay_base/workspace-work\" /workspace/repo")
    guest_run(
        netns,
        f"{overlay_base}; "
        "mountpoint -q /workspace/repo || "
        "mount -t overlay overlay-workspace "
        "-o lowerdir=/workspace-lower/repo,upperdir=\"$overlay_base/workspace-upper\",workdir=\"$overlay_base/workspace-work\" "
        "/workspace/repo",
    )


def run_snapshot_warmup(netns):
    warmup_prompt = os.environ.get("WARMUP_PROMPT", "hi")
    command = (
        "mkdir -p /tmp/rootfs-predict; "
        "cd /workspace/repo || exit 1; "
        f"printf '%s\\n' {shlex.quote(warmup_prompt)} | "
        f"bash -lc {shlex.quote(agent_cmd())} "
        "> /tmp/rootfs-predict/warmup.out 2> /tmp/rootfs-predict/warmup.err"
    )
    guest_run(netns, command)


def copy_image(src, dst):
    dst.unlink(missing_ok=True)
    run(["cp", "--reflink=auto", str(src), str(dst)])


def replace_image(src, dst):
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.unlink(missing_ok=True)
    run(["cp", "--reflink=auto", str(src), str(tmp)])
    tmp.replace(dst)


def make_ext4(path, size_mb):
    path.unlink(missing_ok=True)
    run(["truncate", "-s", f"{size_mb}M", str(path)])
    run(["mkfs.ext4", "-F", "-q", str(path)])


def snapshot_ready(snapshot_dir):
    required = (
        "rootfs.ext4",
        "workspace.ext4",
        "upper.ext4",
        "active-upper.ext4",
        "firecracker.json",
        "vm.state",
        "vm.mem",
        "warmup.done",
    )
    return all((snapshot_dir / name).is_file() and (snapshot_dir / name).stat().st_size > 0 for name in required)


def snapshot_atlas_ready(snapshot_dir):
    required = (
        "rootfs_block_atlas.csv",
        "workspace_block_atlas.csv",
    )
    return all((snapshot_dir / name).is_file() and (snapshot_dir / name).stat().st_size > 0 for name in required)


def instance_metadata_ready(snapshot_dir):
    required = ("task.json", "prompt.txt")
    return all((snapshot_dir / name).is_file() and (snapshot_dir / name).stat().st_size > 0 for name in required)


def next_run_id(instance_id, requested_run_id):
    runs_root = ROOT / "runs" / instance_id
    candidate = requested_run_id
    match = re.match(r"^(.*?)(\d+)$", requested_run_id)
    if match:
        prefix = match.group(1)
        number = int(match.group(2))
    else:
        prefix = requested_run_id + "-"
        number = 0

    while (runs_root / candidate).exists():
        number += 1
        candidate = f"{prefix}{number}"
    return candidate


def net_names(instance_id, run_id):
    label = instance_id.replace("/", "-").replace("_", "-")[:24].strip("-")
    run_label = run_id.replace("/", "-").replace("_", "-")[:16].strip("-")
    digest = hashlib.sha1(f"{instance_id}\0{run_id}".encode("utf-8")).hexdigest()[:12]
    netns = f"rpf-{label}-{run_label}-{digest}"
    return netns, short_ifname("rv", netns), host_link_prefix(netns)


def wait_for_api_sock(sock, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock.exists():
            return
        time.sleep(0.1)
    raise TimeoutError("firecracker api socket is not ready")


def start_vm(instance_id, run_dir, config_path, run_id, block_trace_path=None, load=None):
    netns, host_veth, host_prefix = net_names(instance_id, run_id)
    api_sock = Path(os.environ.get("FC_API_SOCK_DIR", "/tmp")) / f"rpf-{short_ifname('fc', netns)}.sock"
    up = setup_netns(netns, host_veth, host_prefix)
    fc = None
    try:
        fc = start_firecracker(netns, run_dir, None if load is not None else config_path, api_sock, block_trace_path)
        if load is not None:
            wait_for_api_sock(api_sock)
            load_snapshot(api_sock, load[0], load[1])
        wait_for_ssh(netns)
        return netns, host_veth, host_prefix, api_sock, up, fc
    except Exception:
        stop_vm(netns, host_veth, host_prefix, api_sock, up, fc)
        raise


def stop_vm(netns, host_veth, host_prefix, api_sock, up, fc):
    if fc is not None and fc.poll() is None:
        fc.terminate()
        try:
            fc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fc.kill()
    cleanup_netns(netns, up, host_veth, host_prefix)
    api_sock.unlink(missing_ok=True)


def export_block_atlas(snapshot_dir, python):
    rootfs_img = snapshot_dir / "rootfs.ext4"
    workspace_img = snapshot_dir / "workspace.ext4"
    rootfs_atlas = snapshot_dir / "rootfs_block_atlas.csv"
    workspace_atlas = snapshot_dir / "workspace_block_atlas.csv"

    run(
        [
            python,
            str(ROOT / "scripts/collect/export_block_atlas.py"),
            str(rootfs_img),
            str(rootfs_atlas),
            "rootfs",
            "/",
            "/",
            "/dev",
            "/proc",
            "/run",
            "/sys",
            "/tmp",
        ]
    )
    run(
        [
            python,
            str(ROOT / "scripts/collect/export_block_atlas.py"),
            str(workspace_img),
            str(workspace_atlas),
            "workspace",
            "/workspace/repo",
            "/repo",
        ]
    )


def prepare_snapshot(instance_id, repo, base_commit, snapshot_dir, python):
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    rootfs_img = snapshot_dir / "rootfs.ext4"
    workspace_img = snapshot_dir / "workspace.ext4"
    upper_img = snapshot_dir / "upper.ext4"
    active_upper = snapshot_dir / "active-upper.ext4"
    config_path = snapshot_dir / "firecracker.json"
    state_path = snapshot_dir / "vm.state"
    mem_path = snapshot_dir / "vm.mem"
    warmup_marker = snapshot_dir / "warmup.done"
    if snapshot_ready(snapshot_dir) and snapshot_atlas_ready(snapshot_dir):
        return
    if snapshot_ready(snapshot_dir):
        export_block_atlas(snapshot_dir, python)
        return

    for path in (
        state_path,
        mem_path,
        warmup_marker,
        snapshot_dir / "rootfs_block_atlas.csv",
        snapshot_dir / "workspace_block_atlas.csv",
    ):
        path.unlink(missing_ok=True)

    make_ext4(workspace_img, os.environ.get("FC_WORKSPACE_MB", "8192"))
    copy_image(ROOTFS, rootfs_img)
    make_ext4(upper_img, os.environ.get("FC_UPPER_MB", "4096"))

    write_fc_config(config_path, rootfs_img, workspace_img, None, False, False)
    trace_path = snapshot_dir / "prepare_block_trace.csv"
    netns, host_veth, host_prefix, api_sock, up, fc = start_vm(instance_id, snapshot_dir, config_path, "prepare", trace_path)
    try:
        guest_run(netns, "mountpoint -q /tmp || mount -t tmpfs tmpfs /tmp || true")
        configure_guest_network(netns)
        prepare_workspace_direct(netns, repo, base_commit)
        guest_run(netns, "sync")
    finally:
        stop_vm(netns, host_veth, host_prefix, api_sock, up, fc)

    copy_image(upper_img, active_upper)
    write_fc_config(config_path, rootfs_img, workspace_img, active_upper, True, env_bool("FC_ROOTFS_READ_ONLY", False))
    trace_path = snapshot_dir / "snapshot_block_trace.csv"
    netns, host_veth, host_prefix, api_sock, up, fc = start_vm(instance_id, snapshot_dir, config_path, "snapshot", trace_path)
    try:
        guest_run(netns, "mountpoint -q /tmp || mount -t tmpfs tmpfs /tmp || true")
        guest_run(netns, "mkdir -p /tmp/rootfs-predict")
        guest_run(netns, f"ip route replace default via {shlex.quote(guest_gateway())}")
        mount_workspace_overlay(netns)
        run_snapshot_warmup(netns)
        guest_run(netns, "sync")
        pause_vm(api_sock)
        create_snapshot(api_sock, state_path, mem_path)
    finally:
        stop_vm(netns, host_veth, host_prefix, api_sock, up, fc)

    replace_image(active_upper, upper_img)
    replace_image(upper_img, active_upper)
    warmup_marker.write_text("ok\n", encoding="utf-8")
    export_block_atlas(snapshot_dir, python)


def write_agent_script(run_dir, agent_cmd):
    guest_script = run_dir / "guest_run.sh"
    guest_script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set +e",
                "mkdir -p /tmp/rootfs-predict",
                "cd /workspace/repo || exit 1",
                f"bash -lc {shlex.quote(agent_cmd)} < /tmp/rootfs-predict/prompt.txt > /tmp/rootfs-predict/agent.out 2> /tmp/rootfs-predict/agent.err",
                "status=$?",
                "sync",
                "echo \"$status\" > /tmp/rootfs-predict/status",
                "exit \"$status\"",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return guest_script


def run_instance(instance_id, run_id, dataset, split, python):
    run_dir = ROOT / "runs" / instance_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / "data").mkdir(exist_ok=True)

    snapshot_dir = SNAPSHOT_DIR / instance_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    if not instance_metadata_ready(snapshot_dir):
        run([python, str(ROOT / "scripts/dataset/get_instance.py"), dataset, split, instance_id, str(snapshot_dir)])
    task_path = snapshot_dir / "task.json"
    prompt_path = snapshot_dir / "prompt.txt"
    repo = subprocess.check_output([python, str(ROOT / "scripts/dataset/print_instance_field.py"), str(task_path), "repo"], text=True).strip()
    base_commit = subprocess.check_output([python, str(ROOT / "scripts/dataset/print_instance_field.py"), str(task_path), "base_commit"], text=True).strip()

    block_size = int(os.environ.get("FC_BLOCK_SIZE", str(DEFAULT_BLOCK_SIZE)))
    if block_size <= 0:
        raise SystemExit("FC_BLOCK_SIZE must be positive")
    prepare_snapshot(instance_id, repo, base_commit, snapshot_dir, python)
    if not snapshot_ready(snapshot_dir):
        raise SystemExit(f"snapshot is incomplete: {snapshot_dir}")

    upper_base = snapshot_dir / "upper.ext4"
    active_upper = snapshot_dir / "active-upper.ext4"
    state_path = snapshot_dir / "vm.state"
    mem_path = snapshot_dir / "vm.mem"
    config_path = snapshot_dir / "firecracker.json"
    rootfs_atlas = snapshot_dir / "rootfs_block_atlas.csv"
    workspace_atlas = snapshot_dir / "workspace_block_atlas.csv"
    upper_img = run_dir / "upper.ext4"
    copy_image(upper_base, upper_img)
    replace_image(upper_img, active_upper)
    raw_block_trace = run_dir / "block_trace.csv"
    preworkload_block_trace = run_dir / "preworkload_block_trace.csv"
    workload_block_trace = run_dir / "workload_block_trace.csv"
    raw_block_trace.write_text("", encoding="utf-8")
    preworkload_block_trace.unlink(missing_ok=True)
    workload_block_trace.unlink(missing_ok=True)

    up = ""
    fc = None
    netns = host_veth = host_prefix = ""
    api_sock = Path(os.environ.get("FC_API_SOCK_DIR", "/tmp")) / "unused.sock"
    status = 1
    try:
        netns, host_veth, host_prefix, api_sock, up, fc = start_vm(
            instance_id,
            run_dir,
            config_path,
            run_id,
            raw_block_trace,
            load=(state_path, mem_path),
        )
        print(netns)
        guest_run(netns, "mountpoint -q /tmp || mount -t tmpfs tmpfs /tmp || true")
        guest_run(netns, "mkdir -p /tmp/rootfs-predict")

        guest_script = write_agent_script(run_dir, agent_cmd())
        copy_to_guest(netns, prompt_path, "/tmp/rootfs-predict/prompt.txt")
        copy_to_guest(netns, guest_script, "/tmp/rootfs-predict/guest_run.sh")
        guest_run(netns, "chmod +x /tmp/rootfs-predict/guest_run.sh")

        raw_block_trace.replace(preworkload_block_trace)
        proc = subprocess.Popen(
            ssh_base(netns) + ["/tmp/rootfs-predict/guest_run.sh"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

        status = proc.wait()
        for remote_name in ("agent.out", "agent.err", "status"):
            copy_from_guest(
                netns,
                f"/tmp/rootfs-predict/{remote_name}",
                run_dir / remote_name,
            )
        if raw_block_trace.exists():
            raw_block_trace.replace(workload_block_trace)
        else:
            workload_block_trace.write_text("", encoding="utf-8")

        with file_lock("data"):
            run(
                [
                    python,
                    str(ROOT / "scripts/collect/collect_first_read_blocks.py"),
                    str(preworkload_block_trace),
                    run_id,
                    instance_id,
                    "-",
                    str(block_size),
                    str(ROOT / "data/block_phased.csv"),
                    "preworkload",
                ]
            )
            run(
                [
                    python,
                    str(ROOT / "scripts/collect/collect_first_read_blocks.py"),
                    str(workload_block_trace),
                    run_id,
                    instance_id,
                    "-",
                    str(block_size),
                    str(ROOT / "data/block_phased.csv"),
                    "workload",
                ]
            )
            for trace_path, phase in (
                (preworkload_block_trace, "preworkload"),
                (workload_block_trace, "workload"),
            ):
                run(
                    [
                        python,
                        str(ROOT / "scripts/collect/collect_file_reads.py"),
                        str(trace_path),
                        run_id,
                        instance_id,
                        phase,
                        str(ROOT / "data/file_read_summary.csv"),
                        str(ROOT / "data/file_read_blocks.csv"),
                        str(ROOT / "data/unmapped_read_blocks.csv"),
                        str(rootfs_atlas),
                        str(workspace_atlas),
                        str(block_size),
                    ]
                )
    finally:
        if netns:
            stop_vm(netns, host_veth, host_prefix, api_sock, up, fc)
            replace_image(active_upper, upper_img)
        (run_dir / "status").write_text(str(status) + "\n", encoding="utf-8")

    return status


def main():
    if len(sys.argv) != 3:
        print("usage: run_fc_task.py INSTANCE_ID RUN_ID", file=sys.stderr)
        return 2

    instance_id, run_id = sys.argv[1:]
    dataset = os.environ.get("DATASET_NAME", "SWE-bench/SWE-bench_Verified")
    split = os.environ.get("SPLIT", "test")
    python = os.environ.get("PYTHON", str(ROOT / ".venv/bin/python"))
    if not Path(python).exists():
        python = "python3"

    with file_lock(f"instance:{instance_id}"):
        run_id = next_run_id(instance_id, run_id)
        return run_instance(instance_id, run_id, dataset, split, python)


if __name__ == "__main__":
    raise SystemExit(main())
