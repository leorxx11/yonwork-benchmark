# web —— 测试控制台与报告

FastAPI + Jinja + 少量本地原生 JavaScript，没有 CDN、Node 或前端构建链。它既能创建和观察
测试任务，也保留原来的结果对比页面；实际跑批由独立 worker 执行，Web 重启不会中断任务。

推荐从根目录运行 `./scripts/bootstrap.sh`。本地开发可执行：

```bash
.venv/bin/python -m uvicorn web.api:app --host 127.0.0.1 --port 8000 --reload
.venv/bin/python -m runner.worker
```

## 测试流程

1. 在 `/jobs/new` 选择 `cases/catalog.yaml` 中的用例集、模型和实验名称。
2. Web 将任务写入 MySQL 队列，独立 worker 串行领取。
3. 任务页轮询状态，显示总轮数、已完成轮数、事件和最新结果。
4. 完成后 worker 自动生成报告文件、写入 MySQL，并关联实验报告。

任务可在等待或运行时停止。运行中的请求不会被强杀；当前轮结束后不再开启下一轮，以免留下
半截 SSE 或不可解释的结果。

## 页面与接口

| 路由 | 作用 |
|---|---|
| `/jobs/new` | 检查 YonWork 状态、选择用例集与模型、提交测试 |
| `/jobs` | 最近任务列表 |
| `/jobs/{id}` | 任务进度、事件、结果链接和停止操作 |
| `/jobs/{id}/status` | 供本地 JS 轮询的局部 HTML |
| `/healthz` | 容器健康检查 |
| `/` | 全部对比实验 |
| `/suite/{id}` | 模式判定分布、耗时和 token |
| `/matrix/{id}` | Case × 模式矩阵 |
| `/reconcile/{id}` | 端上、会话 JSONL、NewAPI 后台的用量对账 |
| `/run/{benchmark_id}` | 五层断言、三来源用量、SSE 原始流 |

## 两条硬规矩

1. Web 不计算判定。`verdict` 由 `runner/assertions` 在跑批阶段生成；页面和 SQL 只查询、聚合、
   渲染，避免同一轮在不同页面得到不同结论。
2. 判定颜色不能单独传意。每个状态色块必须同时显示文字标签，保证低对比度场景仍可理解。

## 已知限制

- 一个任务目前只跑一个模型模式。用相同实验名称提交多个任务可在报告中对比；跨模式的一键编排
  仍是后续能力。
- NewAPI 后台日志仍按时间窗匹配，串行 worker 可避免互相污染；并发前必须先实现 runId 透传。
- WorkBuddy 尚无可编程入口，仍使用 `benchmark-companion/` 的人工流程，不在此控制台自动运行。

## 测试

```bash
.venv/bin/python -m unittest discover -s web/tests -t .
```
