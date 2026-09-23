# YonWork 基准测试工具链 · 开发规则

> **这份文件只放规则和会踩的坑，不放状态和过程。**
>
> | 想知道 | 去哪 |
> |---|---|
> | 现在做到哪、下一步做什么、怎么跑 | [README](README.md)「当前状态」「待办」 |
> | 所有文档的索引 | [docs/README.md](docs/README.md) |
> | 已查到的产品缺陷 | [docs/product-defects.md](docs/product-defects.md) |
> | 对外能说什么、不能说什么 | [docs/conclusions-and-risks.md](docs/conclusions-and-risks.md) |
> | 当初为什么这么做、踩坑过程 | [docs/history/](docs/history/)，开发日志保留原「五、七」节号 |
>
> 代码注释引用本文件写「CLAUDE.md 二-3」，节号只增不改。维护规矩见第六节。

## 这个项目是什么

给用友的 AI Agent 产品 **YonWork**（桌面端 + 云端）做基准测试：批量跑 prompt，
采集耗时/token/成本，自动判定通过与否；同一套断言也接了 **WorkBuddy** 做横向对比。

取舍标准：**「面试讲得出来 + 两三个月内能做完」**。不追求工程完备性，
不做知识性的补全。价值低的分支直接砍。

---

## 一、环境（先读，坑都在这）

开发机是个人笔记本（Windows + WSL2），不是公司电脑。YonWork 桌面端只有 Windows 版。

| 项 | 值 |
|---|---|
| YonWork 安装路径 | `D:\yonwork\`（WSL: `/mnt/d/yonwork/`），**不许改里面任何文件** |
| YonWork 版本 | 1.0.10，内部代号 `yonclaw`，内嵌 OpenClaw 2026.7.1-2 |
| 网关实际加载的代码 | `resources/openclaw/gateway-bundle.mjs`（**不是** `dist/`，见坑 6） |
| 运行时信息文件 | `C:\Users\z2233\AppData\Roaming\yonwork\host-api-runtime.json` |
| 数据目录 | `%APPDATA%\yonwork\profiles\<id>\userData\`（`.env` 的 `YONWORK_DATA_DIR`） |
| 应用自带 node | `/mnt/d/yonwork/resources/bin/node.exe`（v22.22.3） |
| 早期调查产物 | `/home/leorxx/code/test/`（含 1.0.8 的 `yonwork-asar/` 解包结果） |

**坑 1 —— HTTP 代理劫持（最容易误判）**
shell 里有 `http_proxy=http://127.0.0.1:7897`，且 `no_proxy` 用的 `127.*` 通配 curl 不认。
不加绕过的话请求会被代理吞掉返回空，**看起来像「服务没起来」，实际是假阴性**。
所有探测命令必须 `--noproxy '*'`，或脚本开头 `export no_proxy='*' NO_PROXY='*'`。

**坑 2 —— WSL 网络是 mirrored 模式**
`.wslconfig` 里 `networkingMode=Mirrored`，WSL 的 `127.0.0.1` **就是** Windows 的 loopback，
原生 curl/python 直连即可，**不需要 `powershell.exe` interop**。
（默认的 NAT 模式下不成立，别把这个结论套到别的机器上。`10.70.242.1` 是局域网路由器，不是 Windows 主机。）

**坑 3 —— 环境变量传不到目标进程（两个方向都会踩）**

*WSL → Windows*：调 `node.exe` 等 Windows 程序时必须用 `WSLENV` 声明：
`WSLENV=FOO FOO=bar node.exe ...`，否则 `process.env.FOO` 是 undefined。

*Windows 进程之间*：`setx` 只写注册表，**已在运行的进程不会重读自己的环境块**。
YonWork 是 **uTools 拉起来的**，改了环境变量光重启 YonWork 没用，
**得先重启 uTools**（或注销重登）。查父进程：
```bash
powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='YonWork.exe'\" | Select ProcessId,ParentProcessId,CreationDate"
```

**坑 4 —— 重启 YonWork 只能用 uTools / 开始菜单，别从 WSL 拉起**
本机靠三个注册表变量关掉了产品默认的局域网暴露（[产品缺陷 #1](docs/product-defects.md)）：
`YONCLAW_HOST_API_BIND=127.0.0.1`、`YONCLAW_HOST_API_AUTH_MODE=token`、`YONCLAW_CDP_PROXY_BIND=127.0.0.1`。
从 WSL 拉起的 Windows 进程拿不到它们，会**静默退回 `0.0.0.0` + 免鉴权**。
每次重启后验：`netstat.exe -ano | grep -E ":(3211|922[0-9]) .*LISTEN"` 里没有 `0.0.0.0`，
且不带 token 调 `/api/auth-runtime/session/status` 返回 401、带 token 返回 200。
**千万别设 `YONCLAW_CDP_ENABLED=0`**。回退：`reg delete "HKCU\Environment" /v <变量名> /f`。

**坑 5 —— 端口一个都别硬编码**
Host API 从 `host-api-runtime.json` 读 `port` 和 `token`（3211 被占会回落随机端口）；
CDP 从 `userData/DevToolsActivePort` 读（2026-09-23 重启后从 9222 回落到 9223）。

**坑 6 —— 查产品源码先查 bundle**
运行中的网关是 `node.exe … gateway-bundle.mjs gateway`。`dist/` 是同一份源码的可读拆分，
适合先读懂再去 bundle 里核对；**结论必须在 bundle 上成立**。主进程在 `app.asar`，
用 `scripts/extract_asar.py` 解包，不需要 Node。

---

## 二、核心架构决策（已定，不要推翻）

1. **不做 UI 自动化。** UI 能覆盖的只是确定性的壳（登录、建智能体、点发送），价值最低；
   真正会坏的（模型行为、工具调用、长任务、会话状态、计费）UI 断言不了。
   Electron + 客户端频繁改版，最容易烂尾。
2. **Power Automate Desktop 已废弃。** 改走 YonWork 自带的 Host API，纯 Python + HTTP。
   `archive/yonwork自动化.txt` 是旧 PAD 流程导出，**只作历史记录，不要维护**。
3. **断言不写在驱动层。** 驱动只负责发请求、收原材料、记时间窗；判定逻辑全在 Python 里，
   可单测、可 diff。任何把判断写回驱动层的做法要拦。
4. **失败必须分类**，混在一起统计数据就脏了：
   - `Error` 工具/流程自己的问题
   - `Fail` 产品的问题
   - `Timeout`
   - `Invalid` 数据无意义（请求根本没到 API、跑错通路、我们自己把工具关了却跑工具用例）
5. **断言五层**：完成性 → 产物 → 日志 → 内容（只做弱断言：禁止词、期望关键词、长度下限、
   JSON 可解析）→ 成本与性能（耗时阈值、token 异常跳变）。
   模型输出不能直接断言，所以断言工具调用和产物这些**不变量**。

---

## 三、YonWork Host API 速查

完整调查报告见 [docs/history/yonwork-automation-report.md](docs/history/yonwork-automation-report.md)
（1.0.8 时点，凭据已脱敏，**别往回填真实值**）。以下是必须内化的部分。

**主力通路**：`POST http://127.0.0.1:<port>/api/chat/send`（SSE），完整跑一轮对话，不碰界面。
配套 `/api/sessions/*`、`/api/usage/recent-token-history`、`/api/events`（全局 SSE，任何来源的轮次都推）。

**四个会静默出错的点：**

1. **`idempotencyKey` 事实必填**，不传直接 500（内部无条件 `.trim()`）。
   **且 `runId` 直接取它的值** —— 把 BenchmarkId 传进去，产物关联从源头解决。
   模型调用 hook 的 `event.runId` 也是它（2026-09-23 实测）。
2. **SSE 终止判定**：认 `event: chat.complete`，或 `state:"final"` 的 `chat.message`。
   `stream==="compaction"` 和 `stopReason==="tooluse"` **不是终止**，误判会把轮次提前截断。
   SSE **不上报工具调用**，工具调用靠驱动的 `enrich()` 从会话 JSONL 补。
3. **端口和 token 从 `host-api-runtime.json` 读**，请求里**一律带 `Authorization: Bearer <token>`**
   （出厂默认 `AUTH_MODE=trusted` 不校验，本机已切 `token` 模式，不带就 401）。
4. **每轮必须用全新 `sessionKey`，而且要全小写**（`agent:main:<benchmarkid>`）。
   复用会让第 N 轮看见第 N-1 轮的上下文，**基准数据静默作废且不报错**。
   带大写字母会触发[产品缺陷 #5](docs/product-defects.md) 的会话分裂。
   `idempotencyKey` / `runId` 不要跟着小写——服务端原样保留，动了会打断用量匹配。

**`modelSelection` 只认 `{"providerAccountId", "modelId"}`**，写错形状静默回落默认模型
（[产品缺陷 #4](docs/product-defects.md)），`model-match` 断言兜底判 Invalid。

**备用通路**（不是主力）：
- `yonworkctl`（`resources/cli/yonworkctl.mjs`）—— **裸跑必失败**（[产品缺陷 #2](docs/product-defects.md)），
  必须设 `YONCLAW_HOST_API_URL` + `WSLENV`。
- `openclaw agent --json` —— 不需要应用在跑，但回落 embedded（35s vs 3s），**测的不是用户真实链路**。
- CDP —— **只在需要验证 UI 本身时用**，别拿来跑对话基准。

---

## 四、数据与测量铁律

这个项目反复栽在同一类问题上：**数字看着正常，其实是假的**。下面每条都是栽过之后定的。

**四-1 缺失不是 0。** 没采到就留空、标 `missing` / `unavailable`，绝不补 0。
「没开采集」（`disabled`）、「开了但一个都没采到」（`unavailable`）和「确实 0 次」必须分开——
混了就是[产品缺陷 #3](docs/product-defects.md) 那种静默漏记，只是这次是我们自己造的。
工具调用同理：完整采集才能确认 0 次；日志缺失 → Invalid，读取错误 → Error。

**四-2 精确关联，串行执行。** 请求 / 用量归属只认**标识全等**（产品原生头、hook 绑定的 span、
session-id），命中不了就记未归属，**绝不按时间窗猜**；两个来源说法不一记冲突，不替它挑。
时间窗匹配只在还没有精确标识的来源上作为兜底，且要标注 `match` 方式。
因此**同时只有一个 Worker**（MySQL 咨询锁，CLI 实跑也抢同一把锁），并发往后放。
锁拦不住外部流量：跑批期间别在产品里手动聊天、别让别的进程用同一个 NewAPI 令牌。

**四-3 统计口径跑之前定死。** 报**中位数 + 最小/最大 + 有效样本数**，不报均值；
跨度超过关心的效应量就标「样本不足」。**判定阈值在看到数据前写下来**，看到数据后不改阈值、
不追认新指标。n=1 只能证明链路通了，不是性能结论。「排除了某个问题」本身就是合格产出。
差值（如 UI 滞后）能抵消后端波动，绝对量（Long Task、token）不能。

**四-4 用量按来源独立汇总，不混加。** 端上、会话 JSONL、CLI、代理、NewAPI 后台各算各的，
报告显示每个来源的覆盖率。**没有单一底噪**：同一句「你好！」实测 5,514～16,238，
同一轮换个来源能差 7.6 倍。token 阈值一律按 Case 声明 `max_input_tokens`，不声明只记录。
（线索，n=1：会话 JSONL 的 input 可能只是缓存未命中那部分，没确认前不换算。）

**四-5 时钟绝不跨端相减。** Windows 比 WSL 快过 7.85s。跨进程只取**时长**，
绝对时刻只在同一个时钟里比。

**四-6 耗时三列。** 外层 wall time（`duration_seconds`，耗时断言用它）、产品自报内部耗时、
两者差值；差值**不等于冷启动**。产品不自报就留空显示「不适用」，不填 0。

**四-7 被测对象的状态要跟着数据走。** 本机 YonWork 装了我们的 hook 扩展，**不是出厂状态**；
两个产品的「默认模型」是不同模型，那种对比是「产品默认配置」对比，不是同模型对比。
对外讲任何数字前先看 [docs/conclusions-and-risks.md](docs/conclusions-and-risks.md)。

**四-8 历史判定不自动重写。** 口径修了，旧批次的 verdict 保持原样，新口径只用于新数据。

---

## 五、工程约定（改代码前看）

**驱动与断言**
- 跨产品的词表（`NORMAL_STOP_REASONS` / `USAGE_SOURCES` / `EXACT_MATCHES`）在**断言层加取值**，
  不让驱动把自己的值映射成别人的——那等于驱动在判定（二-3）。
- 驱动协议方法（如 `enrich`）缺失抛的 `AttributeError` **不吞**：新驱动会静默少一份原材料。
- 新增用量来源要同时进 Web 查询；`web/tests/test_queries.py` 有按来源的回归，忘了会当场红。
- 设置一律「先环境变量后 `.env`」（容器靠 Compose 注入，宿主机只有 `.env`），只读 `os.environ` 会静默失效。

**数据库**
- `infra/schema.sql` 与 `runner/job_store.py` / `runner/ingest.py` 里的建表语句是**有意的两份**
  （schema.sql 只在数据目录为空时执行）。**加列两处都改，再补一段按需 ALTER**。
- 来源类字段用 VARCHAR 不用 ENUM（新值会被 MySQL 静默拒掉）。
- `model_requests.benchmark_id` 允许 NULL：未归属请求属于批次，不属于任何一轮。

**Web 与 Worker**
- `web/tests` 必须真离线：`WebApiTests.setUp` 里给 `_active_job` 打的桩别去掉，
  否则本机 MySQL 起着时单测会真的连库执行 DDL。
- 模式标签由服务端从 product + model_query 推，不收客户端显示名；同一模式重复提交要拒；
  `claim_next_job` 排序里的 `plan_position` 不能省（同计划任务 `created_at` 全相同）。
- 容器 Worker 跑不了 WorkBuddy（没有 WSL interop），用 `./scripts/host_worker.sh`；
  切过去后别用裸 `docker compose up -d`（会把容器 Worker 拉起来抢锁）。

**采集代理（`runner/modelproxy/`）**
- 常驻 compose 服务 `collector`，端口固定（3312），产品配置里存的是 URL。
  `runner/` 是挂载进容器的，**改了代码要 `docker compose restart collector`**。
- 只观察不干预：未归属、迟到请求照常转发；只有凭据不对和路径不认识才拒（记 `rejected`）。
  代理不重试；`upstream_attempts` 恒为 None（网关内部重试看不见）。
- `close_run()` 只改归属标记，不代表不会再有请求。
- **账本写入跟着记录走，不跟着「当前文件」走**：常驻服务按批次切账本，一条记录的每次更新
  都写回它见过的每个账本，外加所属轮次的账本（`CollectorProxy._write`）。直接写 `self._ledger`
  会让跨批次的迟到收尾 / 迟到绑定落进下一批。入库时归属到别的批次的记录不入本批。
- 驱动不改产品配置：产品的 baseUrl 要手动指向采集入口；关掉采集时也要手动改回直连 NewAPI。

**YonWork hook 扩展（`plugins/benchmark-trace-bridge/`）**
- 装卸：`.venv/bin/python -m scripts.install_trace_bridge status|install|uninstall`，改完按坑 4 重启。
- **YonWork 更新或重启后跑 `install_trace_bridge verify`**（配置、当前网关是否加载、投递回执、
  版本、安全缓解），再用统一代理跑一批 smoke 后 `verify --batch results/<批次>` 做端到端核对。
  版本不在 `VERIFIED_VERSIONS` 里会提示；`--batch` 全过之后才改那个常量，那就是重新验收的记录。
- 关联键是 **`(traceId, spanId)`，不是 callId**：callId 随 attempt 重置，一轮里会重复。
- 只订阅 `model_call_*`，不申请 `allowConversationAccess`（最小权限）；handler 不许阻塞。
- 依赖产品 hook 的具体形状，**YonWork 每次升级都要重新验**（1.0.10 就把原生头弄丢过）。

**WorkBuddy**
- `--max-turns 1` 和工具冲突；`--permission-mode dontAsk` 等于直接拒绝工具。
- `bypassPermissions` **只跟显式的 `allow_tools` 走**，是真放权（模型可在本机执行任意命令），不许设成默认。
- 它拿 `models.json` 的 `id` 当发给网关的模型名，不能新建同模型条目做对照。
- 它的工作目录是 `\\wsl.localhost\...` UNC 路径，CMD 不支持；比较「当前目录」类用例前先定死工作目录。

**客户端体验探针（`runner/client_probe/`，已结项）**
- UI 驱动白名单只有 `open-session` 一个动作（`ui.py` 的 `ALLOWED_ACTIONS`），加动作要先改本条。
- 选择器靠语义身份，不靠位置/索引；「滞后为负」的自检必须保留。选择器按 1.0.8 校准，升版要重校。

---

## 六、文档怎么维护

- **状态和待办只写在 README**（「当前状态」「待办」两节，唯一权威位置）。
  做完一件事去改它：**改写成「现状 + 剩余部分」，不直接删**——栽过：把没做完的当成做完了。
- **本文件只收规则。** 新规则加进对应节，附一句为什么；**不写过程、不写实测流水、不写单测数量**
  （数量会过期，需要时跑一下就知道）。
- **过程和证据写专题文档**（`docs/*.md`）。专题完结或被取代后 `git mv` 进 `docs/history/`，
  同步更新 [docs/README.md](docs/README.md) 的索引，并修好引用它的链接。
- 引用写法：规则「CLAUDE.md 四-2」、缺陷「产品缺陷 #N」、历史「开发日志 七-3.5」。
  **节号和缺陷编号只增不改**，代码注释靠它们定位。

---

## 七、不要做的事

- 不要写 UI 自动化（pywinauto / PAD / 坐标点击）。
- 不要把断言逻辑写进驱动层。
- 不要修 `archive/` 里的任何东西，尤其是 `yonwork自动化.txt` 里的 PAD 问题，那个流程已废弃。
- 不要修改 `D:\yonwork\` 下的任何文件。
- 不要硬编码端口（3211 / 9222 / 随机端口）或任何 token。
- 不要从 WSL 拉起 YonWork（坑 4）。
- 不要往本文件或 README 里追加过程叙事，写进专题文档。
