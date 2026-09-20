# runner —— YonWork Host API 基准测试驱动

纯 Python + HTTP，不碰界面。只依赖 `openpyxl`（读提示词表和导 xlsx），其余全是标准库。

## 跑起来

```bash
# 一次性：建 venv（WSL 侧的 python3）
python3 -m venv .venv && .venv/bin/pip install -r runner/requirements.txt

# 前置检查 + 用例展开，不发请求
.venv/bin/python -m runner --workbook cases/yonwork_benchmark.xlsx --sheet Cases --dry-run

# 真跑
.venv/bin/python -m runner --workbook cases/yonwork_benchmark.xlsx --sheet Cases --out-dir results
```

产物落在 `<out-dir>/<batch-id>/`：

| 文件 | 内容 |
|---|---|
| `results.jsonl` | 一轮一行，**跑完一轮立刻追加** |
| `results.db` | 整批结束后由 JSONL 汇总而成，可重建 |
| `results.xlsx` | Results / Checks / Summary 三张表 |
| `transcripts/<BenchmarkId>.sse.jsonl` | 每轮的 SSE 原始流（`--no-transcript` 可关） |

退出码：`0` 全过、`1` 有 Fail、`2` 有 Timeout、`3` 有 Error、`4` 有 Invalid、`64` 参数或前置条件问题。
多种结论并存时取最严重的那个（Invalid > Error > Timeout > Fail）。

## 模块

| 文件 | 职责 |
|---|---|
| `discovery.py` | 从 `host-api-runtime.json` 读 port/token；健康探测、登录态探测 |
| `transport.py` | HTTP 收发；**空 ProxyHandler 绕开 `http_proxy`** |
| `client.py` | `POST /api/chat/send` 的 SSE 客户端，终止判定在这 |
| `cases.py` | 读提示词表（openpyxl） |
| `usage.py` | 端上 `/api/usage/recent-token-history` 取样（时间窗匹配） |
| `sessionlog.py` | 会话 JSONL 取样（按 `idempotencyKey` **精确**匹配，覆盖全部模式） |
| `newapi.py` | NewAPI 后台日志取样（时间窗匹配，只覆盖走 newapi 的轮次） |
| `ingest.py` | JSONL → MySQL，幂等，`--rebuild` 可全量重放 |
| `reconcile.py` | 事后补采会话 JSONL 与 NewAPI 后台用量 |
| `assertions/` | 五层断言：完成性 → 产物 → 日志 → 内容 → 成本与性能 |
| `batch.py` | 编排：每轮新 sessionKey、每轮 try/except 隔离、逐轮落盘 |
| `report.py` | JSONL → SQLite → xlsx |
| `__main__.py` | CLI |

**驱动层（discovery/transport/client）不下任何判定**，只发请求、收原材料、记时间窗；
判定全在 `assertions/`，可单测、可 diff。别把判断写回驱动层。

## 提示词表结构

前四列沿用 `cases/yonwork_benchmark.xlsx` 现有的 `CaseName | Prompt | Runs | Enabled`，
后面可选加断言参数列（没有就用默认值），多值用 `|` 分隔：

| 列 | 作用 |
|---|---|
| `Expect` | 期望关键词，缺一个就 Fail |
| `Forbid` | 禁止词，命中就 Fail |
| `MinLength` | 答案长度下限 |
| `JsonParsable` | 答案（或其中的 ```json 围栏）必须可解析 |
| `MaxSeconds` | 单轮耗时阈值 |
| `MaxTotalTokens` | 单轮 totalTokens 阈值 |

内容层**只做弱断言**：模型输出不确定，强断言会变成噪声。

## 三个容易踩的点（都已在代码里处理）

1. **终止判定**（`client.is_terminal_message`）：`stream=="compaction"` 和
   `stopReason=="tooluse"` **不是**终止，认错会把轮次提前截断。
2. **每轮全新 sessionKey**：`ChatClient` 会硬拦复用（`SessionKeyReuse`）。
   复用会让第 N 轮看见第 N-1 轮的上下文，数据静默作废且不报错。
3. **`idempotencyKey` 事实必填**，不传直接 500；服务端 `runId` 直接取它的值，
   所以 BenchmarkId 当 idempotencyKey 用，产物关联从源头解决，不需要按时间窗匹配产物。

## token 的三个来源

一轮的用量同时从三处采，一个来源一条 `UsageSample`，**谁缺就是谁漏记**，不互相顶替：

| 来源 | 怎么匹配 | 覆盖范围 | 实测情况 |
|---|---|---|---|
| `session-jsonl` | `idempotencyKey`，**精确** | 全部模式 | 最可靠，跑批时就能采到 |
| `newapi` | 时间窗 | 只有走 NewAPI 的轮次 | 与会话 JSONL 数字完全一致 |
| `device-api` | 时间窗 + 答案文本 | 全部模式 | **实测在漏记**，见下 |

断言按 `USAGE_SOURCES` 的顺序挑一条用（端上 → 会话 → 后台）。

## 已知缺口

- **端上 `/api/usage/recent-token-history` 在漏记。** 2026-09-20 实测：改过模型配置之后，
  连续 6 轮完成的对话一条都没进这个端点，而同样这些轮在会话 JSONL 和 NewAPI 后台都有记录。
  这是产品问题，不是采集问题；正因为如此，端上这一路不能当唯一数据源。
- **ErrorCalls 只有 NewAPI 后台有**（按 `type==4` 的日志数），而且要事后 `reconcile` 才采得到。
  会话 JSONL 和端上端点都没有错误计数，所以跑批当时那条断言仍然是「未采集」而不是 0——
  用 0 冒充等于把「没采到」说成「没出错」。
- **工具调用名是尽力而为**：`tool_use` 块的字段结构没有逐字段验证过，取不到名字时记 `unknown`。
- **老批次的 `model_mode`**：`requested_model_label` 是后加的字段，更早的 JSONL 里没有，
  重放时会回落成 modelId（显示成 `deepseek-flash` 而不是 `newapi`）。
  给那些文件单独 `--model-mode newapi` 重灌一次即可。

## 测试

```bash
.venv/bin/python -m unittest discover -s runner/tests -t .
```

全部离线：SSE 用假响应，跑批用假 client，不需要 YonWork 在跑。
