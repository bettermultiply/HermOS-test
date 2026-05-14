# fetch-motivation

这个目录用于运行 snapshot fetching motivation 实验。当前实现已经从旧的
`config.toml + scripts/run_experiment.py` harness 改成更直接的
`simple-test/` 方案脚本：每个实验组一个 Python 入口，脚本负责创建 netns、
启动 Firecracker、加载 snapshot、运行 guest workload、清理进程和网络资源，
最后在 stdout 输出本轮 JSON 结果。

## 目录结构

- `simple-test/`：当前实验入口和 host 侧编排代码。
- `simple-test/configs/`：每个实验组的 JSON 配置。
- `simple-test/launch/`：Firecracker、snapshot load、uffd socket 启动逻辑。
- `simple-test/utils/`：配置加载、并发执行、下载、清理和 workload 请求逻辑。
- `simple-test/range_dedup_proxy.py`：Lazy-Remote-Dedup 使用的 HTTP Range 去重代理。
- `simple-test/zero-pages.py`：扫描 memory snapshot 中的零页，并可生成去零页 dense 文件。
- `workload-control/guest/`：安装到 guest/rootfs 内的 `bench-daemon` 和 workload 数据准备脚本。
- `codex-history-parser/`：把 Codex history JSONL 转成 replay 输入的辅助脚本。
- `Motivation.md`：实验动机和目标图表口径的原始方案说明。

旧版 README 中提到的 `config.example.toml`、`config.local.toml`、
`env/check_env.py`、`scripts/run_experiment.py`、`scripts/aggregate_results.py`
等文件已经不属于当前实现。

## 实验组

当前可用入口如下：

| 入口 | 配置 | 说明 | 当前状态 |
|------|------|------|----------|
| `simple-test/Eager-Local.py` | `eager-local.json` | 从本地 memory snapshot 用 `File` backend 恢复 | 可运行 |
| `simple-test/Eager-Remote.py` | `eager-remote.json` | 先下载 memory snapshot 和 vmstate，再用 `File` backend 恢复 | 可运行 |
| `simple-test/Lazy-Local.py` | `lazy-local.json` | 本地 memory snapshot + uffd on-demand handler | 可运行 |
| `simple-test/Lazy-Remote.py` | `lazy-remote.json` | 下载 vmstate，memory page 由远端 uffd handler 按需拉取 | 可运行 |
| `simple-test/Lazy-Remote-Dedup.py` | `lazy-remote-dedup.json` | 在多个 uffd handler 前加单机 HTTP Range 去重代理 | 可运行 |
| `simple-test/Lazy-Prefetch-Remote.py` | `lazy-prefetch-remote.json` | Lazy + prefetch 预留入口 | 仍是占位，直接返回 1 |

## 前置条件

这些脚本需要 root 权限，因为会创建 netns、tap、veth、iptables NAT，并在
netns 中启动 Firecracker：

```bash
sudo python3 fetch-motivation/simple-test/Eager-Local.py
```

配置中的 `{root}` 会展开为仓库上级的 `HermOS-test` 目录，而不是
`fetch-motivation` 目录。默认路径假设这些文件已经存在：

- `{root}/firecracker-build-scripts/firecracker`
- `{root}/snapshots-build/agent_mem_file`
- `{root}/snapshots-build/agent_snapshot_file`
- `{root}/snapshots-build/overlay.ext4`
- `{root}/snapshots-build/rootfs_file_control`

Lazy 组还需要 Firecracker examples 中的 uffd handler，例如：

- `uffd_on_demand_handler`
- `uffd_remote_range_handler`

Remote 组需要一个可访问的 HTTP blob 服务。Eager-Remote 会整文件下载
`memory_snapshot_url` 和 `snapshot_state_url`；Lazy-Remote 和
Lazy-Remote-Dedup 会把 `memory_blob_url` 传给远端 uffd handler，要求它能按
handler 的协议提供 snapshot page/range 数据。

## Guest workload 控制

当前 guest 侧推荐使用 `workload-control/guest/bench-daemon`。它监听 TCP
`8080`，通过 HTTP path 选择 workload：

```bash
curl -i http://172.16.0.2:8080/cli-pipeline
```

daemon 返回 `text/plain`，包含：

- `workload`
- `elapsed_ns`
- `minflt`
- `majflt`
- `status`
- 可选 `detail`

当前注册的 workload id：

| Workload | 说明 |
|----------|------|
| `health-daemon` | daemon 内直接返回，用于最小可达性检查 |
| `health-exec` | fork/exec `/bin/true`，测最小进程启动路径 |
| `python-json-tool` | 读取并解析 `/opt/bench/data/data.json` |
| `cli-pipeline` | `find + rg + sort + head`，模拟 coding agent shell 工具链 |
| `random-rg-scan-fixed` | 固定 seed 的 ripgrep 扫描 |
| `random-rg-scan-random` | 随机 seed 的 ripgrep 扫描 |
| `read-list` | 顺序读取 `/dev/shm/read-list.bin` |
| `agent-tool-replay` | replay 固定 agent tool trace |

注意：`simple-test/configs/*.json` 里目前保留了 `control_host`、
`control_port` 和 `workload` 字段，但执行路径尚未读取这些字段。实际 workload
请求在 `simple-test/utils/workload_run.py` 中由 `WORKLOAD_URL` 决定，当前默认是：

```python
WORKLOAD_URL = "http://172.16.0.2:8080/cli-pipeline"
```

切换 workload 时先修改这个常量，或者后续再把它接回 JSON 配置。

## 准备 guest rootfs

在 host 上进入 guest workload 目录，编译 daemon：

```bash
cd fetch-motivation/workload-control/guest
make
```

把 `bench-daemon`、`bench-daemon.service`、`json_tool.py`、
`agent_replay.py`、`replay.json` 和数据准备脚本安装进 rootfs。当前提供的
`setup.sh` 设计为在 guest/chroot 内运行，它会：

- clone 固定版本的 `astropy`
- 生成 ripgrep 文件列表
- 生成 `/opt/bench/data/data.json`
- 安装 `bench-daemon` 和 systemd service
- 安装 JSON/replay workload 辅助脚本

示例：

```bash
cd /opt/bench
make
dd if=/dev/zero of=/dev/shm/read-list.bin bs=1M count=256
./setup.sh
```

`setup.sh` 需要 `git`、`python3`、`rg`、`find`、`sort`、`head`。它当前会在
verify 阶段检查 `/dev/shm/read-list.bin`，但不会主动生成该文件，所以需要先
创建 read-list 再运行 `setup.sh`。如果要调整大小，同时设置
`READLIST_SIZE_MB` 并修改 `dd count`。

确认 guest 中服务启动后再创建 snapshot：

```bash
systemctl status bench-daemon.service
ss -lntp | grep 8080
curl -i http://172.16.0.2:8080/health-daemon
curl -i http://172.16.0.2:8080/cli-pipeline
```

这样 snapshot 恢复后的测量路径只包含：

```text
host 发送 HTTP workload 请求 -> guest 执行 workload -> host 收到结果
```

## 配置

每个实验组直接读取 `simple-test/configs/<group>.json`。常用字段：

- `count`：并发 sandbox 数。
- `snapshot_a`：本地 memory snapshot。
- `snapshot_b`：预留字段，当前 Eager/Lazy local 脚本没有复制使用。
- `snapshot_state`：Firecracker vmstate 文件。
- `memory_snapshot_url`：Eager-Remote 的 memory snapshot 下载地址。
- `memory_blob_url`：Lazy-Remote 的远端 memory blob 地址。
- `snapshot_state_url`：Remote 组的 vmstate 下载地址。
- `firecracker`：Firecracker binary。
- `firecracker_cwd`：Firecracker 启动工作目录。
- `uffd_handler`：Lazy 组使用的 uffd handler。
- `work_dir`：每个 sandbox 的运行目录和日志目录。
- `timeout`：workload 请求超时秒数。
- `proxy_host`、`proxy_port`、`upstream_timeout_sec`：Lazy-Remote-Dedup 代理配置。

`firecracker_cwd` 参数当前只保留在函数签名里；实际 Firecracker 进程的 cwd 是
每个 sandbox 自己的 `work_dir/run_<id>`。启动前会把
`{root}/snapshots-build/overlay.ext4` 复制到该目录，并把
`rootfs_file_control` 软链接到同目录。

## 运行

从 `HermOS-test` 目录运行：

```bash
sudo python3 fetch-motivation/simple-test/Eager-Local.py
sudo python3 fetch-motivation/simple-test/Eager-Remote.py
sudo python3 fetch-motivation/simple-test/Lazy-Local.py
sudo python3 fetch-motivation/simple-test/Lazy-Remote.py
sudo python3 fetch-motivation/simple-test/Lazy-Remote-Dedup.py
```

也可以从 `fetch-motivation` 目录运行，但路径示例仍按 `{root}` 展开到上级
`HermOS-test`。

每个脚本都会在 `finally` 中清理：

- Firecracker 进程
- uffd handler 进程
- dedup proxy 进程
- netns、tap、veth、iptables NAT
- `/tmp/fc-sock/fc<i>.sock`
- `/tmp/fc-sock/uf<i>.sock`

如果脚本被强杀，可能需要手工清理残留的 `fc<i>` netns 或 socket。

## 输出

脚本会打印过程日志，并在最后输出 JSON。主要字段：

- `snapshot_pull_ms`：remote 组下载 snapshot 数据的时间。Eager-Remote 还会拆成
  `memory_pull_ms` 和 `snapshot_state_pull_ms`。
- `sandbox_start_ms`：创建 netns、启动 Firecracker、启动 uffd handler、调用
  `/snapshot/load` 并 resume VM 的总耗时。
- `workload_run_ms`：host 并发发送 workload 请求到所有 sandbox 完成的墙钟时间。
- `copied_pages`：Lazy 组从 `uffd-<i>.log` 行数估算的 copied page 数。
- `dedup_proxy`：Lazy-Remote-Dedup 的代理统计，包括 `client_requests`、
  `remote_requests`、`dedup_ratio`、`bandwidth_saving`、`cache_hits` 和
  `inflight_hits`。
- `workloads`：每个 sandbox 的简化 workload 结果。

单个 workload 结果包含：

- `sandbox`
- `workload_ms`
- `guest_process_ms`
- `client_round_trip_ms`
- `status`
- `workload`
- `minflt`
- `majflt`
- 可选 `detail`

Firecracker 和 uffd handler 日志在：

```text
simple-test-data/<group>/run_<id>/firecracker-<id>.log
simple-test-data/<group>/run_<id>/uffd-<id>.log
simple-test-data/<group>/range-dedup-proxy.log
```

## 零页扫描

扫描 memory snapshot：

```bash
python3 fetch-motivation/simple-test/zero-pages.py \
  snapshots-build/agent_mem_file
```

生成去零页 dense 文件：

```bash
python3 fetch-motivation/simple-test/zero-pages.py \
  snapshots-build/agent_mem_file \
  --dense /tmp/agent_mem_file.dense
```

输出包含 `total_pages`、`zero_pages`、`dense_pages`、`zero_ratio` 和
`dense_path`。

## 当前限制

- Lazy+Prefetch-Remote 还没有实现。
- workload URL 目前硬编码在 `simple-test/utils/workload_run.py`，配置文件中的
  `workload/control_host/control_port` 暂时不会生效。
- 当前没有统一的多轮 runner 或结果聚合脚本；需要外层脚本多次调用这些入口并保存 stdout。
- `copied_pages` 只是按 uffd 日志行数计数，依赖 handler 每 copy 一页输出一行。
- `workload-control/README*.md` 里仍有旧的 `motivation-agent:5000` 描述；当前
  `simple-test` 路径以 `bench-daemon:8080` 为准。
