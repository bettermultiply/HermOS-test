# TODO：rootfs-predict 后续实验补充

这个文件记录当前论文观察章节暂不展开、但后续应补强的实验工作。

## 1. 补充 same-repo 不同任务的数据覆盖

当前 same-repo 不同任务的结果方向很强，但有效独立样本偏少：目前主要是 2 个仓库、20 个 task pair。后续如果要更稳健地声称“同仓不同任务之间 rootfs 访问也稳定”，需要增加仓库覆盖面。

建议目标：

- 覆盖 5 到 6 个仓库。
- 每个仓库选择约 5 个任务。
- 每个任务重复运行 5 次。
- 继续使用 `phase=workload`、`metric_kind=block` 的统计口径。

需要重点确认：

- rootfs 在 same-repo 不同任务之间是否仍保持较高相似度。
- workspace 在 same-repo 不同任务之间是否仍显著低于 rootfs。
- 报告样本量时使用 task pair 作为有效单位，不要把 run pair 当作完全独立样本。

## 2. 在 Evaluation 中补充 directional prefetch 实验

当前章节的数据主要证明访问集合的相似性和稳定性，不能单独作为“预取一定有效”的最终证据。真正的预取效果应放到 Evaluation 中用 held-out run 评估。

建议实验设计：

- 用一次历史运行的访问集构建 prefetch set，预测下一次运行。
- 或者用前 k 次运行的 union/core 构建 prefetch set，预测 held-out run。
- 分别评估 rootfs-only prefetch、workspace-only prefetch、full-trace replay。

建议指标：

- Directional coverage：`covered_blocks / target_run_blocks`。
- Overfetch ratio：`prefetched_blocks / target_run_blocks`。
- Unused prefetch ratio：`unused_prefetched_blocks / prefetched_blocks`。
- 可选：按 rootfs/workspace 分开报告，避免 full-trace replay 的平均值掩盖 workspace 的不可预测性。

需要重点确认：

- 稳定的 rootfs 访问是否能带来高覆盖、低浪费的预取效果。
- full-trace replay 是否在 workspace 部分覆盖不稳定，或者引入明显带宽浪费。
- high-cover 只能说明两个集合之间存在包含关系，不能直接等价为真实预取方向上的 coverage，因此 Evaluation 中必须使用 directional coverage。

## 3. 图注中的样本量口径

论文正文可以不展开样本量细节，但图注需要写清楚有效样本单位。

建议图注写法：

- ECDF 图：注明曲线基于 run-pair 计算，但统计独立性主要来自 task/task-pair；例如 same-task 为 20 tasks，same-repo 为 20 task pairs，other-repo 为 170 task pairs。
- 稳定性散点图：注明每个点是一个 `task_id + drive_id`，当前为 20 tasks，因此 rootfs/workspace 各 20 个点。
- 不建议只写 run-pair 数量，否则容易让读者误以为几千个点都是独立样本。
