# YonWork 基准测试工具链

给用友的 AI Agent 产品 **YonWork** 做基准测试：在浏览器里选用例集和模式（产品 × 模型），
一键提交；后台 Worker 调用 Windows 上已安装并登录的 YonWork（也可以是 WorkBuddy），
逐轮保存结果、采集用量、执行五层断言，最后在同一个前端看报告和横向对比。

**这份 README 是现状入口**：做到哪了、下一步做什么、怎么跑。

| 还想知道 | 去哪 |
|---|---|
| 开发规则、环境坑（**改代码前必读**） | [CLAUDE.md](CLAUDE.md) |
| 对外能说什么、不能说什么 | [docs/conclusions-and-risks.md](docs/conclusions-and-risks.md) |
| 全部文档索引（当前参考 / 结果 / 历史） | [docs/README.md](docs/README.md) |

---

## 当前状态（2026-09-23）

| 能力 | 状态 |
|---|---|
| Host API 驱动 + YAML 用例 + 五层断言 + 失败四分类 | ✅ 在用 |
| Web 控制台：持久任务队列、跨模式一键编排、报告与 Case × 模式对比 | ✅ 在用 |
| YonWork × WorkBuddy 同一套断言（WorkBuddy 需宿主机 Worker） | ✅ 在用 |
| 用量多来源独立汇总、逐轮 ErrorCalls、工具调用断言 | ✅ 在用，边界见 [测量正确性](docs/measurement-correctness.md) |
| 逐请求模型调用账本（采集代理 → NewAPI，精确关联到轮次） | ⚠️ 已接通、入库、上报告；**整轮监控未验收**，见待办 2 |
| 四模式横向对比（48 轮） | ✅ 首份成规模结果：[四模式对比](docs/four-way-comparison-20260922.md) |
| 客户端体验探针（CDP 渲染测量） | ⏹ 已结项，不再投入；结论见 [探针结果](docs/history/client-probe-results.md) |

**本机环境现状——跑批或解读数据前要知道：**

- ⚠️ **YonWork 装了我们的 hook 扩展**（`plugins/benchmark-trace-bridge/`），被测对象不是出厂状态。
  它补回了 1.0.10 丢掉的逐请求归属（实测 9/9），代价是每次模型调用都跑我们的代码，开销未量。
  **YonWork 更新或重启后**先跑 `.venv/bin/python -m scripts.install_trace_bridge verify`。
  卸载：`.venv/bin/python -m scripts.install_trace_bridge uninstall`，再用 uTools 重启 YonWork。
- ⚠️ **「统一代理」模式（经采集代理 → NewAPI）的耗时数据现在不能用**：工具用例每轮都有一次
  请求在 ~167s 断流后重试，把轮次拖到 ~178s。见待办 1。
- `.env` 里逐请求采集是**开着的**（`BENCH_COLLECTOR_ENABLED=1`），常驻服务 `collector` 要在跑。
- 本机已关掉产品默认的局域网暴露（[产品缺陷 #1](docs/product-defects.md)）；
  **重启 YonWork 只能用 uTools / 开始菜单**，从 WSL 拉起会静默退回对外开放（CLAUDE.md 坑 4）。
- CDP 端口当前是 9223（不是 9222），以 `userData/DevToolsActivePort` 为准。

## 待办

**这里是唯一的待办清单**，按优先级排。做完一件把它改写成「现状 + 剩余」，不直接删。

1. **NewAPI 残余断流（~167s）。** 统一代理链路上流式请求仍会在 ~167s 断开
   （代理记 `stream-truncated`，而 YonWork 的 hook 报 `completed`）。
   09-22 的 HTTP/1.1 + 禁复用只缓解了成簇挂住，没根治，也还没抓包定位是哪一跳。
   → [NewAPI 超时排查](docs/newapi-stall.md)
2. **整轮模型调用监控验收的剩余部分。** 单次问答、超时/断流、连续轮次已过；
   工具续答只证明了归属，**请求 ↔ 工具执行的对应关系**还没有；
   子代理、取消、compaction 真实触发、受控重试没跑；
   hook 扩展的开销 A/B 没量，报告里也还没有「本批加载了扩展」的显式标记。
   → [监控待办与验收矩阵](docs/model-call-monitoring-todo.md)
3. **上报五条产品缺陷**（走公司内部渠道，先确认是否为测试构建有意放宽）。#5 数据最完整，先报。
   → [产品缺陷清单](docs/product-defects.md)
4. **把 `tokenAmplification` 搬进 runner，再删 `yonwork_usage/`。**
   它读 llm-observer 日志，给出「一轮内部重放了多少上下文」（实测 7.78×），`sessionlog` 给不出来。
5. **WorkBuddy 工作目录定死。** 它的当前目录是 CMD 不支持的 UNC 路径；
   比较「当前目录」类用例前两个产品问的必须是同一个目录。

**明确往后放**（写下来是为了不反复捡起）：
- 并发跑批——锁拦不住外部流量，精确关联没覆盖全部来源前放开就是张冠李戴（CLAUDE.md 四-2）。
- 客户端体验探针——已结项；不做打字 / 新建任务类 UI 自动化。
- 清理 9 条分裂会话——留着当产品缺陷 #5 的上报证据。

---

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
- 构建并启动 MySQL、NewAPI、Web、串行 worker 和采集入口 collector；
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

## 用例

`cases/catalog.yaml` 是唯一主用例源，适合 Git diff、代码审查和合并冲突处理。它描述命名用例集、
执行次数、开关和断言；机器相关的 Windows 路径使用 `${环境变量}` 占位，由 `.env` 注入。

数据库只保存任务状态和测试结果，不保存用例定义：历史任务会记录所用 catalog/case-set，
而当前用例仍由 Git 版本管理。`cases/yonwork_benchmark.xlsx` 仅保留作历史文件和 CLI 兼容入口。

```bash
# 默认 YAML 用例集，前置检查 + 展开，不发请求
.venv/bin/python -m runner --case-set smoke --dry-run

# 单批次实跑（先停 Worker：CLI 与 Worker 抢同一把 MySQL 锁）
.venv/bin/python -m runner --case-set smoke --model 统一代理

# 列出可选模型
.venv/bin/python -m runner --list-models
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
`/matrix/{suite_id}` 就是 Case × 模式的对比表。

- `/plans/{plan_id}` 看整组进度，可一次停掉全部剩余模式。
- 模式之间互相隔离：一个模式失败只影响它自己，报告里表现为缺那一列。
- worker 串行，按提交顺序逐个模式执行。
- 同一个（产品 × 模型）组合选两次会在提交时被拒绝（否则那一列轮次凭空翻倍且看不出来）。

CLI 仍是单模式，一次跑一个批次；跨模式编排只在 Web 控制台。

### 跑 WorkBuddy 要把 Worker 换到宿主机

WorkBuddy 每轮起一个 Windows 进程，而容器 Worker 既没有 WSL interop 也看不到 `/mnt/d`，
**容器 Worker 永远跑不了 WorkBuddy**。要做两个产品的对比，得由同一个宿主机 Worker 串行跑完：

```bash
docker compose stop worker      # 容器 Worker 持锁，不停它宿主机这个起不来
docker compose up -d web        # ⚠️ 别用裸 up -d，worker 是 restart: unless-stopped
./scripts/host_worker.sh        # 前台跑，Ctrl-C 停止（当前这一轮会先收尾）
```

切回容器 Worker：Ctrl-C 结束宿主机进程，再 `docker compose start worker`。

`BENCH_WORKBUDDY_HOME` / `BENCH_WORKBUDDY_CONFIG_DIR` 可留空，默认去
`/mnt/d/WorkBuddy` 和 `/mnt/c/Users/*/.workbuddy` 找；后者**必须唯一**。

### 工具调用用例

`tools` 用例集用 `min_tool_calls` 断言「模型真的调了工具」。不声明就只记录不判定。

⚠️ **跑 WorkBuddy 的工具用例必须勾「允许调用工具」**，否则这一组判 Invalid（跑法不对，不是产品的错）。
勾上之后驱动用 `--permission-mode bypassPermissions`——**那一批跑批期间模型可以在本机执行任意命令**。
YonWork 不受这个开关影响：它的工具由智能体配置决定。

### 测量口径

报告分别显示外层耗时、产品自报内部耗时和两者差值，均列中位数、范围和有效样本数；
用量按来源分别汇总并显示覆盖率，不混加；未采集的字段留空，不补 0。
完整规矩见 CLAUDE.md 第四节，实现与验收见 [测量正确性](docs/measurement-correctness.md)。

### 逐请求模型调用账本

[采集代理](docs/model-request-ledger.md)站在产品和 NewAPI 之间，逐次记录模型请求并精确归属到轮次，
`/run/<id>` 看逐请求时间线，`/suite/<id>` 看覆盖汇总。

- 开关：`.env` 的 `BENCH_COLLECTOR_*`；常驻服务 `docker compose up -d collector`。
- 被测产品里那个模型的 baseUrl 要**手动**指向 `http://127.0.0.1:3312/v1`（驱动不改产品配置）。
- 归属靠产品原生请求头（WorkBuddy、YonWork 1.0.8）或 hook 扩展（YonWork 1.0.10 起）：
  `scripts/install_trace_bridge.py status|install|uninstall|verify`，改完用 uTools 重启 YonWork；
  跑完一批可用 `verify --batch results/<批次>` 核对归属是否端到端成立。
- 离线自检（不需要任何产品）：`.venv/bin/python -m runner.modelproxy selfcheck`
- 跑批变慢先看 [NewAPI 超时排查](docs/newapi-stall.md)。

## 目录

| 目录/文件 | 职责 |
|---|---|
| `runner/` | 驱动层（`drivers/`）、YAML catalog、五层断言、任务 worker、结果入库 |
| `runner/modelproxy/` | 逐请求模型调用账本与采集代理 |
| `runner/client_probe/` | 客户端体验探针（已结项，保留代码） |
| `plugins/benchmark-trace-bridge/` | 装进 YonWork 的 OpenClaw hook 扩展，补 1.0.10 的逐请求归属 |
| `web/` | 测试控制台与报告（FastAPI + Jinja，本地 JS，无前端构建链） |
| `cases/catalog.yaml` | Git 管理的主用例源 |
| `infra/` | MySQL 8.4 表结构和持久化数据 |
| `newapi/` | **被测对象**的模型网关（:3000）及其数据，不是我们的基础设施 |
| `scripts/` | 一次性 / 辅助工具，各自的用途见 [scripts/README.md](scripts/README.md) |
| `yonwork_usage/` | 解析 llm-observer 日志；取 token 已被 `sessionlog` 取代，待办 4 搬完再删 |
| `compose.yml` | 一键环境：MySQL、NewAPI、Web、worker、collector |
| `results/` | 每批 JSONL/SQLite/XLSX/SSE 产物，不进版本库 |
| `docs/` | 专题文档；`docs/history/` 是已完结的调查和开发日志，见 [索引](docs/README.md) |
| `archive/` | 已废弃的 PAD 流程、历史探测脚本、WorkBuddy 人工跑批 GUI，**不维护** |

两个看起来冗余但**刻意保留**的东西：`scripts/newapi_stats.ps1` 和 `archive/benchmark-companion/`。
它们不依赖我们自己的任何代码，runner 的数字可疑时，用它们判断到底是产品坏了还是工具坏了。

## 本地开发与测试

```bash
python3 -m venv .venv
.venv/bin/pip install -r runner/requirements.txt
.venv/bin/python -m unittest discover -s runner/tests -t .
.venv/bin/python -m unittest discover -s web/tests -t .
node --test plugins/benchmark-trace-bridge/
.venv/bin/python -m uvicorn web.api:app --host 127.0.0.1 --port 8000 --reload
```

测试全部离线：SSE、客户端、采集代理上游和数据库均使用替身，不要求 YonWork 或 Docker 正在运行。
