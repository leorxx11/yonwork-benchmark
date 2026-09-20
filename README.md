# YonWork 基准测试工具链

批量跑 prompt → 采耗时/token/成本 → 按五层断言自动判定 → 看板对比。
全程纯 Python + HTTP，不碰界面。

背景、环境坑、架构决策和待办在 **[CLAUDE.md](CLAUDE.md)**，那是跨机器的对齐文档，先读它。

## 一条命令看懂链路

```bash
# 0. 起依赖（各起一次就行）
cd infra  && docker compose up -d   # 结果库 MySQL:3307
cd newapi && docker compose up -d   # 被测的模型网关 :3000

# 1. 跑批 —— 一个模式一个批次
.venv/bin/python -m runner --workbook cases/yonwork_benchmark.xlsx                 # yonwork / 默认模型
.venv/bin/python -m runner --workbook cases/yonwork_benchmark.xlsx --model newapi  # yonwork / newapi

# 2. 入库 —— JSONL 是事实来源，库随时可 --rebuild 重放
.venv/bin/python -m runner.ingest --results results --suite "我的实验"

# 3. 补采历史批次的用量（新批次在跑批时就采了）
.venv/bin/python -m runner.reconcile --suite <suite_id>

# 4. 看板
.venv/bin/python -m uvicorn web.api:app --host 127.0.0.1 --port 8000 --reload
```

## 目录

| 目录 | 是什么 | 状态 |
|---|---|---|
| `runner/` | 驱动 + 五层断言 + 入库。**主链路** | 在用 |
| `web/` | 只读看板（FastAPI + Jinja + HTMX，无构建链） | 在用 |
| `infra/` | 结果库：MySQL 8.4 + 表结构 | 在用 |
| `newapi/` | **被测对象**的模型网关，不是我们的基础设施 | 在用 |
| `cases/` | 提示词清单 + 长文本 fixture | 在用 |
| `scripts/` | 装环境、拉后台用量、解包 asar | 在用 |
| `docs/` | `yonwork-automation-report.md`，**权威参考**（已脱敏） | 在用 |
| `results/` | 跑批产物，一个批次一个目录。不进版本库 | 产物 |
| `benchmark-companion/` | **WorkBuddy** 的人工跑批 GUI（PySide + SQLite + PyInstaller） | 保留，见下 |
| `yonwork_usage/` | 解析 `llm-observer` JSONL 取 token 的单文件 CLI | **待定，见下** |
| `archive/` | 废弃的 PAD 流程和探测脚本，只作历史记录 | 不维护 |

## benchmark-companion 为什么不能算「旧东西」

容易误会成 PAD 时代的遗留，其实不是：**它服务的是 WorkBuddy，不是 YonWork**。
复制提示词 → 人工粘到 WorkBuddy 发送 → `F8` 开始计时 / `F9` 结束 / `F10` 异常 → 落 SQLite 再同步 Excel。

也就是说，`runner/` 覆盖的是 YonWork 那两个模式，**WorkBuddy 那两个模式目前只有这条人工通路**。
「四个模式」里有一半的数据质量和另一半不在一个等级上——这正是待办里
「摸 WorkBuddy 有没有可编程入口」排在前面的原因（CLAUDE.md 七）。

已知问题：`config.json` 里的路径还是旧机器的（`C:\Users\Administrator\Desktop\benchmark\`），
**在这台机器上开箱即坏**。代码里的默认值已经改成仓库相对路径，但 `config.json` 会覆盖默认值，
要用先改它。

## yonwork_usage 的「待定」是什么意思

它读的是 `llm-observer/*.jsonl`，而 `runner/sessionlog.py` 读的是 `sessions/*.jsonl`，
**是两个不同的文件**。取 token 这件事已经被 sessionlog 覆盖了，
但 llm-observer 还独有一个 `tokenAmplification`（累计 token / 最后一轮 token，实测 7.78×），
那是「一轮对话内部究竟重放了多少上下文」的直接指标，sessionlog 给不出来。
真要删，先把这个指标搬进 runner。

## 测试

```bash
.venv/bin/python -m unittest discover -s runner/tests -t .
```

全部离线：SSE 用假响应，跑批用假 client，不需要 YonWork 在跑，也不需要数据库。
