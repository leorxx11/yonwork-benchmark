# runner —— YonWork Host API 基准测试驱动

纯 Python + HTTP，不碰界面。默认从 `cases/catalog.yaml` 读取用例；Excel 只作为兼容输入和
结果导出格式保留。

## 跑起来

```bash
python3 -m venv .venv
.venv/bin/pip install -r runner/requirements.txt

# 默认读取 cases/catalog.yaml 的 smoke 用例集
.venv/bin/python -m runner --case-set smoke --dry-run
.venv/bin/python -m runner --case-set smoke --out-dir results

# 指定 catalog，或兼容旧 Excel
.venv/bin/python -m runner --cases cases/catalog.yaml --case-set long-text --dry-run
.venv/bin/python -m runner --workbook cases/yonwork_benchmark.xlsx --sheet Cases --dry-run
```

前端提交的任务由 `python -m runner.worker` 串行执行。通常直接使用根目录的
`./scripts/bootstrap.sh`，不需要手工启动 worker。

产物落在 `<out-dir>/<batch-id>/`：

| 文件 | 内容 |
|---|---|
| `results.jsonl` | 一轮一行，跑完一轮立即追加，是事实来源 |
| `results.db` | 整批结束后由 JSONL 汇总，可重建 |
| `results.xlsx` | Results / Checks / Summary 三张表 |
| `transcripts/<BenchmarkId>.sse.jsonl` | 每轮 SSE 原始流（`--no-transcript` 可关闭） |

CLI 退出码：`0` 全过、`1` 有 Fail、`2` 有 Timeout、`3` 有 Error、`4` 有 Invalid、
`64` 参数或前置条件问题。多种结论并存时取最严重的一种。

## YAML 用例结构

```yaml
version: 1
case_sets:
  smoke:
    description: 最小冒烟
    cases:
      - name: hello
        prompt: 请只回复：你好
        runs: 1
        enabled: true
        assertions:
          expect: [你好]
          forbid: [无法完成]
          min_length: 2
          max_seconds: 60
```

支持的断言字段为 `expect`、`forbid`、`min_length`、`json_parsable`、`max_seconds`、
`max_total_tokens` 和 `max_input_tokens`。catalog 会拒绝未知字段、重复用例名、非法类型和
不存在的环境变量，避免拼写错误被静默忽略。
机器相关路径写成 `${YONWORK_XIYOUJI_PATH}`，在 `.env` 中提供值。

内容层只做弱断言；模型输出不确定，过强的逐字断言会制造噪声。

**token 阈值按 Case 定，没有全局底噪。** 曾经有个「20832 × 2」的全局天花板，
实测证伪了：同一句「你好！」inputTokens 在 5,514 ～ 16,238 之间，
长文本用例同一轮 session-jsonl 记 30,082、NewAPI 记 228,354。
不设 `max_input_tokens` / `max_total_tokens` 时，对应断言**只记录实测值和来源，不判定**——
这些记录就是将来定分位数基线的原料。别为了让断言"有输出"而填一个拍脑袋的数。

## 模块

| 文件 | 职责 |
|---|---|
| `case_catalog.py` | 严格读取 YAML catalog、选择用例集、展开环境变量 |
| `cases.py` | 旧 Excel 输入兼容层 |
| `discovery.py` | 从 `host-api-runtime.json` 读 port/token，探测健康和登录态 |
| `transport.py` | HTTP 收发；空 ProxyHandler 绕开环境代理 |
| `client.py` | `POST /api/chat/send` 的 SSE 客户端和终止判定 |
| `batch.py` | 每轮隔离、逐轮落盘、进度回调和协作式停止 |
| `job_store.py` | MySQL 持久任务队列、事件和进度 |
| `worker.py` | 领取任务、跑批、生成报告、入库与对账 |
| `usage.py` | 端上用量取样（时间窗匹配） |
| `sessionlog.py` | 会话 JSONL 用量（按 `idempotencyKey` 精确匹配） |
| `newapi.py` | NewAPI 后台日志取样 |
| `ingest.py` | JSONL → MySQL，幂等，`--rebuild` 可重放 |
| `reconcile.py` | 事后补采会话 JSONL 与 NewAPI 后台用量 |
| `assertions/` | 五层断言：完成性 → 产物 → 日志 → 内容 → 成本与性能 |
| `report.py` | JSONL → SQLite → XLSX |

驱动层只发请求、收原材料、记时间窗；判定全部留在 `assertions/`，以便单测和 diff。

## 三个容易踩的点

1. `stream == "compaction"` 和 `stopReason == "tooluse"` 不是终止，不能提前截断。
2. 每轮必须使用全新 `sessionKey`；复用会让后续轮次继承上下文，静默污染数据。
3. `idempotencyKey` 事实必填，服务端 `runId` 直接取它；使用 BenchmarkId 可稳定关联产物。

## token 的三个来源

| 来源 | 匹配方式 | 覆盖范围 |
|---|---|---|
| `session-jsonl` | `idempotencyKey` 精确匹配 | 全部模式，当前最可靠 |
| `newapi` | 时间窗 | 仅走 NewAPI 的轮次 |
| `device-api` | 时间窗 + 答案文本 | 全部模式，但实测存在漏记 |

一个来源一条 `UsageSample`，缺失就是未采集，不用 0 冒充。断言按既定来源顺序选择数据。

## 测试

```bash
.venv/bin/python -m unittest discover -s runner/tests -t .
```

全部离线，不需要 YonWork 或数据库运行。
