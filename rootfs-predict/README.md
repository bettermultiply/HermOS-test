# rootfs-predict

这个目录用于研究 Firecracker snapshot restore 后，LLM Agent 在 SWE-bench
任务中的 rootfs/workspace 访问规律。

当前实验不评估性能瓶颈，不要求任务最终修复成功，也暂时不实现 prefetch。目标是先回答一个更基础的问题：

> 基于一次历史访问 trace 直接预测下一次 snapshot restore 后要预取的 block，是否足够可靠？

更长期的目标是：输入历史 path/block trace，输出下一次 snapshot restore 后应该预取的 block set。当前阶段只记录访问集合并分析相似度。

## 实验问题

1. 同一个任务重复运行时，rootfs/workspace 的 block 访问集合是否稳定？
2. 同一个任务重复运行时，rootfs/workspace 的静态文件读取集合是否稳定？
3. same repo 不同 task 和 different repo 不同 task 之间，是否存在可复用的访问规律？
4. 单次历史 block trace replay 是否不足以覆盖下一次 Agent 运行的访问集合？

## 当前运行逻辑

入口脚本：

```sh
scripts/run/run_fc_task.py <instance_id> <run_id>
```

当前代码使用 Firecracker snapshot：

1. 从 SWE-bench dataset 读取 instance，在 `snapshots/<instance_id>/` 缓存 `task.json` 和 `prompt.txt`。
2. 如果该 instance 的 snapshot 不存在，先准备 snapshot：
   - 创建 rootfs/workspace/upper 镜像。
   - 在 VM 内 clone repo，并 checkout 到 `base_commit`。
   - 挂载 workspace overlay。
   - 在 snapshot 前运行一次 prompt 为 `hi` 的 Codex warmup，使 Codex 启动和基础依赖读取进入快照状态。
   - `sync` 后 pause VM，保存 `vm.state` 和 `vm.mem`。
   - 离线导出 `rootfs.ext4` 和 `workspace.ext4` 的 `block atlas`，把 block 标记为 `file_data`、`dir_data`、`inode_table`、`block_bitmap`、`inode_bitmap`、`superblock_or_gdt`、`journal`。
3. 每次 run 从同一个 snapshot 恢复，并使用 fresh upper 镜像。
4. 恢复后运行 Codex：

   ```sh
   codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -
   ```

5. Firecracker 记录 block trace。
6. 在启动 Codex 前，把当前 `block_trace.csv` 改名为 `preworkload_block_trace.csv`。
7. 再运行真实任务 Codex；之后 Firecracker 会继续按同一路径写新的 `block_trace.csv`，Codex 退出后再把它改名为 `workload_block_trace.csv`。
8. 解析两个 phase 的 block trace，并结合 snapshot block atlas，追加到 `data/*.csv`。

当前代码把“snapshot 恢复后到真实 Codex 启动前”的 block 访问标为 `preworkload`；把“真实 Codex 启动到退出”的 block 访问标为 `workload`。Codex 基础启动路径默认已在 snapshot 前用 `hi` prompt 预热。

## 访问记录

### block_phased.csv

`data/block_phased.csv` 记录每个 phase 中 lower drive 上第一次被读取到的 block：

```text
run_id,task_id,phase,drive_id,op,offset,length
```

当前代码写入两类 phase：

```text
preworkload
workload
```

其中 `preworkload` 表示 snapshot 恢复后到真实 Codex 启动前的访问；`workload` 表示真实 Codex 启动到退出的访问。

### file_read_summary.csv

`data/file_read_summary.csv` 记录通过 “原始 block trace + block atlas” 归因得到的 `file_data` 文件级首次读取汇总：

```text
run_id,task_id,phase,drive_id,path,inode,first_ts_ns,blocks_read
```

- `drive_id=rootfs` 表示 rootfs lower 静态文件。
- `drive_id=workspace` 表示 workspace lower 静态文件，路径按 guest 中的 `/workspace/repo/...` 记录。
- `phase` 为 `preworkload` 或 `workload`。
- `blocks_read` 表示该 run/phase 中命中的唯一 block 数。

### file_read_blocks.csv

`data/file_read_blocks.csv` 记录更细粒度的 block 分类和归因结果：

```text
run_id,task_id,phase,timestamp_ns,drive_id,block_id,class,path,inode,logical_block
```

其中 `class` 当前可能包括：

```text
file_data
dir_data
inode_table
block_bitmap
inode_bitmap
superblock_or_gdt
journal
```

`path/inode/logical_block` 只在能归到具体文件或目录时有值。

### block_class_summary.csv

`data/block_class_summary.csv` 按 run/phase/drive 汇总首次读到的 block class 数量：

```text
run_id,task_id,phase,drive_id,class,blocks_read
```

### unmapped_read_blocks.csv

`data/unmapped_read_blocks.csv` 记录当前 atlas 未覆盖的首次读取 block：

```text
run_id,task_id,phase,timestamp_ns,drive_id,block_id
```

这些 block 可能来自 atlas 未覆盖的 ext4 元数据，或导出逻辑尚未标注的对象。

## 运行实验

运行单个任务一次：

```sh
scripts/run/run_fc_task.py astropy__astropy-12907 run-1
```

如果 `runs/<instance_id>/<run_id>/` 已存在，脚本会自动递增尾部数字直到找到不存在的 run id。例如 `run-1` 已存在时会尝试 `run-2`；`run` 已存在时会尝试 `run-1`。

按任务列表重复运行：

```sh
scripts/run/run_fc_batch.sh <task_file> <repeat_count>
```

并发运行不同 task：

```sh
scripts/run/run_fc_batch.sh <task_file> <repeat_count> <jobs>
```

也可以用环境变量设置并发数：

```sh
FC_BATCH_JOBS=4 scripts/run/run_fc_batch.sh <task_file> <repeat_count>
```

`jobs` 表示同时运行的 instance 数。每个 instance 内部的 repeat 仍按顺序执行，因为同一 instance 会共享 `snapshots/<instance_id>/active-upper.ext4`。脚本会用文件锁保护同一 instance 的 snapshot/upper 镜像，并保护 `data/*.csv` 的追加写入。

`task_file` 每行一个 SWE-bench `instance_id`，空行和 `#` 开头的行会被跳过。

建议至少准备两类任务列表：

1. same repo 不同 task。
2. different repo 不同 task。

## 依赖和输入

默认数据集：

```text
SWE-bench/SWE-bench_Verified
```

可以用环境变量切换 dataset：

```sh
DATASET_NAME=/path/to/SWE-bench_Verified/test.parquet scripts/run/run_fc_task.py <instance_id> <run_id>
```

默认运行资源：

```text
bin/firecracker-block-trace
../firecracker-build-scripts/vmlinux-6.1.155
../firecracker-build-scripts/rootfs_file_codex
../firecracker-build-scripts/ubuntu-24.04.id_rsa
```

rootfs 内需要具备：

```text
ssh
git
python3
codex
可以访问 GitHub 和 Codex 所需网络
```

Agent 命令可以通过 `AGENT_CMD` 覆盖：

```sh
AGENT_CMD='codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -' \
scripts/run/run_fc_task.py <instance_id> <run_id>
```

snapshot 前 Codex warmup 的 prompt 默认是 `hi`，可以通过 `WARMUP_PROMPT` 覆盖：

```sh
WARMUP_PROMPT='hi' scripts/run/run_fc_task.py <instance_id> <run_id>
```

## 输出目录

每次 run 的原始输出：

```text
runs/<instance_id>/<run_id>/
```

其中：

```text
block_trace.csv              运行中当前 phase 正在写入的 trace 路径
preworkload_block_trace.csv  snapshot 恢复后到真实 Codex 启动前的 block trace
workload_block_trace.csv     真实 Codex 启动到退出的 block trace
upper.ext4                   本次 run 的 fresh upper 镜像
guest_run.sh                 本次 run 使用的 guest workload 脚本
firecracker.log              本次 run 的 Firecracker 日志
agent.out                    Codex stdout
agent.err                    Codex stderr
status                       Agent 退出状态码
```

每个 instance 的 snapshot：

```text
snapshots/<instance_id>/
```

其中：

```text
task.json                    SWE-bench instance metadata
prompt.txt                   传给 Agent 的任务 prompt
firecracker.json             该 snapshot 使用的 Firecracker 配置
rootfs.ext4                  snapshot rootfs 镜像
workspace.ext4               repo/base commit workspace 镜像
upper.ext4                   每次 run 复制的 clean upper 基础镜像
active-upper.ext4            Firecracker 配置引用的活动 upper 镜像
vm.state                     Firecracker snapshot 状态
vm.mem                       Firecracker snapshot 内存
rootfs_block_atlas.csv       rootfs block atlas
workspace_block_atlas.csv    workspace block atlas
```

全局汇总 CSV：

```text
data/block_phased.csv
data/file_read_summary.csv
data/file_read_blocks.csv
data/block_class_summary.csv
data/unmapped_read_blocks.csv
```

## 分析

运行：

```sh
python3 scripts/analysis/analyze.py
```

当前分析使用 Jaccard 相似度：

```text
共同访问对象数量 / 总访问对象数量
```

输出包括：

1. 同一任务内 rootfs block 集合相似度。
2. 同一任务内 workspace block 集合相似度。
3. 同一任务内 rootfs `file_data` / `dir_data` / shared metadata 的 block 相似度。
4. 同一任务内 workload 阶段 rootfs/workspace 静态文件集合相似度。
5. 同一任务内 preworkload 阶段 rootfs/workspace 静态文件集合相似度。
6. rootfs/workspace 文件的跨任务覆盖率。

如果某个 task 只有一次 run，则同任务相似度没有可比较对象，结果不能解释为稳定或不稳定。

## 当前边界

当前实验只研究访问集合规律：

- 不评估运行时间。
- 不评估任务是否成功修复。
- 不实现 prefetch。
- 不证明性能瓶颈。

当前重点是 lower rootfs/workspace 上已有静态文件和 ext4 metadata 的 cold read。运行期创建文件、upper 层 copy-up 和 syscall 语义不在本轮统计口径内。
