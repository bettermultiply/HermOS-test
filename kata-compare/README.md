# kata-compare

这个目录用于设计和执行“我们的系统 vs Kata”的对比实验。

当前这一轮实验的核心约束是：

- 被测对象是 **Sandbox 启动路径**，不是 Sandbox 内部 workload/container 的启动路径。
- 对 Kata 基线而言，**`crictl runp --runtime=kata ...` 成功返回** 就视为本次启动阶段完成。
- `runp` 成功后 **不立即 kill sandbox**；sandbox 应继续存活，直到本轮实验的后续观测完成，或统一清理阶段再停止。
- 因此，**不应该把 `crictl create/start`、也不应该把“在 Sandbox 内再启动一个 sandbox/容器”放进主指标**。

## 1. 实验目标

我们要回答的问题不是 “Kata 里面跑应用多快”，而是：

1. 从 host/CRI 发起一次 sandbox 创建请求开始，
2. 到 runtime 成功把 **PodSandbox** 拉起并返回 sandbox id 为止，
3. 这条路径的时延、资源占用、并发扩展性如何，
4. 与我们的系统相比差异在哪里。

这意味着 Kata 的对照组应该尽量收敛为：

```bash
crictl runp --runtime=kata <pod-config>.json
```

而不是：

```bash
crictl runp ...
crictl create ...
crictl start ...
crictl exec ...
```

后者混入了 sandbox 内 workload/container 生命周期，会把问题从
“sandbox bring-up” 变成 “pod 内业务启动”，口径已经变了。

## 2. 本轮建议口径

### 主指标

- `sandbox_core_create_ms`
  - 统一定义：从 host 侧正式发起一次 sandbox 创建请求，到该系统定义的
    “sandbox 创建成功返回点”为止的墙钟时间。
- `sandbox_ready`
  - 定义：创建请求成功，并返回非空 sandbox 标识。

### 次指标

- `inspect_state`
  - `crictl inspectp <pod_id>` 看到的状态，通常应为 `SANDBOX_READY`。
- `sandbox_survival_s`
  - 在不追加业务容器的前提下，sandbox 存活到统一清理前的持续时间。
- `host_cpu_user_ms` / `host_cpu_sys_ms`
  - 可选；从 `/usr/bin/time -v` 或外层采样拿。
- `host_rss_kb`
  - 可选；观察 shim、qemu、virtiofsd、guest 相关进程。

### 不纳入主指标的内容

- `crictl create/start` 普通容器
- sandbox 内执行命令
- guest 内业务 daemon 就绪
- 网络可达性检查
- 应用层首请求时延

这些可以作为“第二阶段实验”，但不能混入当前主图。

## 3. 推荐实验分组

### A 组：Kata 基线

目标：测量 Kata 通过 CRI 创建 PodSandbox 的纯启动路径。

流程：

1. 生成唯一 pod config。
2. 记录 `t0`。
3. 执行 `crictl runp --runtime=kata <pod-config>.json`。
4. 返回 pod sandbox id 时记录 `t1`。
5. 立即 `crictl inspectp <pod_id>` 校验状态。
6. sandbox 保持运行，不做容器创建。
7. 本轮全部测量/采样完成后统一 `stopp`/`rmp`。

主指标：

- `sandbox_core_create_ms = t1 - t0`

### B 组：我们的系统

目标：定义与 Kata **同口径** 的“sandbox 成功启动完成点”。

必须满足：

- 起点要与 A 组一致：host 发起一次 sandbox 启动请求。
- 终点要与 A 组一致：runtime/daemon 成功返回一个可用 sandbox handle，且 sandbox 已处于存活状态。
- 不能把 sandbox 内 workload 的成功作为终点。

当前我们把“我们的系统创建成功点”明确定义为：

```text
host 侧收到 Firecracker resume_vm API 的 HTTP 204 响应
```

也就是说，在我们的系统中：

- `t0`：host 侧开始处理一次 sandbox create request
- `t1`：调用 Firecracker `resume_vm` 后，客户端收到 HTTP `204`

因此主指标为：

- `sandbox_core_create_ms = t1 - t0`

建议同时保留分段指标，便于解释与 Kata 的差异：

- `fc_bootstrap_ms`
- `snapshot_load_ms`
- `resume_ack_ms`

如果我们的系统当前仍然会在 sandbox 启动后自动做额外初始化，需要拆开计时：

- `sandbox_core_create_ms`
- `post_create_init_ms`

用于主图时，应只拿前者与 Kata 对比。

## 4. 单轮实验状态机

建议把一次实验拆成四个阶段：

1. `setup`
   - 清理残留 pod sandbox
   - 生成配置
   - 准备日志目录
2. `launch`
   - 执行 `crictl runp --runtime=kata`
   - 成功返回后结束主计时
3. `observe`
   - `inspectp`
   - 可选采样宿主机进程、cgroup、内存
   - sandbox 在此阶段保持运行
4. `cleanup`
   - 统一停止并删除本轮 sandbox

这样设计的好处是，**launch 是主实验路径，observe 只是附加观测，不污染主时延口径**。

## 5. 并发实验建议

并发时也保持同样口径：

1. 同时发起 N 个 `runp`。
2. 记录每个 sandbox 的：
   - `t0_i`
   - `t1_i`
   - `pod_id_i`
3. 统计：
   - `sandbox_core_create_ms_i = t1_i - t0_i`
   - `sandbox_core_create_ms_p50/p95/p99`
   - `sandbox_core_create_ms_max`
   - `success_rate`

如果要画“整体完成时间”：

- `batch_complete_ms = max(t1_i) - min(t0_i)`

这个定义与之前 `fetch-motivation` 里的 `T_total_max` 思路一致，但这里终点是
`runp` 返回，而不是 workload 完成。

## 6. 最小 Kata 命令模板

```bash
crictl runp --runtime=kata configs/podsandbox.json
```

最小后处理：

```bash
crictl inspectp <pod_id>
```

统一清理：

```bash
crictl stopp <pod_id>
crictl rmp <pod_id>
```

## 7. 当前目录建议内容

- `configs/podsandbox.json`
  - 最小可运行 PodSandboxConfig 模板
- `scripts/run_kata_baseline.sh`
  - 单轮 Kata 基线脚本
- `scripts/run_kata_batch.sh`
  - 并发 Kata 基线脚本；每个并发实例单独落盘，批次级生成 `summary.json`
- 后续可补：
  - `scripts/collect_kata_metrics.py`
  - `results/`

## 8. 当前结论

这轮对比实验里，A/B 两组的终点定义应明确为：

```text
A 组（Kata）:
host 发起 runp -> Kata 成功启动 PodSandbox -> runp 返回 pod sandbox id

B 组（我们的系统）:
host 发起 create request -> Firecracker resume_vm -> host 收到 HTTP 204
```

到各自终点就应停止主计时。

Sandbox 之后继续存活，但那是为了：

- 做状态确认
- 做资源观测
- 做统一清理

而不是为了把 sandbox 内进一步启动动作纳入“启动时间”。
