# WorkBuddy → NewAPI 接入验收

日期：2026-09-22。仅验收用户已配置的模型，未修改 WorkBuddy 配置、凭据或登录态。

## 已验证

驱动读取的 `.workbuddy/models.json` 新增 1 个模型：`deepseek-flash`，
vendor 为 `Custom`，URL 为 `http://127.0.0.1:3000/v1`，声明支持工具调用。
实际请求证明本机当前版本接受 `/v1`；没有按早期文档猜测将它改成完整 completions 路径。

通过 Web 提交 `smoke`、WorkBuddy、显式模型 `deepseek-flash`、1 轮普通问候。
工具保持关闭。当时没有常驻 Worker，使用持有全局排他锁的宿主机 Worker `--once` 执行，
任务结束后退出。

| 项目 | 结果 |
|---|---|
| Job | `100c7c1dd9234fe3ba5031a51ccee93e`，Completed |
| Batch | `web-20260922-021812-100c7c1d` |
| Benchmark | `web-100c7c1d-Case02-r1-1a0c6e81277` |
| 判定 | Pass；`result:success`；模型匹配检查通过 |
| CLI 请求 / 实际模型 | `deepseek-flash` / `deepseek-flash` |
| CLI input / output / total | 3688 / 9 / 3697 |
| 外层 / CLI 自报内部耗时 | 5.792s / 2.207s |
| 同时间窗 NewAPI 日志 | 1 条消费记录；模型 `deepseek-flash`；input=3688、output=9 |

原始轮次和 CLI 输出位于 `results/<batch>/`；这些产物不进入 Git。
本次后台关联依据是隔离时间窗、模型和一致的 token 数，尚不是 runId 精确关联。
自动事后对账已在 MySQL 保存 `workbuddy-cli` 与 `newapi` 两个来源；后台行
APICalls=1、ErrorCalls=0、matched_by=`time-window`。一次性 Worker 退出后，实查排他锁已释放。

## 尚未覆盖

- WorkBuddy 驱动逐轮只采 `workbuddy-cli`；本轮 JSONL 的 ErrorCalls 仍为 null，检查未执行。
  Worker 的事后 NewAPI 对账与逐轮判定是不同环节，不能据此声称错误检查已接通。
- 未验证工具调用经过此网关时的多请求聚合、失败重试、辅助模型或子代理的路由。
- 不能把 CLI 最后一条 assistant 的模型/usage 视为复杂任务的完整逐调用记录。
- 没有验证美元费用换算，也没有验证桌面 UI 延迟或完整分布式追踪。

结论：**WorkBuddy 经本机 NewAPI 的单轮文本调用已实测打通；全链路监控尚未完成。**
