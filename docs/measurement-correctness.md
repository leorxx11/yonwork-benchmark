# 测量正确性修复与采样边界

日期：2026-09-21。

## 修复的四个问题

1. 会话日志缺失或读取失败时，工具列表为空；旧断言将它当成确认 0 次，误判产品 Fail。
2. 把 CLI 自报内部耗时称为纯模型耗时，把外层减内部全归因于冷启动，超出了测量证据。
3. 报告逐轮回落到不同 token 来源后求和，混合了不一致的统计口径。
4. 排他锁只覆盖 Worker，直接 CLI 跑批可以绕过它并污染时间窗。

## 工具观测与判定

`ChatTurn` 增加 `tool_calls_status`、`tool_calls_source`、`tool_calls_detail`，
随原材料落 JSONL。驱动只记录观测状态，由 `assertions/artifacts.py` 分类。

| 观测 | 声明 min_tool_calls 且尚未满足下限 | 没声明下限 |
|---|---|---|
| 完整采集 observed | 次数不足才判 Fail | 记录次数 |
| 日志缺失或尚未完整 unavailable | Invalid | 检查未执行 |
| 读取或解析错误 error | Error | 检查未执行，原因留痕 |
| 批次显式关闭工具 | Invalid | 按观测记录 |

已有正向证据足以满足下限时，可以判该项通过，不要求额外来源一定成功。
例如已看到 1 次调用且只要求至少 1 次，后续补采缺失不会否定已观察到的事实。

YonWork 从会话 JSONL 补采；只有包含终止消息且没有损坏行，才确认计数完整。
SSE 和会话来源不相加，避免重复计数。会话计数少于 SSE 时保留完整性未确认状态。
WorkBuddy 完整 CLI 结果中的工具调用作为该通路的观测来源。

缺失或错误不会中断后面的轮次。`enrich` 漏实现等 AttributeError 仍作为编程错误显式抛出。
空列表且没有完整采集标记时，SQLite/MySQL 调用次数写 NULL，页面显示缺失，不能再写 0。
旧记录的非空列表仍保留正向调用证据；历史判定不自动重算，也不修改历史 JSONL。

## 报告口径

- 原有 `engine_seconds` / `engine_ms` 字段名保留兼容，页面称为“CLI 自报内部耗时”。
- “外层 − 内部差值”按同一轮的两个读数计算；缺任一读数就不参与差值统计。
  不把负差值截为 0，也不强行解释成冷启动。
- 三种耗时分别显示中位数、最小值、最大值、有效 n。包含已记录耗时的所有判定；
  不表示它们都是有效的性能基线样本。缺测显示未采集。
- 每个用量来源分别显示 token 总量、覆盖轮次、实际模型，以及该来源已记录的美元费用与覆盖。
  缺失仍是缺失，真实的 0 保留；各来源不能相加。
- 单轮矩阵仍能展示优先来源的读数，但标明来源；模式总量不再用这种回落方式混加。
- `default` 与显式默认模型保持不同请求模式；分组本身不是重复计算同一轮。
  多 Case 或多个实际模型的汇总只描述这组混合观测，不能直接推广。

## 串行执行

`python -m runner` 实际跑批与 Worker 共用现有 MySQL 排他锁。
拿不到锁或数据库不可用时，CLI 在发起任何轮次前返回 64；`--dry-run` 与模型列表查询不持锁。
Worker 空闲时仍持锁，因此直接 CLI 前需要停止 Worker，或改用 Web 队列。
锁在 CLI 轮次及采集全部结束后释放，异常和中断也会收尾释放。

这只能约束本项目的两个执行入口，不能阻止手工点击产品、旧脚本或其它进程使用同一令牌。
正式采样仍需隔离外部流量。

## 主要文件

- `runner/models.py`、`sessionlog.py`、`drivers/`：观测状态与完整性证据。
- `runner/assertions/artifacts.py`、`batch.py`：分类、留痕和逐轮隔离。
- `runner/report.py`、`ingest.py`：缺失调用数落 NULL，无新增数据库列。
- `runner/__main__.py`、`job_store.py`：CLI 与 Worker 共用排他锁。
- `web/queries.py`、`templates/suite.html`：来源分组、覆盖率和描述统计。

## 验证

```bash
.venv/bin/python -m unittest discover -s runner/tests -t .
.venv/bin/python -m unittest discover -s web/tests -t .
.venv/bin/python -m compileall -q runner web
git diff --check
```

197 项 Runner、23 项 Web 测试通过。关键回归：日志缺失/读取错误/完整零次、
缺少终止消息、损坏行、部分正向证据、失败后继续下一轮、NULL 持久化、
多来源值不混加、覆盖率不重复计数、中位数与成对差值、CLI 锁冲突拒绝执行。

实机检查：新报告查询读取现有两产品实验，外层/内部/差值仍为 36377/33934/2443 ms，
页面明确标 n=1；WorkBuddy CLI 与 YonWork 会话来源分别展示，不解释为纯模型/冷启动。
现有 YonWork 工具会话能识别为完整观测。

宿主机 Worker 持锁时执行以下命令，实测退出码 64，没有产生结果目录或模型轮次：

```bash
.venv/bin/python -m runner --case-set smoke --limit 1 --no-xlsx --no-usage \
  --batch-id measurement-lock-check
```

Web 镜像已更新，宿主机 Worker 已在确认队列空闲后优雅重启；未启动容器 Worker。

最终 Web 冒烟任务 `9d4047cefe95413bb4bdd771f3740ed5`，批次
`web-20260921-154033-9d4047ce`：Completed / Pass。
普通问候轮的 `tool_calls_status=observed`、来源 `session-jsonl`、调用数 0；
JSONL、SQLite、MySQL 一致。这是“确认零次”的正向验收；缺失与读取错误分支由故障注入回归验证。

## 下一批数据的前提

本次没有扩大性能采样，也没有改写历史失败记录。正式采样前固定：

1. Case 的 prompt、断言、fixture 内容及版本；工具用例应验证产物或返回结果，不能只验证发生调用。
2. 产品与驱动版本、明确请求模型及实际模型；默认配置对比与同模型对比单独命名。
3. 工具权限、次数限制、工作目录；WorkBuddy 的 UNC 工作目录问题仍未解决，目录相关跨产品用例暂不比较。
4. 用量来源及 token/费用口径、网络和账号条件、外部并发流量。
5. 调试/验收/正式采样批次分开，预先写清要比较的指标、关心的差异和停止条件。

增加轮数不能消除以上混杂。先报告每种条件下的 n、范围、缺失比例和失败分类，
再决定数据能否支持稳定基线；不预先承诺某个固定样本数就足够。
