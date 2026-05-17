# fc-restore-bench

Firecracker 快照恢复瓶颈分析工具。

## 动机

Firecracker 快照恢复是串行流程，前序步骤的瓶颈会掩盖后续步骤的真实开销。本工具将恢复过程中的关键操作（KVM ioctl、内存映射、设备恢复等）拆成独立函数，在可控并发下单独执行并计时，暴露被串行化掩盖的隐藏瓶颈。

## 用法

```bash
cargo build --release

# 100 次 kvm_create_vm，全并发（100 线程同时跑，默认采集 per-op rusage）
./target/release/bench -p kvm_create_vm -t 100

# 100 次，8 线程
./target/release/bench -p kvm_create_vm -t 100 -c 8

# 关闭 per-op rusage，只保留每次操作的 wall-clock latency
./target/release/bench -p kvm_create_vm -t 100 -c 8 --no-per-op-usage

# 查看可用操作
./target/release/bench -p help

# 遍历运行全部 op，指定总次数和并发
scripts/run.sh -t 100 -c 8
```

结果自动写入 `results/<op>.csv`。每条记录现在包含：

- `instance_id`：全局实例编号，便于按实例画时间线
- `start_us` / `end_us`：相对本次 bench run 起点的绝对时间点
- `elapsed_us`：单次阶段耗时

## 画图

```bash
python3 scripts/plot.py results/*.csv
```

`plot.py` 会为每个 op 绘制时间线图：横轴是相对 run 起点的 timeline，纵轴是按开始时间排序后的实例索引，每一行代表一个实例。

## 参数

| 参数 | 含义 | 默认值 |
|------|------|--------|
| `-p` | 操作名 | 必填 |
| `-t` | 总操作次数 | 必填 |
| `-c` | 并发线程数 | 等于 `-t` |
| `--no-per-op-usage` | 关闭每次操作的线程级 rusage 采集 | 默认采集 |
