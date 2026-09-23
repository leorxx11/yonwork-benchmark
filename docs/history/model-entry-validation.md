# 模型入口与任务关联验证

日期：2026-09-22。范围：待办第一阶段的主模型入口和精确关联验证。

**版本边界更新：YonWork 1.0.10 的两轮隔离探针不再携带下述旧版关联头。**
现有账本仍能记录请求，但精确归属需新增适配；可用 hook 与证据见
[1.0.10 兼容性探针](../yonwork-1.0.10-correlation-probe.md)。以下原结论限定于 1.0.8。

## 结论

**YonWork 1.0.8 与 WorkBuddy 5.5.6 的主模型请求可以通过共享采集入口，
并准确关联到 benchmark 轮次，再关联到 NewAPI 消费日志。**
两款产品已自带任务关联请求头，不必为每一轮修改产品模型账户或生成新网关令牌。
此结论来自真实 HTTP 请求及后台记录，不只是读取代码或配置。

已验证的是普通文本主模型路径；工具续答、重试、辅助模型、子代理与取消尚未完成验收。
目前只有独立探针，尚未接入正式 runner / Web，也不能宣称整轮全量监控完成。

## 实际链路与关联字段

```text
BenchmarkId
  → YonWork Host API / WorkBuddy CLI
  → 临时本机采集入口（同一产品两轮复用相同 URL）
  → NewAPI /v1/chat/completions
  → 上游模型
```

| 层级 | 实测关联字段 | 结论 |
|---|---|---|
| YonWork → 采集入口 | `x-yonwork-run-id`、`x-yonclaw-run-id` | 都严格等于本轮 BenchmarkId；另有 session-key 请求头 |
| WorkBuddy → 采集入口 | `X-Conversation-ID` | 严格等于本轮 CLI `--session-id` / BenchmarkId |
| WorkBuddy 自定义请求头 | `X-Benchmark-Run-Id` | 通过进程级 `CODEBUDDY_CUSTOM_HEADERS` 与 `WSLENV` 注入成功；关联验证未依赖它 |
| NewAPI → 采集入口 | 响应头 `x-oneapi-request-id` | 与 `/api/log/self` 返回记录的 `request_id` 完全一致 |

两款产品均实际请求 `POST .../v1/chat/completions`，`stream=true`；
配置 URL 中的路径前缀被保留。4 次真实响应均包含 usage 和 SSE `[DONE]`。
这条链路可以用“产品任务 ID → 采集请求 ID → NewAPI request_id”实现精确对账；
API 返回的查询内 `id` 仍不能用作持久关联键。

正式采集入口需要预注册任务、检查来源和凭据，把未知标识留作未归属记录。
请求头本身不是认证手段，不能将任意外部传来的任务 ID 直接视为可信。

## 实验与结果

真实转发批次：`model-entry-20260922-105916-10beec`。
以下 run 后缀均接在 `entry-20260922-105916-10beec-` 后。
各轮均收到预期回答 `ENTRY_OK` 和产品终止事件。

| Run 后缀 | 主模型请求数 | 代理 input / output | NewAPI input / output | 请求 ID 精确匹配 |
|---|---:|---:|---:|---|
| workbuddy-1 | 1 | 3696 / 4 | 3696 / 4 | 是 |
| workbuddy-2 | 1 | 3696 / 4 | 3696 / 4 | 是 |
| yonwork-1 | 1 | 16458 / 4 | 16458 / 4 | 是 |
| yonwork-2 | 1 | 16453 / 4 | 16453 / 4 | 是 |

NewAPI 的 `/api/log/self` 与只读 SQLite 查询都命中这 4 个 `request_id`。
时间窗只用于拉取日志候选，最终匹配依据是请求 ID。这些数字不用于性能或成本对比。

额外注入 6 条不转发到上游的请求：

- 两款产品各一条：第 2 轮已注册时，对相同入口发无任务标识请求，返回 404，未归属任何轮次。
- 两条未知路径请求：返回 404，未归属任何轮次。
- 两条带第 1 轮标识的模拟迟到请求：在第 2 轮结束后发起，仍归属第 1 轮，返回 410。
  这是探针验证隔离的策略，不代表产品不会产生真实后台请求。

最初两批模拟实验分别为 `model-entry-20260922-105531-25d759` 与
`model-entry-20260922-105810-313b97`；每批 WorkBuddy 两轮成功、YonWork 一轮成功一轮失败。
失败不得丢弃：其中首批 YonWork 第 2 轮的会话记录明确显示
`missing-provider-auth` / `No API key found for provider`，模型请求没有到达采集入口。
第二批失败未单独提取同等诊断证据，不能仅按症状视为完全相同原因。

YonWork 新建账户的接口以 `waitForGatewayApply:false` 同步运行时（已核对安装包代码）。
最终探针复用每产品单一入口，并在创建后等待 5 秒，真实批次两轮均通过。
**5 秒只是实验准备等待，不是已验证的就绪判据或修复。**
正式实现应固定入口并检查就绪，不在测量轮次中反复新建账户。

## 路由范围与任务收尾

| 路线 | 现状 |
|---|---|
| YonWork 显式 custom / NewAPI 主模型 | 实测可经过采集入口，原生 run ID 可用 |
| YonWork 默认模型 | 当前仍指向产品自己的 `/api/open-platform-model/v1`，不经过本次入口 |
| YonWork 辅助模型、子代理 | 当前主 agent 未显式声明独立子代理模型；不能据此推定所有请求继承本轮配置，待动态验证 |
| WorkBuddy custom / NewAPI 主模型 | 实测可经过采集入口，原生 conversation ID 可用 |
| WorkBuddy lite / reasoning | 自带文档声明独立解析链；当前 settings 未显式配置 `variantModels`，实际辅助请求待验证 |
| WorkBuddy 子代理 | 自带文档明确不读取 `relatedModels.subagent`；有独立解析链。代码包含 parent-conversation 请求头，但本次未实际运行子代理 |

本次各轮模型响应结束后收到产品终止事件，但只覆盖单请求任务。
正式收尾至少要分别记录产品终止、代理在途请求和已知子任务状态；
迟到请求继续保留旧归属、更新覆盖状态，不能借安静等待窗口断言再无请求。
真实重试、取消、后台子任务结束和未知路由的处理仍待验证。

## 复现与副作用

```bash
# 本地模拟响应，无模型费用
.venv/bin/python -m scripts.probe_model_entry --shared-entry

# 两款产品各两轮，真实经过当前已配置的本机 NewAPI
.venv/bin/python -m scripts.probe_model_entry --shared-entry --forward-newapi
```

前提：YonWork 已启动并登录，WorkBuddy 安装可用且已配置 `deepseek-flash`，MySQL 可连接，
无 Worker 持有排他锁。探针持有与正式 runner 相同的锁；工具关闭或提示不调用工具，
未启用 `bypassPermissions`。不往正式 benchmark 结果表插入这些探针样本。

探针会通过 YonWork API 新增临时 provider，产生测试会话及产品自身日志；
WorkBuddy 使用临时隔离配置。真实转发使用已有本机 NewAPI 凭据，仅在内存读取，
临时 provider 使用探针生成的独立凭据。仅保存元数据，不保存请求正文、响应正文或鉴权头。

本次结束后核对：原 YonWork provider 列表完全一致，临时账户已删除；
WorkBuddy 原 `models.json` / `settings.json` 哈希未变；临时配置目录已删除，
监听器已关闭，数据库排他锁已释放。测试会话和审计记录作为实验痕迹保留。

证据保存在 `results/<批次>/evidence.json`；真实批次另有
`newapi-request-id-check.json`，记录只读后台核对结果。`results/` 不入 Git。
脚本是本地受控入口探针，不是正式代理：未覆盖所有协议、长响应、故障恢复或子任务生命周期。

下一步：先实现带原生任务关联与 NewAPI request_id 的逐请求账本，再用可控的工具续答、
重试和子代理场景验证覆盖。无需从每轮临时 provider 或单纯时间窗匹配开始。
