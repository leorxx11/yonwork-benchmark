# web —— 测试控制台与报告

FastAPI + Jinja + 少量本地原生 JavaScript，没有 CDN、Node 或前端构建链。它既能创建和观察
测试任务，也保留原来的结果对比页面；实际跑批由独立 worker 执行，Web 重启不会中断任务。

推荐从根目录运行 `./scripts/bootstrap.sh`。本地开发可执行：

```bash
.venv/bin/python -m uvicorn web.api:app --host 127.0.0.1 --port 8000 --reload
.venv/bin/python -m runner.worker
```

## 测试流程

1. 在 `/jobs/new` 选择 `cases/catalog.yaml` 中的用例集、**被测产品**、模型和实验名称。
2. Web 将任务写入 MySQL 队列，独立 worker 串行领取。
3. 任务页轮询状态，显示总轮数、已完成轮数、事件和最新结果。
4. 完成后 worker 自动生成报告文件、写入 MySQL，并关联实验报告。

任务可在等待或运行时停止。运行中的请求不会被强杀；当前轮结束后不再开启下一轮，以免留下
半截 SSE 或不可解释的结果。

## 页面与接口

| 路由 | 作用 |
|---|---|
| `/jobs/new` | 选用例集、产品、模型并提交；同时显示 YonWork 前置状态 |
| `/jobs` | 最近任务列表 |
| `/jobs/{id}` | 任务进度、事件、结果链接和停止操作 |
| `/jobs/{id}/status` | 供本地 JS 轮询的局部 HTML |
| `/healthz` | 容器健康检查 |
| `/` | 全部测试报告（一个实验名 = 一份报告） |
| `/suite/{id}` | 模式判定分布、耗时和 token |
| `/matrix/{id}` | Case × 模式矩阵 |
| `/reconcile/{id}` | 端上、会话 JSONL、NewAPI 后台的用量对账 |
| `/run/{benchmark_id}` | 五层断言、三来源用量、SSE 原始流 |

## 两条硬规矩

1. Web 不计算判定。`verdict` 由 `runner/assertions` 在跑批阶段生成；页面和 SQL 只查询、聚合、
   渲染，避免同一轮在不同页面得到不同结论。
2. 判定颜色不能单独传意。每个状态色块必须同时显示文字标签，保证低对比度场景仍可理解。

导航按流程顺序写死成「① 新建测试 → ② 测试任务 → ③ 测试报告」，三个词在整站统一。
以前同一个东西在导航叫「报告」、标题叫「对比实验」、面包屑叫「全部实验」，
光是对上号就要费一遍劲。改名字时三处一起改。

## 已知限制

- 一个任务只跑一个「产品 × 模型」。用相同实验名称提交多个任务即可在同一份报告里横向对比；
  一键编排多个模式仍是后续能力。
- NewAPI 后台日志仍按时间窗匹配，串行 worker 可避免互相污染；并发前必须先实现 runId 透传。
- **容器里的 Worker 跑不了 WorkBuddy。** 它要起一个 Windows 进程，而 compose 里的 worker
  既看不到 `/mnt/d` 也没有 WSL interop。从控制台选 WorkBuddy 需要 Worker 在**宿主机原生**起
  （就是上面那条 `python -m runner.worker`）；CLI 直接跑没有这个限制。
  驱动本身已实测跑通（见 `runner/drivers/`）。

## 测试

```bash
.venv/bin/python -m unittest discover -s web/tests -t .
```
