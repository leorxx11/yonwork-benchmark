# YonWork 基准测试工具链

在浏览器里选择用例集和模型，一键提交测试；后台 worker 调用 Windows 上已经安装并启动的
YonWork，逐轮保存结果、采集用量、执行五层断言，最后在同一个前端查看报告。

背景、环境坑、架构决策和待办见 [CLAUDE.md](CLAUDE.md)。本轮基础设施与前端集成的完整改动
记录见 [docs/implementation-summary.md](docs/implementation-summary.md)。
逐轮 ErrorCalls 采集的范围和验收结果见 [docs/error-calls-collection.md](docs/error-calls-collection.md)。
**现在能说什么、不能说什么，见 [docs/conclusions-and-risks.md](docs/conclusions-and-risks.md)。**

## 换机后一键启动

适用环境：Windows + WSL2 mirrored 网络、WSL 内原生 Docker、Windows 侧已安装 YonWork。
先启动并登录一次 YonWork，然后在 WSL 中执行：

```bash
./scripts/bootstrap.sh
```

脚本会自动完成以下工作：

- 探测 `%APPDATA%/yonwork` 并挂载运行时文件和会话日志；
- 将长文本 fixture 复制到 Windows Documents；
- 生成本机专用且不入库的 `.env`；已有旧版 `infra/.env` / NewAPI token 会自动继承；
- 构建并启动 MySQL、NewAPI、Web 和串行 worker；
- 初始化结果表、任务队列表，并等待服务健康。

打开 <http://127.0.0.1:8000/jobs/new>，选择用例集和模式（产品 × 模型，可加多行）即可开测。YonWork 必须保持运行并已登录；
它是 Windows 应用，不放进容器。当前容器通过 host 网络访问 Windows loopback，这依赖 WSL
mirrored 网络和原生 Docker；普通 WSL NAT 或 Docker Desktop 需要另行配置宿主机地址。

NewAPI 第一次启动仍需在 <http://127.0.0.1:3000> 完成管理员初始化。若要测试 NewAPI 模型，
再把后台 token 和模型映射写进 `.env`；默认 YonWork 模型不需要这一步。

常用命令：

```bash
docker compose ps
docker compose logs -f web worker
docker compose restart web worker
docker compose down                  # 保留 infra/data 与 newapi/data
docker compose up -d --build
```

## 用例不再依赖 Excel

`cases/catalog.yaml` 是唯一主用例源，适合 Git diff、代码审查和合并冲突处理。它描述命名用例集、
执行次数、开关和断言；机器相关的 Windows 路径使用 `${环境变量}` 占位，由 `.env` 注入。

数据库只保存任务状态和测试结果，不保存用例定义：这样历史任务会记录所用 catalog/case-set，
而当前用例仍由 Git 版本管理。`yonwork_benchmark.xlsx` 仅保留作历史文件和 CLI 兼容入口，
前端及默认 CLI 都不再读取它。

```bash
# 默认 YAML 用例集，前置检查 + 展开，不发请求
.venv/bin/python -m runner --case-set smoke --dry-run

# Excel 兼容模式
.venv/bin/python -m runner --workbook cases/yonwork_benchmark.xlsx --sheet Cases --dry-run
```

## 前端工作流

1. `/jobs/new` 选择 YAML 用例集、实验名称，以及**一到多个模式**（产品 × 模型）并提交。
2. worker 从持久化队列取任务；刷新或重启 Web 不会丢任务。
3. `/jobs/{id}` 实时显示进度、事件和本轮结果，可请求停止。
4. 完成后自动生成 JSONL、SQLite、XLSX，入库并跳转到报告。

停止是协作式的：正在进行的一轮会先收尾，再阻止下一轮开始。worker 意外中断后，残留的
`Running` 任务会标记为失败，避免永远显示运行中。

### 跨模式一键编排

一次提交多个模式时，每个模式各自成为一条任务（一个批次），共用一个 `plan_id` 和
**同一个实验名**。实验名决定 `suite_id`，所以这一组批次天然落进同一份报告：
`/matrix/{suite_id}` 就是 Case × 模式的对比表，不需要另外拼装。

- `/plans/{plan_id}` 看整组进度，可一次停掉全部剩余模式。
- 模式之间互相隔离：一个模式失败只影响它自己，报告里表现为缺那一列。
- worker 仍是串行的，按提交顺序逐个模式执行——并发会让 NewAPI 用量按时间窗张冠李戴。
- 同一个（产品 × 模型）组合选两次会在提交时被拒绝：报告页按模式合并批次，
  重复提交会让那一列的轮次凭空翻倍且看不出来。

CLI（`python -m runner`）仍是单模式，一次跑一个批次；跨模式编排只在 Web 控制台。

### 跑 WorkBuddy 要把 Worker 换到宿主机

WorkBuddy 每轮起一个 Windows 进程（`WorkBuddy.exe`），而 compose 里的 worker
既没有 WSL interop 也看不到 `/mnt/d`，**容器 Worker 永远跑不了 WorkBuddy**。
要做 YonWork × WorkBuddy 的对比，两个产品得由同一个 Worker 串行跑完：

```bash
docker compose stop worker      # 容器 Worker 持锁，不停它宿主机这个起不来
docker compose up -d web        # ⚠️ 别用裸 up -d，worker 是 restart: unless-stopped
./scripts/host_worker.sh        # 前台跑，Ctrl-C 停止（当前这一轮会先收尾）
```

切回容器 Worker：Ctrl-C 结束宿主机进程，再 `docker compose start worker`。

始终只有一个 Worker 在跑：MySQL 咨询锁会拦住后启动的那个。这是正确性前提
不是运维约定——并发跑批会让 NewAPI 后台用量按时间窗张冠李戴。

`BENCH_WORKBUDDY_HOME` / `BENCH_WORKBUDDY_CONFIG_DIR` 可留空，默认去
`/mnt/d/WorkBuddy` 和 `/mnt/c/Users/*/.workbuddy` 找；后者**必须唯一**，
有多个候选时驱动报错而不是挑一个。

### 工具调用用例

`cases/catalog.yaml` 的 `tools` 用例集用 `min_tool_calls` 断言「模型真的调了工具」，
而不是「答案看起来像调过工具」。不声明就只记录不判定。

⚠️ **跑 WorkBuddy 的工具用例必须勾「允许调用工具」**，否则驱动传 `--tools ''`，
这一组会判 Invalid（跑法不对，不是产品的错）。勾上之后驱动会用
`--permission-mode bypassPermissions`——**那一批跑批期间模型可以在本机执行任意命令**，
前置检查里会打警告。YonWork 不受这个开关影响：它的工具由智能体配置决定。

### 测量口径

报告分别显示外层耗时、CLI 自报内部耗时、外层减内部的差值，均列出中位数、范围和有效样本数。
内部耗时可能包含工具和编排，差值不能全部归因于冷启动。未采集的字段留空，不补 0。

用量按来源分别汇总并显示覆盖率，不再把端上、会话、CLI 和后台的数字回落混加。
`default` 与显式选择默认模型仍保留为不同请求方式，不代表两个实际模型。
工具调用只有完整采集才能确认 0 次；要求调用工具的用例遇到日志缺失为 Invalid、读取错误为 Error。

直接 CLI 跑批与 Worker 使用同一把 MySQL 锁，Worker 空闲时也持锁；直接跑 CLI 前应先停止 Worker，
或通过 Web 队列提交。干运行和查询模型不需要这把锁。手工操作产品和外部同令牌流量仍需自行隔离。
完整边界和后续数据采集要求见 [测量正确性修复](docs/measurement-correctness.md)。

下一项：[YonWork / WorkBuddy 整轮模型调用监控待办](docs/model-call-monitoring-todo.md)，
涵盖逐请求采集、任务关联、失败重试与完整性验收，当前尚未实现。

## 目录

| 目录/文件 | 职责 |
|---|---|
| `runner/` | YonWork 驱动、YAML catalog、五层断言、任务 worker、结果入库 |
| `web/` | 测试控制台与报告（FastAPI + Jinja，本地 JS，无前端构建链） |
| `cases/catalog.yaml` | Git 管理的主用例源 |
| `infra/` | MySQL 8.4 表结构和持久化数据 |
| `newapi/` | 被测模型网关及其持久化数据 |
| `compose.yml` | 一键环境：MySQL、NewAPI、Web、worker |
| `results/` | 每批 JSONL/SQLite/XLSX/SSE 产物，不进版本库 |
| `docs/` | 调查报告、实现总结、结论与风险 |
| `archive/` | 已废弃的 PAD 流程、历史探测脚本、人工跑批 GUI，不维护 |

## 本地开发与测试

```bash
python3 -m venv .venv
.venv/bin/pip install -r runner/requirements.txt
.venv/bin/python -m unittest discover -s runner/tests -t .
.venv/bin/python -m unittest discover -s web/tests -t .
.venv/bin/python -m uvicorn web.api:app --host 127.0.0.1 --port 8000 --reload
```

测试全部离线：SSE、客户端和数据库均使用替身，不要求 YonWork 或 Docker 正在运行。
