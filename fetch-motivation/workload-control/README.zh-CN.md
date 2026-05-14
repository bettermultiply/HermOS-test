# Workload Control 安装说明

这份文档说明如何把 workload control 安装进 guest rootfs，并在恢复 Firecracker sandbox 后通过 `tap0` 网络向 guest 发送 workload id。并发恢复时，`tap0` 位于每个 sandbox 独立的 netns 中，因此多个 guest 可以复用相同的 `172.16.0.2:5000`。

当前 `snapshots-build/min-configuration.json` 是 tap-only 配置：

```json
"network-interfaces": [
  {
    "iface_id": "net1",
    "guest_mac": "06:00:AC:10:00:02",
    "host_dev_name": "tap0"
  }
],
"vsock": null
```

因此当前不要使用 vsock。控制通道使用 TCP over tap，默认 guest IP 假设为 `172.16.0.2`，agent 监听端口为 `5000`。在实验 harness 中，host 侧 client 会自动进入对应 sandbox 的 netns 后再访问这个地址。

## 目标

安装完成后，guest 内会有一个 systemd 服务：

```text
motivation-agent.service
```

它会在 guest 启动时运行：

```bash
/usr/local/bin/motivation-daemon 5000
```

host 侧可以通过下面的命令触发 guest workload：

```bash
python3 fetch-motivation/workload-control/host/workload_client.py id \
  --transport tcp \
  --host 172.16.0.2 \
  --port 5000 \
  --timeout-sec 120 \
  agent-tool-trace
```

也可以用 HTTP health endpoint 做最小存活检查：

```bash
curl -i http://172.16.0.2:5000/health
```

服务正常时应返回：

```text
HTTP/1.1 204 No Content
```

## 安装到 rootfs

下面假设你已经把 rootfs 挂载到 host 上的某个目录，例如：

```bash
export ROOTFS_MNT=/mnt/hermos-rootfs
```

创建目录：

```bash
sudo mkdir -p "$ROOTFS_MNT/usr/local/bin"
sudo mkdir -p "$ROOTFS_MNT/etc/systemd/system"
sudo mkdir -p "$ROOTFS_MNT/opt/workloads"
```

安装推荐的 C 版 guest daemon：

```bash
cc -O2 -static -s \
  fetch-motivation/workload-control/guest/motivation_daemon.c \
  -o "$ROOTFS_MNT/usr/local/bin/motivation-daemon"
```

如果 rootfs 不支持静态链接，可以去掉 `-static`。需要 Python 调试能力时，也可以安装 Python agent：

```bash
sudo install -m 0755 \
  fetch-motivation/workload-control/guest/motivation_agent.py \
  "$ROOTFS_MNT/usr/local/bin/motivation-agent"
```

安装 TCP systemd unit：

```bash
sudo install -m 0644 \
  fetch-motivation/workload-control/guest/motivation-daemon-tcp.service \
  "$ROOTFS_MNT/etc/systemd/system/motivation-agent.service"
```

安装 workload：

```bash
sudo install -m 0755 fetch-motivation/workload-control/workloads/*.py \
  "$ROOTFS_MNT/opt/workloads/"
```

这些 workload 只依赖 Python 标准库和常见 shell 工具；`cli-pipeline`、`random-rg-scan-*`、`agent-tool-trace` 默认要求安装 `rg`，以保证测到的是 Agent 常用的 ripgrep 行为。只有手工调试时才建议用脚本的 `--allow-grep` 降级。

当前核心 workload：

| 名称 | 脚本 | 说明 |
|------|------|------|
| `health-check` | 无 guest 脚本 | host 发送 `health-daemon` workload id，不 fork 子进程 |
| `python-json-tool` | `python_json_tool.py` | Python JSON/JSONL 解析与聚合，代表 Agent glue code |
| `cli-pipeline` | `cli_pipeline.py` | `find + rg + sort + head` 工具链 |
| `random-rg-scan-fixed` | `random_rg_scan.py --mode fixed` | 固定代码扫描 baseline |
| `random-rg-scan-random` | `random_rg_scan.py --mode random` | 随机 pattern/path 的不可预测工作集 |
| `read-list` | `read_list.py` | 读取 `/dev/shm/read-list.bin` 中的 512 MiB 内存文件 |
| `agent-tool-trace` | `agent_tool_trace.py` | replay 固定 Agent tool-use trace，不包含 LLM 推理 |

选择这些 workload 的思路：

- `python-json-tool` 和 `cli-pipeline` 作为通用负载，但仍然贴近 Agent 平台中的 JSON glue code 和 shell 工具链。
- `health-check` 是最小 sanity check，只验证恢复后的 guest daemon/agent 是否能响应，不在 guest 内启动 workload 子进程。
- `random-rg-scan-fixed` 和 `random-rg-scan-random` 合并代码仓库扫描与难预测工作集两个需求；固定模式用于稳定 baseline，随机模式用于观察 prefetch 对动态访问模式的局限。
- `read-list` 专门构造内存读密集场景。它使用 `/dev/shm`，目的是让 512 MiB 数据进入 memory snapshot，避免退化成普通磁盘读。
- `agent-tool-trace` 是目标场景负载，但只 replay 工具调用，不调用 LLM，从而剥离推理时间。
- fixture 由脚本生成，避免下载大型软件或数据集；实验重点放在 snapshot fetching，而不是外部依赖安装。
- snapshot 恢复后只发送 workload id。guest 侧固定映射到对应 Python 或 CLI workload 入口，默认不传 `--output`，并用 `python3 -B` 禁止生成 `__pycache__`，避免 workload 成功依赖恢复后写磁盘。

## 创建 workload fixture

在创建 snapshot 前，先在普通 VM 内准备共享 fixture 和 read-list 文件：

```bash
/usr/local/bin/python3 /opt/workloads/python_json_tool.py prepare \
  --fixture-dir /opt/workloads/agent-fixture \
  --records 20000

/usr/local/bin/python3 /opt/workloads/read_list.py prepare \
  --path /dev/shm/read-list.bin \
  --size-mib 512 \
  --pattern random
```

`/opt/workloads/agent-fixture` 是轻量合成代码仓库、Markdown 文档、JSON 配置和 JSONL event 数据；它避免下载大型软件，同时让 workload 行为接近 Agent 常见的 repo/search/config 访问。`/dev/shm/read-list.bin` 必须在 snapshot 前创建，因为它的目标是进入 guest memory snapshot。

创建 snapshot 前，运行完整 workload 检查：

```bash
/usr/local/bin/python3 -B /opt/workloads/check_workloads.py \
  --fixture-dir /opt/workloads/agent-fixture \
  --read-list-path /dev/shm/read-list.bin \
  --min-read-list-mib 512 \
  --require-rg
```

预期行为：

- `motivation-agent` 的 `http://127.0.0.1:5000/health` 返回 HTTP `204`。
- `agent_tool_trace.py check` 确认 trace 文件、repo、`rg` 和搜索命中可用。
- 所有 workload 的 `run` 子命令都返回 `WORKLOAD_DONE` JSON。
- 检查脚本比较运行前后的 `/opt/workloads` 和 `/opt/workloads/agent-fixture` 文件列表、大小和 mtime，确认 `run` 阶段没有写入或修改文件。
- 成功时最后输出一行 JSON，包含 `"workload": "check-workloads"` 和 `"marker": "WORKLOAD_DONE"`，退出码为 0。
- 失败时输出 `"marker": "WORKLOAD_CHECK_FAILED"`，退出码为 1，不应创建 snapshot。

## 启用 systemd 服务

如果 rootfs 可以 chroot，并且里面有 systemd 工具，可以执行：

```bash
sudo chroot "$ROOTFS_MNT" systemctl enable motivation-agent.service
```

如果不能 chroot，可以手动创建 systemd enable 所需的 symlink：

```bash
sudo mkdir -p "$ROOTFS_MNT/etc/systemd/system/multi-user.target.wants"
sudo ln -sf \
  ../motivation-agent.service \
  "$ROOTFS_MNT/etc/systemd/system/multi-user.target.wants/motivation-agent.service"
```

## Guest 网络要求

agent 监听 `0.0.0.0:5000`，但 guest 必须有可达的 IP。当前 host client 默认连接：

```text
172.16.0.2:5000
```

因此 guest 启动后需要满足：

```bash
ip addr show
```

能看到 guest 网卡上有 `172.16.0.2` 或你在 `config.local.toml` 中配置的其他 IP。

如果 guest 没有自动配置 IP，可以在 rootfs 的网络配置中固定分配，例如：

```text
guest ip: 172.16.0.2/24
host tap: 172.16.0.1/24
```

具体网络配置方式取决于你的 rootfs 使用 `systemd-networkd`、`ifupdown`、自定义 init 脚本，还是 `overlay-init`。

## 创建 snapshot 前的检查

启动一个普通 Firecracker VM，进入 guest 后检查：

```bash
systemctl status motivation-agent.service
ss -lntp | grep 5000
```

应看到 agent 正在监听 `0.0.0.0:5000`。

在 host 上检查 TCP 可达：

```bash
python3 fetch-motivation/workload-control/host/workload_client.py ping \
  --transport tcp \
  --host 172.16.0.2 \
  --port 5000 \
  --json
```

预期返回包含：

```json
{
  "ok": true,
  "op": "pong"
}
```

也可以直接用 `curl` 检查 HTTP health：

```bash
curl -fsS -o /dev/null -w "%{http_code}\n" http://172.16.0.2:5000/health
```

预期输出：

```text
204
```

再检查 workload id：

```bash
python3 fetch-motivation/workload-control/host/workload_client.py id \
  --transport tcp \
  --host 172.16.0.2 \
  --port 5000 \
  --json \
  agent-tool-trace
```

预期返回包含：

```json
{
  "ok": true,
  "returncode": 0
}
```

并且 `stdout` 中包含：

```text
WORKLOAD_DONE
```

## 什么时候创建 snapshot

必须在下面条件都满足后再创建 snapshot：

- `motivation-agent.service` 已启动。
- daemon/agent 已监听 TCP `5000` 端口。
- `curl http://172.16.0.2:5000/health` 返回 HTTP `204`。
- host 可以成功 `ping` agent。
- host 可以通过 agent 执行目标 workload。

这样恢复 snapshot 后，systemd 启动 agent 的开销不会进入实验测量。实验中的 `workload_round_trip_ms` 只包含：

```text
host 发送 workload id -> guest 执行 workload -> host 收到结果
```

如果要把 health check 当作最小 workload，可以在 workload 表中使用 `health-daemon` id。`fetch-motivation/config.example.toml` 已经包含 `health-check` 示例：

```toml
[[workloads]]
name = "health-check"
workload_command = "health-daemon"
```

这条路径只测恢复后 guest agent 是否已经可达，不会触发 guest 内的子进程执行。

## 配置实验 harness

`fetch-motivation/config.example.toml` 已经默认使用 TCP：

```toml
[experiment]
control_transport = "tcp"
control_host = "172.16.0.2"
control_port = 5000
workload_timeout_sec = 30
```

真实运行时，在 `config.local.toml` 中确认 workload 命令：

```toml
[[workloads]]
name = "agent-tool-trace"
workload_command = "agent-tool-trace"
```

如果 guest IP 不是 `172.16.0.2`，修改：

```toml
[experiment]
control_host = "你的 guest IP"
```

## 和 vsock 的关系

当前不要启用 vsock。启用 vsock 需要 Firecracker 配置中额外增加 vsock device，并且 guest kernel/rootfs 支持 virtio-vsock。增加设备可能改变 restore 路径和冷启动时间，因此不适合作为当前 motivation 实验的默认控制通道。

以后如果要切换到 vsock，需要同时修改：

- Firecracker VM 配置，增加 vsock 设备。
- rootfs 中启用 `motivation-agent-vsock.service`。
- `config.local.toml` 中设置 `control_transport = "vsock"`。
