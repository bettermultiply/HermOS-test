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

Lazy 组还需要编译 Firecracker examples 中的 uffd handler，例如：

- `uffd_on_demand_handler`
- `uffd_remote_range_handler`

当前仓库里的 lazy 组配置默认指向：

- `{root}/firecracker-build-scripts/firecracker_src/build/cargo_target/release/examples/uffd_on_demand_handler`
- `{root}/firecracker-build-scripts/firecracker_src/build/cargo_target/release/examples/uffd_remote_range_handler`

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

当前 workload 由 `simple-test/utils/workload_run.py` 中的 `WORKLOAD_URL`
单点决定，当前默认是：

```python
WORKLOAD_URL = "http://172.16.0.2:8080/cli-pipeline"
```

单独运行某个实验组时，默认会使用这个常量。批量脚本
`scripts/run_four_groups.py` 会通过环境变量 `WORKLOAD_ID` 自动覆盖 path，
依次运行默认 workload 列表：`health-daemon`、`health-exec`、`read-list`。
如果加上 `--with-agent-tool-replay`，才会把 `agent-tool-replay` 追加进去。
如果要手工切换单次运行的 workload，也可以在启动前设置：

```bash
sudo WORKLOAD_ID=read-list \
  python3 fetch-motivation/simple-test/Lazy-Local.py
```

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

如果要用同一个并发数批量跑四个默认实验组（`Eager-Local`、`Eager-Remote`、
`Lazy-Local`、`Lazy-Remote-Dedup`），并自动轮流跑一组 workload
（默认是 `health-daemon`、`health-exec`、`read-list`），可以用：

```bash
sudo python3 fetch-motivation/scripts/run_four_groups.py --count 4
```

如果要连续跑多轮，例如“10 并发，跑 10 轮”，可以用：

```bash
sudo python3 fetch-motivation/scripts/run_four_groups.py --count 10 --repeats 10
```

可选参数：

- `--repeats <n>`：同一并发数连续跑多少轮，默认是 `1`。
- `--log-dir <path>`：每组 stdout/stderr 的日志目录，默认是
  `fetch-motivation/data/batch-runs/`。
- `--with-lazy-remote`：额外再跑 `Lazy-Remote`，作为可选第五组。
- `--with-agent-tool-replay`：把 `agent-tool-replay` 追加到默认 workload 列表末尾。
- `--workloads <id...>`：覆盖默认 workload 列表，自定义运行顺序。

这个 runner 会临时把对应配置文件里的 `count` 改成指定值，逐组运行后再恢复原值。
`data/batch-runs/` 下的目录规则是：

- 不同并发数使用不同目录，例如 `count_1`、`count_4`、`count_16`。
- 相同并发数下的不同批量运行轮次使用 `run_<n>`。
- 分配时优先使用第一个缺失的编号；例如已有 `run_1`、`run_3`、`run_4`，下一次会用 `run_2`。
- 当中间没有空洞时继续递增；例如已有 `run_1` 到 `run_4`，下一次会用 `run_5`。
- 每一轮还会把 `REPEAT_IDX` 自动写成实际分配到的 `run_<n>` 编号，用于 CSV 汇总。
- 每个 `run_<n>/` 下面按 `组名--workload名.log` 保存日志，例如
  `eager-local--health-daemon.log`。

每个脚本都会在 `finally` 中清理：

- Firecracker 进程
- uffd handler 进程
- dedup proxy 进程
- netns、tap、veth、iptables NAT
- `/tmp/fc-sock/fc<i>.sock`
- `/tmp/fc-sock/uf<i>.sock`

如果脚本被强杀，可能需要手工清理残留的 `fc<i>` netns 或 socket。

## 输出

脚本会打印过程日志，并在最后输出 JSON。同时会向
`fetch-motivation/data/experiment_runs.csv` 追加一行汇总记录。CSV 字段如下：

- `run_id`：本次运行的唯一 ID。
- `group_name`：实验组名称，例如 `eager-local`、`eager-remote`、`lazy-local`、
  `lazy-remote`、`lazy-remote-dedup`。
- `concurrency`：本次运行的 sandbox 并发数，对应配置里的 `count`。
- `repeat_idx`：重复实验编号；当前如果没有外层多轮 runner，默认写 `0`。如需覆盖，
  可以在启动前设置环境变量 `REPEAT_IDX`。
- `workload_id`：guest 实际返回的 workload 名称；如果本轮结果里没有该字段，则回退到
  当前 `WORKLOAD_URL` 的 path。
- `workload_ms_avg`：本轮所有 sandbox 的 `workload_ms` 平均值。
- `memory_pull_ms`：memory snapshot 下载耗时。没有这个操作时写 `0`。
- `snapshot_state_pull_ms`：snapshot state 下载耗时。没有这个操作时写 `0`。
- `sandbox_start_ms`：创建 netns、启动 Firecracker、启动 uffd handler、调用
  `/snapshot/load` 并 resume VM 的总耗时。
- `workload_run_ms`：host 并发发送 workload 请求到所有 sandbox 完成的墙钟时间。
- `roundtrip_ms_max`：本轮所有 sandbox 中最大的 `client_round_trip_ms`。
- `copied_pages_total`：本轮所有 sandbox 的 copied page 总数。没有这个操作时写 `0`。
- `total_time_ms`：`memory_pull_ms + snapshot_state_pull_ms + sandbox_start_ms + workload_run_ms`。

stdout JSON 中仍会保留更细的调试字段。主要字段：

- `snapshot_pull_ms`：remote 组下载 snapshot 数据的时间。Eager-Remote 还会拆成
  `memory_pull_ms` 和 `snapshot_state_pull_ms`。
- `sandbox_start_ms`：创建 netns、启动 Firecracker、启动 uffd handler、调用
  `/snapshot/load` 并 resume VM 的总耗时。
- `workload_run_ms`：host 并发发送 workload 请求到所有 sandbox 完成的墙钟时间。
- `roundtrip_ms_max`：本轮所有 sandbox 中最大的 workload 请求 round-trip 时间。
- `total_time_ms`：本次运行的总墙钟时间。
- `copied_pages`：Lazy 组从每个 `uffd-<i>.log` 中读取 handler 退出时输出的
  `COPIED_PAGES=<n>` summary。旧日志没有 summary 时，仍会回退到按非空行数估算。
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
runtime-artifacts/<group>/run_<id>/firecracker-<id>.log
runtime-artifacts/<group>/run_<id>/uffd-<id>.log
runtime-artifacts/<group>/range-dedup-proxy.log
```

## 绘图

`scripts/plot_transfer_execution.py` 会从 `data/experiment_runs.csv` 读取汇总结果，
绘制三子图 grouped bar 组合图：

- 默认 workload 顺序是 `health-exec`、`read-list`、`agent-tool-replay`。
- y 轴口径是 `memory_pull_ms + snapshot_state_pull_ms + workload_ms_avg`，
  即 transfer time + execution time，不包含 `sandbox_start_ms` / restoration time。
- 同一并发度的多轮实验取 median。
- 每个 concurrency 是一组柱，组内顺序为 `Eager / local`、`Lazy / local`、
  `Eager / remote`、`Lazy / remote`。
- eager 柱顶的浅色覆盖部分表示 transfer portion，即
  `workload_ms_avg -> memory_pull_ms + snapshot_state_pull_ms + workload_ms_avg`。
- 默认使用对数 y 轴；如需线性 y 轴，可加 `--yscale linear`。

生成 SVG、PNG 和 PDF：

```bash
python3 fetch-motivation/scripts/plot_transfer_execution.py
```

输出路径默认是：

```text
fetch-motivation/data/figures/transfer_execution_breakdown.{svg,png,pdf}
```

如果要把轻量 workload 改成 `health-daemon`，可以指定：

```bash
python3 fetch-motivation/scripts/plot_transfer_execution.py \
  --workloads health-daemon read-list agent-tool-replay
```

如果要生成线性 y 轴对照版：

```bash
python3 fetch-motivation/scripts/plot_transfer_execution.py --yscale linear
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
- workload URL 目前故意硬编码在 `simple-test/utils/workload_run.py`，用于让所有实验组共用同一个切换点。
- 当前只有 `scripts/run_four_groups.py` 这一类固定编排 runner；如果后续要做更复杂的并发数 sweep、结果聚合或画图，仍然需要额外脚本。
- `workload-control/README*.md` 里仍有旧的 `motivation-agent:5000` 描述；当前
  `simple-test` 路径以 `bench-daemon:8080` 为准。
