# YonWork 基准测试工具链

> 本文件是跨机器的项目对齐文档。开发机已从 Mac 迁到 **个人笔记本（Windows + WSL2）**，
> 因为 YonWork 桌面端只有 Windows 版。最后更新 2026-09-20。

## 这个项目是什么

给用友的 AI Agent 产品 **YonWork**（桌面端 + 云端）做基准测试：批量跑 prompt，
采集耗时/token/成本，自动判定通过与否。

取舍标准：**「面试讲得出来 + 两三个月内能做完」**。不追求工程完备性，
不做知识性的补全。价值低的分支直接砍。

---

## 一、环境（先读，三个坑都在这）

开发机是个人笔记本，不是公司电脑。

| 项 | 值 |
|---|---|
| YonWork 安装路径 | `D:\yonwork\`（WSL: `/mnt/d/yonwork/`） |
| YonWork 版本 | 1.0.8，内部代号 `yonclaw`，引擎是内嵌 OpenClaw 2026.6.11 |
| 运行时信息文件 | `C:\Users\z2233\AppData\Roaming\yonwork\host-api-runtime.json` |
| 应用自带 node | `/mnt/d/yonwork/resources/bin/node.exe` (v22.22.2) |
| 上一次调查的产物 | `/home/leorxx/code/test/`（含 `yonwork-asar/` 解包结果、`ctx.py`） |

**坑 1 —— HTTP 代理劫持（最容易误判）**
shell 里有 `http_proxy=http://127.0.0.1:7897`，且 `no_proxy` 用的 `127.*` 通配 curl 不认。
不加绕过的话请求会被代理吞掉返回空，**看起来像「服务没起来」，实际是假阴性**。
所有探测命令必须 `--noproxy '*'`，或脚本开头 `export no_proxy='*' NO_PROXY='*'`。

**坑 2 —— WSL 网络是 mirrored 模式**
`.wslconfig` 里 `networkingMode=Mirrored`，WSL 的 `127.0.0.1` **就是** Windows 的 loopback，
原生 curl/python 直连即可，**不需要 `powershell.exe` interop**。
（注意：默认的 NAT 模式下不成立，别把这个结论套到别的机器上。`10.70.242.1` 是局域网路由器，不是 Windows 主机。）

**坑 3 —— 环境变量传不到目标进程（两个方向都会踩）**

*WSL → Windows*：调 `node.exe` 等 Windows 程序时必须用 `WSLENV` 声明：
`WSLENV=FOO FOO=bar node.exe ...`，否则 `process.env.FOO` 是 undefined。

*Windows 进程之间*：`setx` 只写注册表，**已在运行的进程不会重读自己的环境块**。
2026-09-21 实测：设完三个 `YONCLAW_*` 变量后重启 YonWork，绑定地址纹丝不动——
因为 YonWork 是 **uTools 拉起来的**，而 uTools 那个进程 9:18 就起了、比 setx 还早，
传给子进程的是那份旧环境。
**光重启目标应用没用，得先重启启动它的那个进程**（或注销重登）。
查父进程：
```bash
powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='YonWork.exe'\" | Select ProcessId,ParentProcessId,CreationDate"
```

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
   - `Invalid` 数据无意义（如请求根本没到 API）
5. **断言五层**：完成性 → 产物 → 日志 → 内容（只做弱断言：禁止词、期望关键词、长度下限、
   JSON 可解析）→ 成本与性能（耗时阈值、token 异常跳变）。
   模型输出不能直接断言，所以断言工具调用和产物这些**不变量**。

---

## 三、YonWork Host API 速查

完整调查报告见 `docs/yonwork-automation-report.md`。以下是必须内化的部分。

**主力通路**：`POST http://127.0.0.1:3211/api/chat/send`（SSE），完整跑一轮对话，不碰界面。
配套 `/api/sessions/*`、`/api/usage/recent-token-history`、`/api/events`。

**四个会静默出错的点：**

1. **`idempotencyKey` 事实必填**，不传直接 500（内部无条件 `.trim()`）。
   **且 `runId` 直接取它的值** —— 把 BenchmarkId 传进去，产物关联问题从源头解决，
   *不再需要按时间窗匹配产物*。
2. **SSE 终止判定**：认 `event: chat.complete`，或 `state:"final"` 的 `chat.message`。
   `stream==="compaction"` 和 `stopReason==="tooluse"` **不是终止**，误判会把轮次提前截断。
3. **端口别硬编码**。3211 被占用会回落随机端口。从 `host-api-runtime.json` 读 `port` 和 `token`。
   当前构建 `AUTH_MODE=trusted` 免鉴权，但**请求里照样带 `Authorization: Bearer <token>`**，
   将来切 token 模式脚本不用改。
4. **每轮必须用全新 `sessionKey`，而且要全小写**（`agent:main:<benchmarkid>`）。
   复用会让第 N 轮看见第 N-1 轮的上下文，**基准数据静默作废且不报错**，
   这是最难查的一类污染。等价于 PAD 里每轮点「新建任务」。
   带大写字母则会触发第六节-5 的会话分裂，**界面上看不到完整对话**。
   `idempotencyKey` / `runId` 不要跟着小写——服务端原样保留，动了会打断用量匹配。

**成本基线**：**没有单一底噪，别再找这个数。** 最小 prompt 曾实测 20832，
但同一句「你好！」后来实测到 5,514 ～ 16,238，同一轮换个用量来源还能差 7.6 倍
（长文本：session-jsonl 30,082 / NewAPI 228,354）。
阈值一律按 Case 声明 `max_input_tokens`，不声明就只记录不判定，见七-0.2。

**备用通路**（不是主力）：
- `yonworkctl`（`resources/cli/yonworkctl.mjs`，60+ 子命令）—— 官方 CLI，退出码规范，适合 CI 判定。
  **裸跑必失败**，见第六节缺陷 2，必须设 `YONCLAW_HOST_API_URL` + `WSLENV`。
- `openclaw agent --json` —— 唯一不需要应用在跑的路线，但实测回落 embedded（35s vs 3s），
  **测的不是用户真实链路**，做产品基准会失真。
- CDP `127.0.0.1:9222`（默认就开，端口固定）—— **只在需要验证 UI 本身时用**，别拿来跑对话基准。

---

## 四、现有资产

目录结构和上手命令在根目录 `README.md`。这里只记**为什么留着**。

| 路径 | 说明 | 状态 |
|---|---|---|
| `runner/` | 驱动层（`drivers/`）+ 五层断言 + 入库，主链路 | 在用 |
| `web/` | 测试控制台 + 报告，FastAPI + Jinja + 本地原生 JS | 在用 |
| `infra/` | 结果库 MySQL 8.4（:3307），JSONL 可随时重放 | 在用 |
| `newapi/` | **被测对象**的模型网关（:3000），不是我们的基础设施 | 在用 |
| `cases/catalog.yaml` | Git 版本化的主用例源，含用例集与断言 | 在用 |
| `cases/yonwork_benchmark.xlsx` | 旧 prompt 清单和历史结果 | 只作兼容，不再默认读取 |
| `compose.yml` / `Dockerfile` | MySQL、NewAPI、Web、串行 worker 一键环境 | 在用 |
| `scripts/newapi_stats.ps1` | 拉 NewAPI 后台用量 | **保留**，见下 |
| `scripts/extract_asar.py` | 无需 Node 的 asar 解包/grep 工具 | 查源码时还用得到 |
| `docs/yonwork-automation-report.md` | 完整调查报告（已脱敏） | **权威参考** |
| `benchmark-companion/` | **WorkBuddy** 的人工跑批 GUI（热键计时 + SQLite + Excel 同步） | 在用，见下 |
| `yonwork_usage/` | 解析 llm-observer JSONL 取 token | 主用途已被 `sessionlog.py` 取代，见下 |
| `archive/` | PAD 流程导出、CDP 探测脚本、旧错误日志、当初的调查 prompt | 历史记录，不维护 |

**`scripts/newapi_stats.ps1` 不是冗余**：`/api/usage/recent-token-history` 是**端上**数据，
它拉的是 **NewAPI 后台**数据。日常对账已经进了 `runner/reconcile.py`，
这个脚本留作手工交叉验证——它不依赖我们自己的任何代码，
所以当 runner 的数字可疑时，用它判断到底是谁错了。这是**两端，不是重复**。

**`benchmark-companion/` 可以准备退场了**：它服务的是 **WorkBuddy**，不是 YonWork。
`runner/drivers/workbuddy.py` 已经实测跑通（见七-3.2），
**WorkBuddy 那两个模式终于和 YonWork 那两个走同一条自动通路、同一套断言**——
「四个模式里有一半数据质量低一档」这个缺口到此补上。
先别急着删：目前只验证过默认模型、单轮、关工具那一种组合，
等多模型和工具调用的 case 也跑过一轮，再决定它的去留。

**`yonwork_usage/` 先别删**：它读 `llm-observer/*.jsonl`，`runner/sessionlog.py` 读
`sessions/*.jsonl`，**是两个不同的文件**。取 token 已被覆盖，但 llm-observer 独有
`tokenAmplification`（累计 token / 最后一轮 token，实测 **7.78×**）——
「一轮对话内部重放了多少上下文」的直接指标，sessionlog 给不出来。删之前先把它搬进 runner。

---

## 五、为什么迁移（PAD 9 条问题对照）

旧 PAD 流程确认过 9 个问题，迁到 Host API 后 **8 条直接消失**：

| # | 原问题 | 迁移后 |
|---|---|---|
| 1 | 一次超时整批中止（产品出最有价值缺陷时测试工具自己崩了） | 每轮 try/except 隔离 |
| 3 | 两个 WAIT 无显式超时，退化成 Error 而非 Timeout | `chat.complete` + 客户端 timeout |
| 4 | 剪贴板污染（跑批时人复制东西会换掉 prompt） | 没有剪贴板 |
| 5 | 开头弹窗需人点 Yes/No，无法无人值守 | 没有弹窗 |
| 6 | 每轮 Save 两次 Excel，60 轮 = 120 次 COM Save | JSONL 追加，整批汇总 |
| 7 | BenchmarkId 只在一个分支生成，无法关联 | `idempotencyKey` 即 `runId` |
| 8 | EXIT Code 恒为 0 | 自定义退出码 |
| 9 | PAD 导出含 42 万字符 base64 截图，git diff 不可读 | 没有 PAD 导出 |

**唯一存活的是第 2 条**：`ErrorCalls` / `APICalls` 采集了却从不判断。
那本来就不是 PAD 的锅，是断言缺失，换什么驱动都得自己写。

---

## 六、已查到的 YonWork 产品缺陷（本职工作产出）

这五条比自动化工具本身更值钱，应走公司内部渠道上报（先确认是否为测试构建有意放宽）。
前两条是调查阶段查到的，后三条是搭 runner 和跑批的过程中撞出来的。

1. **高危：本地服务默认对局域网开放且免鉴权。**
   Host API 默认 `BIND=0.0.0.0`、`AUTH_MODE=trusted`（跳过全部 token 校验）；
   CDP TCP 代理默认 `0.0.0.0:9222`（应用日志自己写着「任何机器都可驱动本应用，风险极高」）。
   实测无任何凭据调 `/api/auth-runtime/session/status` 拿到完整登录态
   （accessToken、userId、tenantId、用户名）。叠加后 = 同网段任何人可无凭据调用全部 364 条路由。
   **本机已于 2026-09-21 缓解**（见七-0.1），实测改完两个 `0.0.0.0` 消失、
   无 token 调该端点返回 401，而 CDP 和整条自动化链路零改动照常工作：
   `YONCLAW_HOST_API_BIND=127.0.0.1`、`YONCLAW_HOST_API_AUTH_MODE=token`、`YONCLAW_CDP_PROXY_BIND=127.0.0.1`。
   **但产品默认值没变，这条缺陷本身依然要上报。**
2. **`yonworkctl` 路径不匹配，官方 CLI 完全不可用。**
   主进程写 `%APPDATA%\yonwork\host-api-runtime.json`，CLI 读 `%APPDATA%\yonclaw\`，
   导致应用在跑时任何命令都返回「YonWork is not running」+ 退出码 7。
   说明该 CLI 在这个构建上从未被端到端验证过。
3. **端上 `/api/usage/recent-token-history` 在漏记。**（2026-09-20 实测）
   改过模型配置之后，连续 6 轮完成的对话一条都没进这个端点，
   而同样这些轮在会话 JSONL 和 NewAPI 后台**都有记录，且两者数字完全一致**
   （16,179 / 16,188 / 16,435）。两个互相独立的来源对得上，产品自己的端点是那个异常值。
   看 `/reconcile/<suite_id>` 页，一眼能看出来。
4. **`modelSelection` 字段名写错时静默回落默认模型。**
   只认 `{"providerAccountId", "modelId"}`，写别的形状**不报错**：
   HTTP 200、答案正常，实际跑的却是智能体默认模型。
   会话日志里有实证（`probe-mode` / `probe-prov` 两轮跑的是 `deepseek-v4-flash`）。
   已加 `model-match` 断言兜底，判 `Invalid`。
5. **`sessionKey` 大小写不一致，一轮对话被拆成两条会话。**（2026-09-21 实测）
   `sessionKey` 里带大写字母时，应用会存出两条会话元数据：
   原样大小写那条只有标题（`displayName`），**没有 `sessionId`、没有对话内容**；
   全小写那条有 `sessionId` 和完整对话，**但界面里点不到**。
   症状是「用户消息和 Agent 回答不在一个界面」。
   统计（`/api/sessions/list-metadata`）：含大写的 key **10 个里 9 个分裂**，
   全小写的 4 个**一个都没分裂**；改成全小写后新跑的轮次恢复成单条。
   说明写会话元数据和建会话绑定这两条路径对 key 做了不同的规范化。
   我们这边已经规避（`session_key_for` 统一小写），但**产品自己应该修**——
   任何用混合大小写 sessionKey 的调用方都会踩到，而且不报错。

⚠️ `docs/yonwork-automation-report.md` 的凭据**已于 2026-09-20 脱敏**
（host-api token / accessToken / gateway token / 用户名 → `<REDACTED:…>`）。
别往回填真实值。

---

## 七、待办（按优先级）

主链路已完工：驱动 + YAML 用例 + 五层断言 + 三来源用量 + 持久任务队列 + MySQL +
Web 测试控制台与报告全部接通，runner 132 个、web 8 个离线单测。
链路命令见根目录 `README.md`。

下面按**批次**排，同一批内可以任意顺序，跨批有依赖。

### 第 0 批 · 今天就能清掉（合计 < 1 小时）

0.1 ~~关掉第六节-1 的两个默认暴露~~ **已完成 2026-09-21。**
   （拖了最久的一条：2026-09-20 复查时 `0.0.0.0:3211` 和 `0.0.0.0:9222` 还在 LISTEN，
   这台是个人笔记本，接公司 WiFi 时同网段任何人都能无凭据调用全部 364 条路由。）

   **改完实测：**
   ```
   127.0.0.1:3211   LISTENING   ← 原 0.0.0.0，没了
   127.0.0.1:9222   LISTENING   ← 原 0.0.0.0 + 127.0.0.1 两条，现在只剩本体
   无 token  调 /api/auth-runtime/session/status → HTTP 401
   带 token  调同一端点                          → HTTP 200
   ```
   401/200 那一对是关键——说明 `AUTH_MODE=token` 真的在拦，不是摆设。
   那正是以前无凭据就能拿到完整登录态（accessToken / userId / tenantId / 用户名）的端点。
   CDP 仍通（`Chrome/144.0.7559.236 Electron/40.9.1`，1 个 renderer target），
   `--case-set smoke --dry-run` 前置检查照常过，自动化零改动。

   - 设 `YONCLAW_HOST_API_BIND=127.0.0.1`、`YONCLAW_HOST_API_AUTH_MODE=token`、
     `YONCLAW_CDP_PROXY_BIND=127.0.0.1`，然后重启 YonWork。
   - **千万别设 `YONCLAW_CDP_ENABLED=0`**，那会直接废掉 0.3 的体验探针。
   - `0.0.0.0:9222` 是 TCP 代理，`127.0.0.1:9222` 是 Chromium 本体，**两个独立监听**。
     改绑后代理要么只绑 loopback、要么因端口被本体占用而起不来——两种结果都是
     「局域网暴露没了，本地 CDP 照常」。
   - **切 token 模式对我们无影响，已核实**（2026-09-21）：五个调用点
     `discovery.health_check` / `discovery.session_status` / `catalog.list_model_choices` /
     `usage` / `client` 全部经 `transport._headers` 带 `Authorization: Bearer`。
   - ⚠️ **只重启 YonWork 不够**，见坑 3：它是 uTools 拉起来的，
     uTools 不重启就一直传旧环境。2026-09-21 已在这里栽过一次。
   - 改完验三条：`netstat` 里两个 `0.0.0.0` 消失且 `127.0.0.1:9222` 还在、
     `curl --noproxy '*' 127.0.0.1:9222/json/version` 仍通、
     `python -m runner --case-set smoke --dry-run` 前置检查仍过
     （第三条是验 `AUTH_MODE=token` 之后鉴权没断）。

   回退：`reg delete "HKCU\Environment" /v <变量名> /f`，三个变量原先都未设置。
   ⚠️ 这只是**本机缓解**。第六节-1 作为产品缺陷依然成立（默认值仍是 `0.0.0.0` + `trusted`），
   照常上报。

0.2 ~~重定成本阈值~~ **已完成 2026-09-21。**
   查库后发现原来的判断错了一半：天花板 `20832 × 2 = 41,664` 对「你好！」**永远不触发**，
   真正的假阳性在另一头——长文本用例走 NewAPI 那一路 **228,354**，
   是工具调用反复重放文件的正常开销，却会被判成 Fail。

   实测分布（`usage_samples`）：同一句「你好！」**5,514 ～ 16,238**（3 倍差）；
   长文本同一轮 session-jsonl 记 **30,082**、NewAPI 记 **228,354**（7.6 倍差）。
   **一个常数既管不了寒暄也管不了长文本，跨来源比更没有意义。**

   做法：删掉全局常数，阈值改成按 Case 声明 `max_input_tokens`
   （YAML 和旧 Excel 的 `MaxInputTokens` 列都支持）；没声明就只记录实测值和来源，
   **不判定、不猜**。断言名从 `input-token-jump` 改成 `input-tokens`。
   记录下来的值就是将来定分位数基线的原料——现在样本太少（每个 Case 个位数），
   **先攒数据再定阈值**，别急着填一个拍脑袋的数。

### 第 1 批 · POC 之前必须先回答的那个问题（半天）

1.1 **Spike：能不能让 UI 渲染一轮由 Host API 发起的对话。**
   **2026-09-21 已查。结论：能，POC 路线成立，但还差「每轮换新会话」这一块拼图。**

   **关键实测——UI 会实时渲染 API 发起的轮次，前提是那一轮发给「UI 当前打开的会话」。**
   往 `agent:main:main`（UI 首页那个会话）发一轮，CDP 侧看到完整的流式过程：
   ```
    0.00s  len=313   空白首页
    8.64s  len=253   "等待结果返回"                 ← loading 出现
   10.66s  len=281   "命令执行中…已等待 00:01"
   11.92s  len=254   "我是"                         ← 首段文本，流式
   12.38s  len=398   完整回答                       ← 最后一次变化
   ```
   同轮 Host API 侧 `first_delta=9.427s / duration=10.377s`（观察器早起 2.0s）：

   | 指标 | UI | Host API | Client Overhead |
   |---|---|---|---|
   | 首字 | ~9.92s | 9.427s | **~0.5s** |
   | 完成 | ~10.38s | 10.377s | ~0 |

   **POC 想要的那个指标，第一次测就出来了**（0.4s 轮询精度很粗，
   真正的 MutationObserver + `performance.now()` 会准得多）。

   **反过来，发给 UI 没打开的会话就完全不渲染**：UI 停在会话 A 时往新会话 B 发一轮，
   DOM 只有 665 → 662 → 620 的相对时间戳抖动，新消息始终没出现。

   **查完的死路（别再走一遍）：**
   - Host API **没有**任何「切换当前会话」的路由。
     `/api/chat/*`、`/api/sessions/*` 全量列过，只有 query / pin / rename /
     upsert-metadata / delete / compact 这些，没有 activate / select / switch。
   - `/api/sessions/bootstrap-local` 名字像创建，其实是个 **GET 列表**接口。
   - renderer **没有** `/chat/:sessionKey` 路由，会话靠 React state 选，改 hash 没用。
   - `/api/webview-agent/dispatch` 是真的动作分发入口，动作名
     `agent:webview:new-session`（返回新 sessionKey）和 `agent:webview:open-chat` 都在，
     **但它服务的是内嵌 webview，不是主聊天界面**：无绑定时返回
     `webview target not found for current session`。值得再看一眼
     `/api/webview-agent/virtual-binding` 能不能造一个绑定。

   **候选也查完了（2026-09-21），自动开新会话这条路走不通：**
   - **没有 IPC 能新建会话。** 主进程 179 个 `ipcMain.handle` 通道全量列过，
     会话相关的只有 `session:delete`、`workspaceSession:{registerActive,listActive,deleteActive}`。
     「新建任务」是**纯 renderer 内部的状态切换**（`navigate("/", {state:{createNewSession:true}})`，
     React Router state，外部注入不了），会话是首次发消息时才惰性创建的。
   - **webview-agent dispatch 够不到主界面。** 试了 `agent:main:main` / `agent:main` / `main`
     三个 sessionKey，一律 `webview target not found for current session`。
     那套 `agent:webview:*` 动作只服务内嵌浏览器；`virtual-binding` 的日志写着
     "virtual binding upserted **from remote browser**"，也是给外部浏览器注册用的。
   - **⚠️ `agent:main:main` 会累积上下文，不是每次都空。** 实测我们那轮落在
     `98d96aca-….jsonl`，文件里前面已经有一对 user/assistant——
     也就是上面那个 9.427s 的测量**是带着前文跑的**。反复往它发就是 CLAUDE.md 三-4 说的污染。

   **唯一没试的自动路径**：`/api/sessions/delete` 删掉当前会话，看 UI 会不会重开一个空的。
   有破坏性（会删用户的对话），试之前先确认。

   **结论：走半自动——每轮人工点一次「新建任务」，再由 API 发这一轮。**
   这不是退而求其次，而是**本来就该这么划范围**：
   Client Overhead 是**客户端的属性，不是 prompt 的属性**，
   量几轮就够，不需要跟基准跑批逐轮对齐。
   每轮隔离那条铁律是为了保证**模型/Agent 指标**干净，测 UI 渲染延迟不需要它。
   所以体验探针应当是**独立的小规模测量**，不要塞进主跑批链路。

   ⚠️ 附带的方法论坑：**在共享会话上测 UI 延迟会被上下文增长污染**——
   上下文越长首字越慢，测出来的"客户端开销"里混着模型变慢。
   共享会话连续测多轮的数字不可比，必须每轮新会话。

   ⚠️ POC 页里 T0 定义成「触发发送」，那是 UI 发起才有的时间点。
   走 API 发起要把 T0 重定义成 HTTP 请求发出时刻，时间点表跟着改。

### 第 2 批 · 客户端体验探针（独立实验，不进主跑批）

设计与评审见 Notion「YonWork 客户端体验探针：CDP 渲染监测 POC」及其「9.21 评审补充」。
**定位：Core Benchmark 管 Agent/API 层，这条管「用户什么时候看到什么」，两者不合并。**

已替它验过（2026-09-21）：CDP 通，`Chrome/144.0.7559.236 Electron/40.9.1`，1 个 renderer target。
POC 清单前三项可划掉。API 侧的被减数现成：`first_delta_ms` 已在 `runs` 表里。

**两条贯穿全程的铁律，写在最前面因为它们会悄悄毁掉数据：**

- **时钟：绝不跨端相减。** 实测 Windows 比 WSL 快 **7.85s**（renderer 用 Windows 时钟，
  Python 探针用 WSL 时钟）。被测量级 0.5～3s，误差比它大一个量级且符号固定。
  T0 一律在 **renderer 内**打点，T0–T5 全在同一时钟；Host API 侧只取**时长**
  （`first_delta_seconds` / `duration_seconds`，与时钟无关）加到同一个 T0 上。
- **UI 驱动范围：只允许点「新建任务」一个按钮。** 不打字、不碰剪贴板、不选模型，
  prompt 一律由 Host API 投递。「少量驱动 UI」不写死范围，就是当年 PAD 的起点。

2.1 **选择器勘探**（要 YonWork 在跑 + 手动发一条消息）。
   找用户消息节点、assistant 节点、loading 节点、Stop/Send 按钮的**稳定锚点**。
   优先 `data-*` / ARIA role / 语义标签，**不要用 classname**（React 打包后全是哈希）。
   ⚠️ Stop→Send 在输入框区域，**很可能不在 chatRoot 子树内**，要同时观察两处。

2.2 **`probe_renderer.py`**：MutationObserver + `performance.now()` + `requestAnimationFrame`
   + `Runtime.addBinding` 事件驱动，采 T1–T4。
   T3 叫 `first_content_dom` 不叫 `first_paint`、首字后补一帧取 `first_visible_frame`——
   这两个命名判断是对的，别改。

2.3 **T5 完成事件**。别用「N 毫秒没有文本变化」——实测 DOM 序列
   `253 → 281 → 253 → 254 → 398`，中途从「命令执行中…已等待 00:01」**退回**「等待结果返回」，
   短静默窗口会在这里误判完成。
   先找确定性标记（loading 消失 / Stop→Send / running 属性）；
   **找不到也不阻塞**：用 Host API 的 SSE 终止事件当「后端已完成」的基准真值，
   T5 定义成「后端完成之后 UI 的最后一次变化」，指标依然良定义，
   而且「UI 完成滞后」正好就是要测的量。

2.4 **串起来**：人工点「新建任务」→ 探针在 renderer 内打 T0 → Host API 发这一轮 →
   收 binding 事件 → 落 JSONL。**字段形状现在就对齐 `results.jsonl`**（`benchmark_id` 等），
   将来想入库直接复用 `ingest.py`，零返工。**暂不接 MySQL / Dashboard。**

2.5 **场景集 S1–S7**（短文本 / 5k Markdown / 20k Markdown / 大表格+代码块 /
   多轮工具调用 / 工具+产物面板 / 长历史会话）。两个会毁掉可比性的点：
   - **输出长度必须受控**——测的是「渲染 X 字节的成本」，X 每轮不一样就没法比。
     用确定性指令（原样重复 N 遍 / 恰好 N 行表格），每轮记录实际字符数，按 `ms/1k 字` 归一化。
   - **流式分片节奏和总长度同样重要**——同样 5k 字分 50 片和 500 片，
     DOM 更新次数差 10 倍。把 `text-update` 计数一并记录。
   2.3MB 文件那个场景压的是 Agent/文件处理，长 Markdown 才压 renderer，**分开看**。

2.6 **每场景 3～5 轮，看差值是否随复杂度放大。**
   `backend → visible` 是差值，后端波动大部分抵消，3～5 轮够用；
   但 **Long Task 计数、Streaming duration 是绝对量**，不抵消——
   本项目实测同一句「你好！」inputTokens 在 5,514～16,238 之间跳。
   **报中位数 + 最小/最大，不报均值**；跨度超过关心的效应量就标「样本不足」。
   **判定阈值跑之前写死**，例如「S1–S7 的 backend→visible 全部 < 1s
   且 Long Task 最大 < 200ms ⇒ 判定 renderer 不是瓶颈」——
   不预先定标准，拿到数据会不自觉往「看起来还行」上圆。
   ⚠️ **「排除客户端瓶颈」本身就是合格产出**，不要求一定发现问题。

**2026-09-21 进展：探针已跑通，2.1–2.3 完成。** `runner/client_probe/` 三个文件，
事件驱动（MutationObserver + `Runtime.addBinding`），两种模式：

- `probe_once()`——API 发起，有后端时长做被减数，能算客户端开销
- `watch_once()`——**人工在界面发送，探针只观察**，给纯用户视角时间线

实测（手动发送，1036 字回答，相对 T1 = 消息进 DOM）：

```
T2 出现「等待结果返回」    0.1 ms   ← 几乎瞬时，不存在「点了没反应」
T3 首次看到回答文字      843 ms   ← 用户最在意的数
T3' 首帧上屏 (rAF)       844 ms   ← 只比 T3 晚 1.4ms
T5 生成完成            11679 ms   流式更新 28 次
```

`first_content_dom` / `first_visible_frame` 分开命名是对的，**实测这一层只有 1.4ms**。

**选择器速查**（版本 1.0.8，客户端升版后要重校）：

| 用途 | 锚点 |
|---|---|
| 观察挂载点 | `.yonclaw-main-stage`（首页和会话页都在） |
| 会话根 | `.yonclaw-chat-scroll-region`（**首页还不存在**） |
| 消息列表 | `.yonclaw-chat-max-width`，直接子元素 = 一条 turn |
| T5 完成信号 | 气泡里「正在生成」**消失** |
| 新建任务按钮 | `.yonclaw-sidebar-new-task` / `[aria-label="新建任务"]` |

⚠️ **别用 `.prose` 当内容锚点。** 实测流式过程中它恒定 48 字不动，
整个气泡从 98 涨到 2284，**结束时才把全文灌进 `.prose`**——
盯它测出来的是「整块渲染完成」，不是「首次可见」。要取整个气泡的文本。

**踩过的四个坑，全是「静默假数据」型**（数字看着正常，只有符号或零值能暴露）：

| 坑 | 症状 | 根因 |
|---|---|---|
| 抓到历史气泡 | 滞后 −1794ms | 没排除会话里已有的轮次 |
| 抓到用户气泡 | 滞后 −1189ms | 用户消息先出现，被当成「首段内容」 |
| 占位文案残渣 | 滞后 −1962ms | 按子串剥离，「已等待 00:01」剩下残字被当内容 |
| 基线用索引 | 零事件 | 换个会话就废：列表只剩 2 条而基线是 26 |

**规律：只要锚点依赖位置或索引，状态一变就会悄悄失效。**
选择器和基线都要靠身份（元素本身 / 语义类），不能靠位置。
「滞后为负 = 物理不可能」这条自检抓出了前三个，**必须保留**。

**watch 模式的固有边界**：那一轮不经过我们的 SSE 客户端，所以**拿不到后端时长**，
算不了客户端开销；它给的是用户视角的绝对时间线。两种模式各有各的用途，别混。
另外按下发送键到气泡进 DOM 那一小段（Submit latency）**采不到**——
MutationObserver 看不到点击。T2=0.1ms 已说明这段没有可感知延迟，不值得为它越界去 hook 输入框。

### 第 3 批 · 主链路欠的账

3.1 **跑批时也采 ErrorCalls（原 §7-3）。** 五节存活的那条断言（`ErrorCalls > 0` → `Fail`；
   `APICalls == 0` → `Invalid`）目前只有事后 `reconcile` 拉 NewAPI 才有数，
   跑批当时记的是「未采集」。**别用 0 冒充**——那等于把「没采到」说成「没出错」。

3.2 ~~抽 `Driver` 接口~~ **已完成 2026-09-21。** 跨模式一键编排仍未做，见下。
   接口在 `runner/drivers/`，`batch.py` 现在对被测产品一无所知——
   隔离、逐轮落盘、判定、计数这四件事共用，产品相关的全在驱动后面。
   YonWork 主链路**已实测**（Web 提交 → Worker → 驱动 → 入库 → 报告，跑通一轮 Pass）。

   **两边形状差得越远，接口越不会写歪**，这是当初等 WorkBuddy 报告的原因：

   | | YonWork | WorkBuddy |
   |---|---|---|
   | 通路 | 常驻 Host API + SSE | 每轮一个一次性进程 + JSON |
   | 隔离 | 全新 `sessionKey`（全小写） | 全新 `--session-id` + `--no-session-persistence` |
   | 终止 | `chat.complete` / `state:"final"` | 最后一条 `type:"result"` |
   | 用量 | 事后采两个来源 | 随本轮输出回来，按 session-id 精确匹配 |

   **抽接口当场抓出来的三个「只有一个实现时看不见」的问题：**
   - `NORMAL_STOP_REASONS` 只有 YonWork 的取值，WorkBuddy 的 `success` 会被判 Fail。
     修法是**往断言层的词表里加取值**，不是让驱动把自己的值映射成 `stop`——
     后者等于驱动在判定「这算不算正常结束」，正是二-3 要拦的。
     `USAGE_SOURCES` / `EXACT_MATCHES` 同理，都是跨产品的表。
   - `usage_samples.source` 是 **ENUM**，新来源会被 MySQL 静默拒掉，
     症状跟六-3 的端上漏记一模一样，极难查。已改 VARCHAR + 入库时按需迁移。
   - `BatchOptions.product` 和驱动各存了一份产品名。已删掉前者，只认 `driver.product`。

   **WorkBuddy 活链路也实测跑通了**（2026-09-21，CLI、默认模型、`--tools ''`）：
   `runId == BenchmarkId`、`result:success`、用量 3645/8 tok、
   实际模型 `deepseek-v4.1-flash`、按 session-id 精确匹配、判定 Pass。
   报告列的七条成功判定有五条直接落在现成断言上，不用另写：`subtype` → 完成性层、
   `session_id` → 产物层 run-id（所以 `--session-id` 直接用 BenchmarkId）、
   实际模型 → 成本层 model-match。

   **实测撞出来的两条，用之前必须知道：**
   - ⚠️ **冷启动占大头**：外层 **16.744s**，内部 `duration_ms` 只有 **3.087s**——
     每个 case 一个新进程，**13.6s 是冷启动**，是模型耗时的 4 倍多。
     **别拿它直接跟 YonWork 的常驻服务比耗时**，那不是同一回事。
     两个数都存着就是为了能把这一段拆出来。
   - ⚠️ **容器里的 Worker 跑不了它**：它要起 Windows 进程，而 compose 里的 worker
     既看不到 `/mnt/d` 也没有 WSL interop（已实测确认）。
     要从 Web 控制台跑 WorkBuddy，Worker 得在宿主机原生起（`python -m runner.worker`）。
     控制台的产品选项上已经写了这个条件。

   **仍未做：跨模式一键编排**——现在还是「同一个实验名提交多次，每次换产品/模型」。

### 明确往后放的（写下来是为了不再反复捡起）

- **device-api / newapi 改按 runId 匹配（原 §7-5）**：worker 已被 MySQL 咨询锁
  **强制串行**，串行下时间窗匹配是对的，不构成当前风险。真要并发再做。
- **`infra/schema.sql` 与 `job_store.py` 的双份表定义**：已验证逐字段一致。
  重复是必要的（schema.sql 只在数据目录为空时执行一次），加个交叉引用注释即可。
- **清理那 9 条分裂会话**：留着当第六节-5 的上报证据，比清掉有用。

### 随时可做、不占开发时间

- **上报第六节的五条产品缺陷**，走公司内部渠道，先确认是否为测试构建有意放宽。
  第 5 条（sessionKey 大小写）有完整复现数据，最好上报。
  它和 1.1 那个「API 发起的轮次界面不渲染」很可能是**同一处规范化不一致的两个表现**，
  上报时一起说。

## 八、不要做的事

- 不要写 UI 自动化（pywinauto / PAD / 坐标点击）。
- 不要把断言逻辑写进驱动层。
- 不要修 `archive/` 里的任何东西，尤其是 `yonwork自动化.txt` 里的 PAD 问题，那个流程已废弃。
- 不要修改 `D:\yonwork\` 下的任何文件。
- 不要硬编码端口 3211 或任何 token。
