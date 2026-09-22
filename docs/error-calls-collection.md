# 逐轮 ErrorCalls 采集

实现日期：2026-09-21。

⚠️ **2026-09-22 新增盲区：被网关在选通道之前拒掉的请求，`/api/log/self` 里一条都没有**——
消费和错误记录都没有。实测一轮里 18 次 HTTP 503 `No available channel`，
逐请求账本全记下了，CLI 自己 stdout 空白退出码 0，而后台日志是 0 条。
**所以「后台错误日志为 0」不能推断「这一轮没有失败的请求」。**
范围：只实测了 503 这一种拒绝、只查了本文用的 `/api/log/self`。
见 [逐请求账本](model-request-ledger.md)。

## 解决的问题

原流程在整批判定、JSONL 落盘和结果入库之后才对账 NewAPI。后台即使发现错误调用，
本轮日志检查依然是「未采集」，已经保存的 Pass 也不会改变。

现在 YonWork 显式选择显示名 `newapi` 的模型时，每轮结束先采后台日志，再调用现有
`assertions`。消费和错误日志都计入 APICalls，错误日志单独计入 ErrorCalls。
回答重试成功但 ErrorCalls > 0 时，最终判定为 Fail；仍由统一断言层决定优先级。

## 数据流与文件

`YonWorkDriver.collect_usage()` → `collect_turn_logs()` → `UsageSample` →
`run_one()` → `log_stats` → 五层断言 → JSONL → SQLite/XLSX/MySQL/Web。

| 文件 | 改动 |
|---|---|
| `runner/newapi.py` | 逐轮时间窗与模型匹配、短暂补采、完整分页校验、UTC 转换 |
| `runner/drivers/yonwork.py` | NewAPI 作为第三个采集来源；失败留痕且不泄漏后台错误正文 |
| `runner/models.py` | 用量样本增加 api_calls、error_calls、log_entries |
| `runner/batch.py`、`runner/usage.py` | 调用统计使用后台实采值，独立于 token 来源优先级 |
| `runner/ingest.py` | 计数只存入相应来源，避免复制到端上/会话来源 |
| `runner/reconcile.py` | 不覆盖已用于逐轮判定的 NewAPI 样本 |
| `.env.example`、`compose.yml` | 可选 NEWAPI_TOKEN_NAME 过滤 |

未改数据库结构。已有 nullable 调用计数列和 raw JSON 字段足够承载新样本，
历史 JSONL 仍可读取。事后对账保留补采功能，但不重算历史判定。

## 匹配与缺失语义

- 只覆盖显式选择显示名 `newapi` 的 YonWork 模型。默认模型、其它显示名、WorkBuddy
  不查询这一路；未来要支持其它网关配置名，需要扩展路由识别。
- 使用本轮 UTC 起止时间（NewAPI 精度为秒）和请求模型；不带旧对账的 ±15 秒余量。
  `NEWAPI_TOKEN_NAME` 可进一步限制令牌名。仍要求串行执行、同一令牌没有其它并发流量。
- 在已有端上取样等待后查询 3 次，间隔 1 秒，采用最后一次完整结果。
  分页期间总数变化或返回结构无效，标记采集失败，不把部分结果用于判定。
- `/api/log/self` 的 `id` 是重编后的结果序号。实机观察：后台 SQLite 最新记录 id=16，
  对应查询返回 id=1。不能把这个序号当作持久日志标识；不跨查询按 id 合并。
  保存 created_at、type、model_name、prompt_tokens、completion_tokens 摘要作为计数证据，
  不保存后台错误正文，也不去重同秒同内容的不同调用。
- 缺凭据、查询失败、空查询均不产生伪造的 0。ErrorCalls 为 `None`，检查为未采集。
  APICalls 可以继续使用端上请求发生的有限证据，但不能用空后台查询推断 APICalls=0。
- 日志延迟超过补采窗口、网关没有记录的错误仍无法覆盖。ErrorCalls=0 只表示本次
  匹配到的日志没有错误记录，不是对整个产品错误数的保证。
- 模型耗时仍使用 ChatTurn 的时间，不包括采集等待。

## 验证

```bash
.venv/bin/python -m unittest discover -s runner/tests -t .
.venv/bin/python -m unittest discover -s web/tests -t .
git diff --check
```

Runner 157 项、Web 10 项通过。新增回归覆盖延迟错误、重试成功仍判 Fail、空日志、
不同模型和相邻时间窗过滤、查询 id 重编号、同秒重复调用、分页变化、采集失败隔离、
敏感错误文字不入报告、JSONL/SQLite 判定一致、来源计数隔离和事后对账不覆盖。

本机 Docker 镜像已重新构建，Web/Worker 已更新。最终版本通过 Web 提交了
`smoke` / `newapi` / 1 轮任务：

- job：`603f986ce92c46568be4cd18ff220c19`，状态 Completed。
- batch：`web-20260921-121859-603f986c`。
- benchmark：`web-603f986c-Case02-r1-1a0c3e7c5ac`，判定 Pass。
- 结果：APICalls=1、ErrorCalls=0；后台 tokens=16308+74=16382。
- `results/<batch>/results.jsonl`、SQLite、XLSX、MySQL 数值一致。
  MySQL 的 NewAPI 行保留 `time-window+model`，证明后续自动对账未覆盖逐轮证据；
  会话来源的 ErrorCalls 保持 NULL，没有复制后台的 0。
- `/healthz`、任务页、实验报告页、单轮报告页均返回 HTTP 200。

真实冒烟只覆盖成功调用。错误调用判 Fail、迟到错误、空日志和请求失败分支由离线回归
验证，没有为了测试而修改模型路由或制造真实上游故障。

复现入口：Web 选择 YonWork / newapi / 基础冒烟，勾选采集用量后提交；或执行：

```bash
.venv/bin/python -m runner --case-set smoke --model newapi --limit 1
```

CLI 验证时不要同时运行 Web 队列任务，以免破坏时间窗匹配的串行前提。
