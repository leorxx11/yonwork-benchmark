# web —— 只读展示层

FastAPI + Jinja + HTMX，没有构建链、没有 node_modules。

```bash
.venv/bin/python -m uvicorn web.api:app --host 127.0.0.1 --port 8000 --reload
# → http://127.0.0.1:8000
```

## 完整链路

```bash
# 1. 跑批（一个模式一个批次）
.venv/bin/python -m runner --workbook cases/yonwork_benchmark.xlsx                    # yonwork / default
.venv/bin/python -m runner --workbook cases/yonwork_benchmark.xlsx --model newapi     # yonwork / newapi

# 2. 入库（JSONL 是事实来源，库随时可 --rebuild 重放）
.venv/bin/python -m runner.ingest --results results --suite "我的实验"

# 3. 事后补采用量（新跑的批次在跑批时就会采会话 JSONL，这步主要用于历史批次和后台数据）
.venv/bin/python -m runner.reconcile --suite <suite_id>
.venv/bin/python -m runner.reconcile --suite <suite_id> --skip-newapi   # 只补会话 JSONL
```

## 页面

| 路由 | 看什么 |
|---|---|
| `/` | 全部对比实验 |
| `/suite/{id}` | 四模式判定分布、耗时、token |
| `/matrix/{id}` | Case × 模式，一格一轮，点进详情 |
| `/reconcile/{id}` | 端上 vs 会话 JSONL vs NewAPI 后台，差异高亮 |
| `/run/{benchmark_id}` | 五层断言逐条 + 三来源用量 + SSE 原始流回放 |

## 两条硬规矩

1. **这里不许有任何判定逻辑。** `verdict` 是 `assertions/` 早就算好的，展示层只查询、
   聚合、渲染。SQL 里也不许偷偷判 Pass/Fail——判定要可单测、可 diff，混进 SQL 就都没了。
2. **判定颜色不能单独传意。** 用的是 status 调色板，light 模式下 warning/serious
   低于 3:1 对比度是设计如此，所以每个色块旁边必须有文字标签。

## 已知缺口

- 后台日志靠时间窗匹配：NewAPI 不知道 runId，YonWork 也没把它透给上游。
  串行跑批没问题，**并发跑批前必须先解决**。对不上的日志会在 `reconcile` 的输出里报出来，
  不会被悄悄丢掉。（会话 JSONL 那一路走 `idempotencyKey`，不受这条影响。）
- 总览和矩阵页的 token 列按「端上 → 会话 → 后台」回落，格子里标了实际来源；
  逐轮三列并排看对账页。
