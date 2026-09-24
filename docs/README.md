# 文档索引

**现状和待办不在这里**，在根目录 [README](../README.md)；开发规则在 [CLAUDE.md](../CLAUDE.md)。
这里只是地图：每篇讲什么、什么时候该看。

## 阅读路径

- **第一次接触**：[README](../README.md) → [结论与风险](conclusions-and-risks.md) → [CLAUDE.md](../CLAUDE.md)
- **要改代码**：[CLAUDE.md](../CLAUDE.md)，再看下面对应模块的专题文档
- **要对外讲结论**：[结论与风险](conclusions-and-risks.md)，数据出处在「结果」一栏
- **想知道某个决定为什么这么做**：[开发日志](history/dev-log-2026-09.md)，代码注释里的「开发日志 七-x.y」就指它

## 当前参考（描述现在的系统，随代码更新）

| 文档 | 什么时候看 |
|---|---|
| [结论与风险](conclusions-and-risks.md) | 对外讲任何数字之前；「能说什么 / 不能说什么 / 风险」 |
| [产品缺陷清单](product-defects.md) | 上报缺陷；代码注释里的「产品缺陷 #N」 |
| [测量正确性](measurement-correctness.md) | 改耗时 / 用量 / 工具调用口径之前 |
| [逐轮 ErrorCalls 采集](error-calls-collection.md) | 改 NewAPI 后台日志采集与判定 |
| [逐请求账本与采集代理](model-request-ledger.md) | 改 `runner/modelproxy/`，或读 `/run`、`/suite` 的逐请求数据 |
| [整轮模型调用监控待办](model-call-monitoring-todo.md) | 监控这条线的明细待办和验收矩阵（总待办在 README） |
| [YonWork 1.0.10 归属探针与 hook 扩展](yonwork-1.0.10-correlation-probe.md) | 改 `plugins/benchmark-trace-bridge/`；YonWork 升级后重验归属 |
| [NewAPI / 统一代理超时排查](newapi-stall.md) | **跑批变慢、请求挂住时先看** |

## 结果（某次实验的数据，不随代码更新）

| 文档 | 内容 |
|---|---|
| [四模式横向对比 2026-09-22](four-way-comparison-20260922.md) | 首份成规模对比：YonWork / WorkBuddy × 两种通路，48 轮 |
| [YonWork hook 开销对照 2026-09-24](hook-overhead-20260924.md) | 开—关—开三批短文本；归属效果与耗时差值的证据边界 |

## 历史（已完结或被取代，只读）

结论已经吸收进上面的文档或 CLAUDE.md；**与现状冲突时以现状为准**。

| 文档 | 内容 |
|---|---|
| [开发日志 2026-09](history/dev-log-2026-09.md) | 原 CLAUDE.md 第五、七节：PAD 迁移对照、按批次的待办与实测全过程 |
| [YonWork 自动化入口调查](history/yonwork-automation-report.md) | 1.0.8 时点的 Host API / CLI / CDP 全量调查，**Host API 细节仍以它为准** |
| [WorkBuddy 入口摸底](history/workbuddy-probe-report.md) | WorkBuddy CLI 形状、输出格式、会话隔离方式 |
| [WorkBuddy → NewAPI 接入验收](history/workbuddy-newapi-validation.md) | WorkBuddy 经 NewAPI 的单次调用验收 |
| [模型入口与任务关联验证](history/model-entry-validation.md) | 逐请求账本的前置验证：原生头能否精确关联 |
| [客户端体验探针 · 协议](history/client-probe-protocol.md) | 预注册判据（看数据前定稿） |
| [客户端体验探针 · 结果](history/client-probe-results.md) | S1–S7 共 21 轮，探针结项依据 |

## 维护规矩

- 专题完结或被新文档取代：`git mv` 进 `history/`，更新本索引，修好引用它的链接
  （`grep -rn <文件名>` 查代码注释和其他文档）。
- 新专题文档开头写日期和范围；状态变化回头改文档开头，不在末尾无限追加。
