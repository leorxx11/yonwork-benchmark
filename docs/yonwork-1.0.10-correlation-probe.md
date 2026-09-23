# YonWork 1.0.10 逐请求归属兼容性探针

日期：2026-09-22。范围：验证标识传递与替代接口。
**2026-09-23 更新：hook 扩展已实现并装机，实机 9/9 请求归属**，见文末「正式实现与实机验证」。

## 结论

**仅修改代理的请求头匹配规则不够；新版有可利用的模型调用 hook，需要补一份轮次与 trace 的映射。**

- 实际 Host API 两轮隔离调用均完成：模型入口只有产品新生成的 `traceparent`，
  没有旧的 `x-yonwork-run-id` / `x-yonclaw-run-id`。从 Host API 注入这类请求头，
  包括注入自己的 `traceparent`，均未透传至模型入口。
- 当前安装的 OpenClaw `model_call_started` / `model_call_ended` hook 同时提供
  `event.runId`、`event.callId` 和 `ctx.trace`；模型请求使用同一份 trace 生成请求头。
- 在**独立 Node 进程**调用当前安装包的真实包装函数和 hook dispatcher：
  8 次本地假模型调用、16 次 hook 回调，全部精确对应 runId 和请求 traceparent。
  覆盖成功、受控异常、诊断开/关；没有安装插件到实际 YonWork。

因此接口层面的方案成立。**实际产品加载扩展后的完整链路、工具续答、SDK 重试、
迟到请求、子代理等还没有验收**，不能把组件探针的 8 次调用说成 8 次产品端到端通过。

## 实际 Host API 探针

脚本：`scripts/probe_yonwork_correlation.py`。
证据目录：`results/yonwork-correlation-20260922-184108-45c6af/`（不进 Git）。

两轮共用一个临时模型账户和本机假模型入口。每轮使用全新的 BenchmarkId/sessionKey，
向 Host API 请求头注入独立 traceparent、`x-benchmark-run-id` 和 `x-yonwork-run-id`。
模型入口只记录允许的关联头、正文顶层字段名等元数据，返回固定 `ENTRY_OK`。
没有调用 NewAPI/DeepSeek，故这两轮的时间不是模型性能数据。

| 轮次 | 产品终止 | 模型请求数 | 旧轮次头 | 注入的 trace ID 是否保留 |
|---|---|---:|---|---|
| 1 | `chat.message:final`，收到 `ENTRY_OK` | 1 | 无 | 否 |
| 2 | `chat.message:final`，收到 `ENTRY_OK` | 1 | 无 | 否 |

请求体顶层只有 `messages`、`model`、`stream`、`stream_options`、`tools`、
`tool_choice`、`max_completion_tokens`；没有独立 runId 字段。
没有把消息正文中的提示文本、会话上下文或消息标记拿来建立关联。

源码与实测一致：Host API 的 `chat.send` 到内部 Gateway RPC 传递
sessionKey/message/idempotencyKey 等参数，没有转发这次注入的 HTTP 跟踪头。
模型调用包装函数自己生成子 span 并写入 `traceparent`。
这说明本次注入方式不能直接使用；并不证明所有可能的产品接口都不存在。

## 现有日志为什么不足以直接替代

`logs/trace-upload/trace-index.json` 有 runId，但其中 `traceId` 是 `yonwork-…`
形式的业务 trace 标识，不能当作 HTTP `traceparent` 中的 trace ID。

内部 `runtime/openclaw/logs/openclaw.log` 确实保存了 HTTP 对应的 `traceId`：
第一轮 5 行里有 1 行工具初始化日志同时包含 sessionKey，可以关联；
第二轮 4 行都没有轮次/会话标识。这种偶发的工具初始化日志不能成为完整归属依据。
本次检查的 llm-observer、trace-upload、Host 日志与会话文件中，未找到两轮都可用的导出映射。

这是已检查来源的边界，不是对产品所有诊断接口的排除结论。

## 可用的 hook 与组件验证

当前安装的产品版本由 `app.asar/package.json` 确认为 **1.0.10**。
核心文件与行号（文件 SHA-256 已保存到证据目录）：

- `resources/openclaw/dist/attempt.model-diagnostic-events-CfZQM0hs.js`
  - 4599：`baseModelCallEvent` 同时带 runId、callId、trace。
  - 4696：`modelCallHookContext` 将 trace 与 runId 交给插件。
  - 4709：派发 `model_call_started`。
  - 4773：`withDiagnosticTraceparentHeader` 生成模型请求的 traceparent。
  - 4902：同一 trace 用于 hook 和下游模型调用。
- `resources/openclaw/dist/hook-runner-global-BmIrGlLG.js`：真实 hook dispatcher。
- `resources/openclaw/dist/diagnostic-events-JZsXee1S.js`：trace 格式化与上下文。

组件脚本 `scripts/probe_yonwork_trace_hooks.mjs` 在独立进程中注册临时内存 hook，
调用当前安装包的包装函数；底层模型为进程内 stub，不连接外部服务。
脚本沿包装函数的实际 import 选择 dispatcher，避免安装包内多个副本造成假阴性。

实测输出：

```json
{"requests":8,"hook_events":16,"exact_run_and_trace_matches":true,"unique_call_spans":true,"success_and_error_covered":true,"diagnostics_enabled_and_disabled_covered":true}
```

hook 是异步、有界、允许失败的观察接口，**映射不保证先于 HTTP 请求到达**。
因此正式实现必须允许先记未归属、等映射到达后补关联，不能让代理等 hook 再转发请求。

## hook 的 runId 是否等于 BenchmarkId（2026-09-23）

组件探针的 runId 是脚本自己喂的，只证明 runId 与 traceparent 一致，
不证明产品里的 runId 就是我们的 BenchmarkId。本节补这一环，**未安装任何扩展**。

**结论：主对话路径上成立。** 本节是装扩展之前的推断；装上之后已直接观测证实，见下文。

1. **源码：两个 hook 同源。** `selection-JInn13lc.js` 的 `runEmbeddedAttempt(params)`
   （11424 行）里，模型调用包装函数收 `runId: params.runId`（13199 行），
   `llm_input` / `llm_output` 也收 `runId: params.runId`（14181 / 14747 行）。
   函数内 `params` 与 `params.runId` 都没有被重新赋值。
   包装函数把它原样放进 `event.runId`（`baseModelCallEvent`）和 `ctx.runId`
   （`modelCallHookContext`）；`callId` 形如 `${runId}:model:N`，自带 runId。
2. **实测：`llm_output` 的 runId 就是 BenchmarkId。** 产品自带的 llm-observer
   正是 `llm_input` / `llm_output` 的订阅者。四模式对比 v4 的两个 YonWork 批次
   （`0cef44fe` 统一代理、`c4c528ef` 默认模型）共 24 轮，
   observer 的 `runId` 与 `benchmark_id` **24/24 逐字相等（大小写保留）**，
   `sessionKey` 24/24 一致，没有含批次标记的多余 runId。
   `0cef44fe` 正是代理侧 13 请求 0 归属的那一批——runId 在产品内部一直在，
   只是没出现在 HTTP 请求上。

1 + 2 ⇒ 同一 attempt 内 `model_call_started.event.runId` = BenchmarkId。
**同日已直接观测到**，见下文「正式实现与实机验证」：10 次 hook 事件的 runId 全部等于 BenchmarkId。

⚠️ **上面引的是 `dist/`，但运行中的网关加载的是 `gateway-bundle.mjs`**
（`node.exe … gateway-bundle.mjs gateway --port …`）。已在 bundle 里复核：
包装函数 `runId:e.runId`、`nextCallId:()=>\`${e.runId}:model:…\``，
同一函数里 `runLlmOutput({runId:e.runId,…})`，结论不变。
以后查源码行为先查 bundle，`dist/` 只作可读参照。

**边界（未覆盖）：**

- **Compaction**：`compact-DLB4d8IL.js` 419 行用
  `` `${params.runId ?? params.sessionId}:compaction:${diagId}` ``。
  runId 若由调用方传入，可按 `:compaction:` 前缀还原主轮；
  但各调用方（运行中溢出压缩、手动 compact）是否都传了 runId **没追**，
  回落成 sessionId 的话需要另走 sessionId → 轮次映射。
- **子代理**：独立 runId，父子关系要另证，本次没查。
- llm-observer 每个 attempt 只记一对 input/output，不是每次模型调用一条，
  **不能替代** `model_call_*` hook 做逐请求映射。

## 建议的正式实现

1. 增加一个独立 OpenClaw 扩展，通过 `model_call_started` 与 `model_call_ended`
   仅保存白名单元数据：runId、callId、sessionKey、traceId、spanId、阶段。
   不改安装目录，不改 llm-observer，不记录提示词/回答/鉴权信息。
   结束事件可补开始事件，但事件缺失时仍应报告覆盖不足。
2. 代理保存并校验收到的 traceparent。runner 从本地映射中只接受已注册轮次，
   通过受鉴权控制接口提交 `(traceId, spanId) → runId/callId` 绑定。
   不接受来自外部模型请求头的任意 runId 自我声明为新轮次。
3. 映射前后到达都支持；原始请求 ID 不变，补关联追加记录并保留来源。
   一次模型调用的 SDK 重试可能共用 span，允许多个 HTTP 请求映射到同一 callId，
   请求次数仍按代理实际观察计数，不与 hook 次数混同。
4. 已结束轮次的迟到请求仍归原轮；来源冲突时留作冲突/未归属，不按优先级默默覆盖。
   子代理如果具有独立 runId，必须另证父子关系，不能仅因为 trace ID 相同就并入主轮。
5. 保存能力探活结果，区分“没有请求记录”与“有记录但不能归属”；保留旧版 YonWork
   和 WorkBuddy 已验证的原生头方式。对成功、工具续答、重试、取消后迟到和外部请求做验收。

历史账本目前只保存请求头名称，**没有保存 traceparent 值**。
不能承诺仅凭新增适配就能补回此前所有 `unattributed` 记录。

## 正式实现与实机验证（2026-09-23）

按上面 1–4 实现，**已装进本机 YonWork 1.0.10**：

| 部件 | 位置 | 要点 |
|---|---|---|
| 插件 | `plugins/benchmark-trace-bridge/`（6 项 node 单测） | 订阅 `model_call_started/ended`，handler 立即返回；白名单字段 POST 到采集代理，本地 `logs/bindings-*.jsonl` 每事件一行带回执 |
| 安装 | `scripts/install_trace_bridge.py status/install/uninstall/verify` | 文件放 `C:\Users\<用户>\.benchmark\benchmark-trace-bridge`；改 `openclaw.json` 三处并备份；重启 YonWork 生效；升级后 `verify [--batch]` 重验 |
| 代理 | `CollectorProxy.bind_trace` + `POST /_control/trace-bindings` | 先绑后请求、先请求后绑都接；后到的补归属并追加 closed 行；只认已注册轮次；`<id>:compaction:<n>` 还原主轮；冲突记 `conflict` |
| 入库 | `ingest._model_request_rows` | 账本读得到时以账本为准，迟到绑定也能进库；只认本批次的 BenchmarkId |

插件不需要 `allowConversationAccess`：网关只对 `llm_input/llm_output` 等 7 个 hook
要求这个开关（bundle 里的 `LTm` 列表），`model_call_*` 不在其中。
不 import `openclaw/plugin-sdk`：`definePluginEntry` 只是返回普通对象，
省掉就不用在插件目录里造 node_modules 链接。
网关启动日志确认加载：`plugins.gateway-load.plugin.benchmark-trace-bridge loadFailedCount=0 … registerFailedCount=0`。

**实机结果**（`统一代理` 模式，经采集入口；证据 `results/bridge-{smoke,tools}-20260923/`，不进 Git）：

| 批次 | 轮次 | 模型请求 | 归属 | hook 事件 | runId == BenchmarkId |
|---|---:|---:|---:|---:|---|
| smoke | 1 | 1 | 1/1 | 2，回执全 `bound` | ✅ |
| tools | 2（各调 2 次工具，Pass） | 8 | **8/8** | 16，回执全 `bound` | ✅ |

对照：四模式对比 v4 同一模式 13 请求 **0** 归属。hook 的 `(traceId, spanId)` 集合与账本
traceparent 集合逐一相等（8 = 8），没有多余也没有缺失。
同期 YonWork 自己起的一个会话（`session-…`）的 hook 事件回执 `unregistered`，没被归进任何轮。

**实测撞出来的三件事：**

- ⚠️ **callId 在一轮里不唯一。** tools 每轮 4 次请求，callId 却只有 `:model:1`、`:model:2` 各两次：
  产品在一次断流后开了新 attempt，`nextCallId` 的计数器随 attempt 重置。
  **关联键必须是 `(traceId, spanId)`**（4 个各不相同），按 callId 去重会少算一半请求。
- ⚠️ **同一次调用，代理和产品说法不一。** 两轮的第 2 次请求都在 ~167s 断流
  （代理记 `stream-truncated` / `IncompleteRead`，与 `docs/newapi-stall.md` 那条残余问题同形），
  而 hook 的 `model_call_ended.outcome` 报 `completed`。两边照实各记各的，没有互相覆盖。
  这让每轮拖到 ~178s，但两轮仍判 Pass——**耗时数据在修掉那条之前不能用**。
- ⚠️ **重启后 CDP 从 9222 挪到了 9223**（`userData/DevToolsActivePort` 里写着实际端口，仍只绑 loopback）。
  与插件无关（CDP 在 Electron 主进程，插件在网关进程），应是启动时 9222 还被占着。
  三节说的「端口固定」不绝对，用 CDP 前先读 `DevToolsActivePort`。

**仍未覆盖：** 子代理、compaction 的真实触发、取消后迟到、受控重试；
加载插件的开销 A/B（需要两次重启 YonWork，没做）；
报告层还没有「本批数据加载了扩展」的显式标记——眼下能看出来的只有
`model_requests.attribution_source` 以 `traceparent+` 开头。

## 副作用、清理与复现

开始前确认没有 Running/Queued Job，暂时停止空闲宿主机 Worker；探针持有全局串行锁。
本次创建的临时模型账户已删除，账户列表与探针前一致，本地监听器已关闭。
原运行时主模型 `custom-3859c0c4/deepseek-flash` 保持不变，宿主机 Worker 已恢复。
两条隔离测试会话保留为探针证据。没有修改产品安装文件或登录态，没有加载正式插件。

在没有其他 Worker/CLI 持锁时，从仓库根运行：

```bash
.venv/bin/python -m scripts.probe_yonwork_correlation
```

组件探针（使用产品自带 Node；Python 传参，避免 shell 转义修改 JavaScript）：

```bash
.venv/bin/python - <<'PY'
import subprocess
from pathlib import Path
subprocess.run([
    '/mnt/d/yonwork/resources/bin/node.exe', '--input-type=module', '-e',
    Path('scripts/probe_yonwork_trace_hooks.mjs').read_text(),
    r'D:\yonwork\resources\openclaw\dist',
], check=True)
PY
```

脚本依赖当前安装包的模块导出；未来升级找不到导出时会明确失败，不静默换到不同实现。
