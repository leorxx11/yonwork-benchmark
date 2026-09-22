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

⚠️ **会话 JSONL 的 input token 可能只是「缓存未命中」那部分。**（2026-09-22，线索，n=1）
逐请求账本第一轮实测：同一轮代理和 NewAPI 都记 **16,098**，会话 JSONL 记 **7,010**，
而代理拿到的 usage 里 `prompt_cache_miss_tokens` **正好是 7,010**
（`prompt_cache_hit_tokens` 9,088）。若成立，下面那个「换个来源差 7.6 倍」
就有了具体机制。**但 n=1，没确认字段语义之前不要据此换算**，
按来源独立汇总的规矩不变。见 `docs/model-request-ledger.md`。

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
| `runner/modelproxy/` | 逐请求模型调用账本 + 采集代理 | 已接进跑批，**默认关闭、未实测**，见七-下一项 |
| `web/` | 测试控制台 + 报告，FastAPI + Jinja + 本地原生 JS | 在用 |
| `infra/` | 结果库 MySQL 8.4（:3307），JSONL 可随时重放 | 在用 |
| `newapi/` | **被测对象**的模型网关（:3000），不是我们的基础设施 | 在用 |
| `cases/catalog.yaml` | Git 版本化的主用例源，含用例集与断言 | 在用 |
| `cases/yonwork_benchmark.xlsx` | 旧 prompt 清单和历史结果 | 只作兼容，不再默认读取 |
| `compose.yml` / `Dockerfile` | MySQL、NewAPI、Web、串行 worker 一键环境 | 在用 |
| `scripts/newapi_stats.ps1` | 拉 NewAPI 后台用量 | **保留**，见下 |
| `scripts/extract_asar.py` | 无需 Node 的 asar 解包/grep 工具 | 查源码时还用得到 |
| `docs/yonwork-automation-report.md` | 完整调查报告（已脱敏） | **权威参考** |
| `docs/conclusions-and-risks.md` | 现在能说什么 / 不能说什么 / 风险清单 | **对外讲之前先看这个** |
| `archive/benchmark-companion/` | WorkBuddy 的人工跑批 GUI（热键计时 + SQLite + Excel 同步） | **已归档 2026-09-21**，见下 |
| `yonwork_usage/` | 解析 llm-observer JSONL 取 token | 主用途已被 `sessionlog.py` 取代，见下 |
| `archive/` | PAD 流程导出、CDP 探测脚本、旧错误日志、当初的调查 prompt | 历史记录，不维护 |

**`scripts/newapi_stats.ps1` 不是冗余**：`/api/usage/recent-token-history` 是**端上**数据，
它拉的是 **NewAPI 后台**数据。日常对账已经进了 `runner/reconcile.py`，
这个脚本留作手工交叉验证——它不依赖我们自己的任何代码，
所以当 runner 的数字可疑时，用它判断到底是谁错了。这是**两端，不是重复**。

**`benchmark-companion/` 已于 2026-09-21 移进 `archive/`，不再维护，也不删。**
它服务的是 **WorkBuddy** 的人工跑批，而 `runner/drivers/workbuddy.py` 这条
自动通路已经把那两个模式接上了同一套断言——
「四个模式里有一半数据质量低一档」这个缺口到此补上。
退场的三个前置条件全部实测通过：Web 执行链路（七-3.4）、
跨模式一键提交（七-3.3）、工具调用用例两个产品都 Pass（七-3.5）。
留着是因为它是**唯一不依赖我们自己代码**的那条路：
将来 CLI 换版本或驱动出问题时，它能回答「到底是产品坏了还是我们的工具坏了」——
和 `scripts/newapi_stats.ps1` 留下来的理由是同一个。
归档后适用八节那条：**不要再修它**。

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

   **结论（2026-09-21 二次更新）：能全自动，不需要人工。** 上面那些死路都不假，
   但**换个问法就通了**——不要去「新建」会话，而是**先让会话存在、再点开它**。

   ```
   API 发热身轮（全新 sessionKey） → 会话出现在侧边栏，标题 = 首条消息
   → CDP 点那条侧边栏条目        → UI 切过去，chatRoot 出现
   → API 发被测轮（同一 key）     → 界面实时渲染，探针采到完整 T1–T5
   ```

   **三轮连跑实测，零人工干预**，`history_turns` 恒为 2（就是热身那一轮），
   首字滞后 757 / 634 / 822 ms，自检全过。**每轮全新会话，互不污染。**
   代码在 `runner/client_probe/ui.py`（UI 驱动白名单）+ `probe.py::_auto_run`。

   ⚠️ **点「新建任务」确实没用，实测过**：它只把界面导航到一个**还不存在**的会话，
   `chatRoot=false`、`turns=0`，这时 API 往新 sessionKey 发过去
   **界面 turns 恒为 0，完全不渲染**。会话是首次发消息时才惰性创建的，
   所以必须先有会话再点它——顺序反了就不成立。

   ⚠️ 想省掉热身轮的路试过了：`/api/sessions/upsert-metadata` 两种 payload 形状
   （`key` / `sessionKey`）都是 **HTTP 400**，没继续反解。热身轮很便宜，先这样。

   半自动那条仍然保留（`probe_once(auto=False)`），因为它能往**任意既有会话**发，
   比如要量一个长历史会话（场景表 S7）时用得上。

   下面这段划范围的理由依然成立，只是现在不必靠它兜底了：
   Client Overhead 是**客户端的属性，不是 prompt 的属性**，
   量几轮就够，不需要跟基准跑批逐轮对齐。
   每轮隔离那条铁律是为了保证**模型/Agent 指标**干净，测 UI 渲染延迟不需要它。
   所以体验探针应当是**独立的小规模测量**，不要塞进主跑批链路。

   ⚠️ 附带的方法论坑，**但要分清毁掉的是哪个指标**（这一条当初也写过头了）：
   上下文越长首字越慢，所以**跨轮比较绝对后端耗时**在共享会话上确实不可比。
   但**滞后指标不受影响**——滞后 = UI 首字 − 后端首字，同一轮、同一上下文，
   上下文带来的变慢在两边同时出现，**相减时抵消**。
   所以共享一个舞台会话是成立的，`history_turns` 记成**协变量**而不是噪声
   （DOM 里挂的历史越多渲染越贵，那正是 S7 要测的东西）。

   ⚠️ POC 页里 T0 定义成「触发发送」，那是 UI 发起才有的时间点。
   走 API 发起要把 T0 重定义成 HTTP 请求发出时刻，时间点表跟着改。

### 第 2 批 · 客户端体验探针 —— **已结项 2026-09-21，不再投入**

> **结论：探针已完成，现有证据不足以排除真实用户路径的客户端瓶颈。**
> 本阶段确认了测量路径的边界，并取得有限范围的主线程阻塞观测。
> 保存的 21 轮记录中最长 Long Task 为 182ms，但 S4 波动达到协议的样本不足条件；
> P1 不适用、P3 定义失效，未满足预注册的整体判据。
> 完整数据见 `docs/client-probe-results.md`，判据见 `docs/client-probe-protocol.md`。
> 本次审查在看到数据后修正结论，不修改预注册阈值。
>
> | 已有观察 | 证据与边界 |
> |---|---|
> | 本批最长 Long Task 未超过 200ms | S3 最大 133ms，S4 为 79／88／182ms；S4 样本不足，且未证明 API 路径能覆盖流式路径的最坏情况 |
> | 两次 UI 首段内容时间接近 | 表格 1081ms、纯文本 1111ms；包含后端等待，不能据此判断全文渲染成本或结构影响 |
> | S7 与 S1 首字滞后中位数接近 | 1276ms vs 1329ms；指标混入后端剩余生成时间，不能排除历史长度的影响 |
> | 本次 UI 与 API 发起的呈现行为不同 | UI 对照观察到流式，API 批次仅 1～2 次文本更新；不能把 API 路径的等待外推为真实用户体验 |

**⛔ 明确决定不做：打字 / 新建任务那类 UI 自动化。**

想测「不同结构的渲染成本缩放」就得让 UI 发起一轮受控 prompt，而不打字只能三选二：

| | UI 发起（流式） | 任意受控 prompt | 干净上下文 |
|---|---|---|---|
| 点推荐词 + 发送 | ✅ | ❌ 只有固定几句 | ✅ |
| 点编辑 + 重发 | ✅ | ✅ | ❌ **追加不替换**，实测 turnCount 4，被测轮带着前一轮 1687 字答案跑 |
| 打字 + 发送 | ✅ | ✅ | ✅ |

**停止投入是项目优先级决定。** 当前保留探针和测量路径调查的产出，
不继续扩展 UI 自动化；真实用户路径的瓶颈、结构缩放和历史长度影响仍是未决问题。
1081ms 与 1111ms 的差值不能称为「缩放效应只有 30ms」：
它们是两次包含后端等待的首段内容时间，最终答案长度也不等于首段渲染量。
真人手动 843ms（n=1）与 CDP 点击数据同样不足以判断两种发起方式的差距。

⚠️ **UI 驱动白名单保持 1 个动作**（`open-session`）。编辑重发那条路**作废**，
它只存在过于一次性验证脚本，没进过仓库——别再捡回来。

⚠️ **P3 判据是当初写错的**，不是「没通过」：归一化完成滞后会变负
（UI 最后一次变化早于 SSE 终止），负数之间求倍率没意义。
如实记为「定义缺陷，未测」，**没有事后追认一个新指标再声称它通过**。

下面是历史记录，留作复盘和将来重启时的起点，**不代表还要继续做**。

---

设计与评审见 Notion「YonWork 客户端体验探针：CDP 渲染监测 POC」及其「9.21 评审补充」。
**定位：Core Benchmark 管 Agent/API 层，这条管「用户什么时候看到什么」，两者不合并。**

已替它验过（2026-09-21）：CDP 通，`Chrome/144.0.7559.236 Electron/40.9.1`，1 个 renderer target。
POC 清单前三项可划掉。API 侧的被减数现成：`first_delta_ms` 已在 `runs` 表里。

**两条贯穿全程的铁律，写在最前面因为它们会悄悄毁掉数据：**

- **时钟：绝不跨端相减。** 实测 Windows 比 WSL 快 **7.85s**（renderer 用 Windows 时钟，
  Python 探针用 WSL 时钟）。被测量级 0.5～3s，误差比它大一个量级且符号固定。
  T0 一律在 **renderer 内**打点，T0–T5 全在同一时钟；Host API 侧只取**时长**
  （`first_delta_seconds` / `duration_seconds`，与时钟无关）加到同一个 T0 上。
- **UI 驱动范围：只允许「点开一个会话」这一个动作。** 白名单写在
  `runner/client_probe/ui.py` 的 `ALLOWED_ACTIONS` 里，加动作要同时改那里和这里。
  不打字、不碰剪贴板、不选模型、不点发送，prompt 一律由 Host API 投递。
  「少量驱动 UI」不写死范围，就是当年 PAD 的起点。
  （原来写的是「点新建任务」，实测那个动作没用，见 1.1。）

  这**不是**八节禁的那种 UI 自动化：定位用 `data-testid` 语义选择器不是坐标，
  通道是 CDP 里调元素自己的 `.click()` 不是模拟系统输入。
  差别在改版之后——坐标会错位并**静默点错东西**，选择器找不到则**当场报错**。

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

2.4 ~~串起来~~ **已完成 2026-09-21，而且是全自动的**：API 发热身轮建会话 →
   CDP 点开它 → 探针在 renderer 内打 T0 → Host API 往这个会话发被测轮 →
   收 binding 事件 → 落 JSONL。**字段形状现在就对齐 `results.jsonl`**（`benchmark_id` 等），
   将来想入库直接复用 `ingest.py`，零返工。**暂不接 MySQL / Dashboard。**

2.5 ~~场景集 S1–S7~~ **已跑完 2026-09-21，21 轮，结果见 `docs/client-probe-results.md`。**
   **最重要的结论是个坏消息：`probe_once` 的 API 发起模式量不了用户感知延迟。**
   本次 UI 对照观察到 18 次 `text-update`、首段内容 1111ms；
   API 批次只有 **1～2 次**更新，首段内容在生成后期出现，
   首字滞后混入了后端剩余生成时间，不能直接当作渲染开销。
   所以 S3「空白 45 秒」**不能作为真实用户路径存在同样缺陷的证据**。
   这就是三节警告 `openclaw agent --json` 的同一类问题：测的不是用户真实链路。

   **P2 仅在已保存样本中未超阈值**：S3 最长 Long Task 为 133ms，S4 为 182ms。
   S4 三次值为 79／88／182ms，跨度 103ms 大于中位数 88ms，按协议应标样本不足。
   一次性呈现未被证明是流式路径的最坏情况，不能据此排除 renderer 瓶颈。
   S7 与 S1 的首字滞后中位数接近，也不能排除历史影响：该指标混入后端生成时间；
   `history_turns` 实际记 DOM 消息节点，S1 为 2，S7 为 12／14／16，三次历史在累积。

   ⚠️ **S5/S6 的 `tool_calls` 全是 0**，所以它们不能当「工具场景没问题」的证据。
   **2026-09-21 更正原因**：当时写的是「模型压根没调工具」，这个推论站不住——
   探针的 `tool_calls` 取自 `turn.tool_calls`（`probe.py:97`），而 YonWork 的
   SSE **根本不上报工具调用**（见七-3.5 实测）。0 只说明这条通路看不见，
   **既不能证明调了也不能证明没调**。结论（不能当证据）不变，理由换掉。
   要重测的话现在有办法了：`enrich()` 会从会话 JSONL 补齐。
   ⚠️ **P3 的定义是我写错了**：归一化完成滞后会变负（UI 最后一次变化早于 SSE 终止），
   负数之间求倍率没意义。如实记为「算不出来」，没有追认新指标再声称通过。

   原始要求（仍然有效，重测时照做）：

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
| 会话条目 | `[data-testid="sidebar-session-item"]`，内层 `[role="button"]` 才可点 |
| 当前打开的是哪条 | 条目上的 `data-nav-selected="true"`（**点完必须核对**，点歪了不报错） |
| 新建任务按钮 | `.yonclaw-sidebar-new-task`（**探针用不上**，见 1.1） |

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

~~**watch 模式的固有边界**：拿不到后端时长~~ **已推翻，2026-09-21 实测。**
`GET /api/events` 是**全局 SSE 流**，推送任何来源发起的轮次，带
`runId` / `sessionKey` / `state:"delta"` / `deltaText`。对照实测：
它给的首个 `deltaText` 在 **+2.805s**，我们自家 SSE 客户端测的也是 **2.805s**，
逐毫秒一致。**所以 UI 发起的轮次一样能算客户端开销**，
那个「固有盲区」只是没订阅这个端点而已。
时钟规矩不变：`/api/events` 只取**时长**，绝对时刻仍用 renderer 的 `performance.now()`。
另外按下发送键到气泡进 DOM 那一小段（Submit latency）**采不到**——
MutationObserver 看不到点击。T2=0.1ms 已说明这段没有可感知延迟，不值得为它越界去 hook 输入框。

### 第 3 批 · 主链路欠的账

3.1 **跑批时也采 ErrorCalls（原 §7-3）。已实现 2026-09-21。**
   YonWork 显式选择显示名 `newapi` 的模型时，逐轮采后台日志，再判定、落盘、入库。
   消费 + 错误计 APICalls，错误计 ErrorCalls；重试成功仍会因错误调用判 Fail。
   缺配置、请求失败、空日志继续标未采集，不能用空查询推断 APICalls=0。
   按本轮 UTC 时间窗 + 请求模型匹配，取 3 次查询中最后一次完整结果；仍需串行、无其它同令牌流量。
   默认模型和 WorkBuddy 不在这一路的覆盖范围。入库按来源保留计数，事后对账不覆盖
   已用于判定的逐轮证据。细节与验证见 `docs/error-calls-collection.md`。

   ⚠️ **新增盲区（2026-09-22 实测）：被网关在选通道之前拒掉的请求，
   `/api/log/self` 里一条都没有**——消费和错误记录都没有。
   实测一轮里 18 次 HTTP 503 `No available channel`，逐请求账本全记下了，
   CLI 自己 stdout 空白退出码 0，而后台日志是 0 条。
   所以**「后台错误日志为 0」不能推断「这一轮没有失败的请求」**。
   范围：只实测了 503 这一种拒绝、只查了我们实际在用的 `/api/log/self`。
   见 `docs/model-request-ledger.md`。

3.2 ~~抽 `Driver` 接口~~ **已完成 2026-09-21。** 跨模式一键编排也已完成，见 3.3。
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
   - ⚠️ **计时边界差异很大**：外层 **16.744s**，内部 `duration_ms` 只有 **3.087s**——
     每个 case 一个新进程，**两种计时相差 13.6s**，尚不能把差值全部归因于冷启动。
     **别拿它直接跟 YonWork 的常驻服务比耗时**，那不是同一回事。
     两个数都存着就是为了能把这一段拆出来。
   - ⚠️ **容器里的 Worker 跑不了它**：它要起 Windows 进程，而 compose 里的 worker
     既看不到 `/mnt/d` 也没有 WSL interop（已实测确认）。
     要从 Web 控制台跑 WorkBuddy，Worker 得在宿主机原生起——
     用 `./scripts/host_worker.sh`（见 3.4），别直接敲 `python -m runner.worker`，
     脚本还顺带查了代理、`.env` 和容器 Worker 是否还占着锁。
     控制台的产品选项上已经写了这个条件。

3.3 **跨模式一键编排。已完成 2026-09-21。**
   `/jobs/new` 现在收的是**一到多行模式**（产品 × 模型），一次提交建出 N 条任务，
   共用一个 `plan_id` 和**同一个实验名**。

   **支点是实验名，不是新写一个编排器**：`suite_id` 就是实验名的哈希，所以这组批次
   天然落进同一份报告，`/matrix/<suite_id>` 那张 Case × 模式表直接就是对比结果。
   **Worker、`batch.py`、驱动层一行没改**——Worker 本来就串行领任务，
   一个模式 = 一条任务 = 一个批次，正好是它已有的粒度。

   - 数据层：`benchmark_jobs` 加 `plan_id` / `plan_position` / `plan_label`
     （`job_store._ensure_plan_columns` 按需 ALTER，旧库自动补；`infra/schema.sql` 同步改）。
   - `create_plan()` 的 N 条 INSERT 在**同一个事务**里。半个计划比没有计划更糟：
     报告里缺的那一列和「那个模式全挂了」长得一模一样。
   - `/plans/<plan_id>` 看整组进度、一次停掉全部剩余模式。
     聚合的**只有进度不是判定**——哪个模式好由报告按断言说话。
   - 失败隔离抬到计划层：一个模式挂了只缺那一列，其余照跑。

   ⚠️ **`claim_next_job` / `active_job` 的排序里 `plan_position` 不能省。**
   同一计划的 N 条任务共用一个 `created_at`（刻意的，它们是一次提交），
   光按时间排全是并列，MySQL 挑哪条随缘 —— 表现为模式乱序执行。

   ⚠️ **同一个（产品 × 模型）组合选两次会被提交时拒绝。**
   `mode_summary` 按 product+model 分组合并批次，重复提交会让那一列轮次凭空翻倍
   且不报错，又是一个静默脏数据。

   ⚠️ **模式的 label 一律服务端从 product + model_query 推，不收客户端传的显示名。**
   落库的标签只能反映真正发出去的值；报告里的模型名来自实际响应（`model_mode`），
   两者对不上正是六-4 那个静默回落默认模型的信号，不能被一个好看的标签盖住。

   **实测（2026-09-21，重建镜像后走 Web）**：
   - 两模式计划（`yonwork/newapi` + `yonwork/默认`）一次提交跑完，两轮都 Pass，
     落进同一个 `suite_id`，`/matrix` 出两列 `yonwork / newapi`、`yonwork / default`。
   - 三模式计划排队时整组取消 → 三条全 Cancelled，顺序与提交一致。
   - 重复模式提交 → HTTP 400，库里一条都没留（事务回滚验证过）。

   **仍是单模式的：CLI（`python -m runner`）**。一次跑一个批次，没打算跟着改——
   编排属于控制台，CLI 的定位是单批次和 `--dry-run` 前置检查。

3.4 **WorkBuddy 的 Web 执行链路 + 跨产品横向对比。已完成 2026-09-21。**

   **宿主机 Worker**：`./scripts/host_worker.sh`。容器 Worker 没有 WSL interop、
   看不到 `/mnt/d`，**永远跑不了 WorkBuddy**；要做 YonWork × WorkBuddy 对比，
   两个产品必须由同一个 Worker 串行跑完，那就只能是宿主机这个。
   仍然只允许一个 Worker（MySQL 咨询锁），脚本会在容器 Worker 还活着时
   直接退出并告诉你敲哪条命令。
   ⚠️ 切过去之后**别用裸 `docker compose up -d`**——worker 是
   `restart: unless-stopped`，会被重新拉起来抢锁。只起 Web 用 `up -d web`。

   **报错指向真正的原因**：`WorkBuddyDriver.preflight` 第一件事是查 WSL interop
   （`/proc/sys/fs/binfmt_misc/WSLInterop*`，新内核叫 `-late`），不是查安装目录。
   顺序反了的话容器里报的是「安装目录不存在，设 `BENCH_WORKBUDDY_HOME`」，
   **把人引去设一个设了也没用的变量**。

   **`.env` 现在真的会被读到**：`BENCH_WORKBUDDY_*` 以前只读 `os.environ`，
   而容器靠 Compose 注入、宿主机 Worker 和 CLI 只有 `.env`——
   写在 `.env` 里的值**被静默忽略**，表现为「明明配了还是走默认路径」。
   已改成先环境变量后 `.env`（口径同 `NewApiConfig.load`）。

   **耗时拆成三个数**（`ChatTurn.engine_seconds` → `runs.engine_ms`）：
   外层 wall time 一律是 `duration_seconds`（耗时断言仍用它，最接近用户感受），
   `engine_seconds` 是**产品自报**的内部耗时；两者之差是未归因差值，不等于冷启动。
   YonWork 常驻服务没有每轮起进程这回事，这个字段**留空**，报告显示「不适用」
   而不是 0——填 0 会读成「冷启动为零」，那是结论不是事实。
   旧库靠 `ingest._ensure_engine_column` 按需 ALTER；**这一条不吞异常**
   （列缺了每条 INSERT 都会失败），和 `_widen_usage_source` 的处理刻意不同。

   **实测一轮（2026-09-21，宿主机 Worker，smoke × 1 轮，两个产品都用各自默认模型）**：

   | | 外层 | CLI 自报内部 | 外层减内部差值 | token | 实际模型 |
   |---|---|---|---|---|---|
   | yonwork | 3.26s | 不适用 | 不适用 | 16,325 | deepseek-v4-flash |
   | workbuddy | 8.26s | 4.92s | 3.34s | 3,460 | minimax-m3 |

   ⚠️ **这组数只证明链路通了，不能当性能结论**：每个模式 n=1；
   两边「默认模型」是**不同的模型**（deepseek-v4-flash vs minimax-m3），
   所以这是「产品默认配置」的对比，不是同模型对比；
   计时差值这次是 3.34s，而摸底那次是 13.6s——**不能用固定差值校正耗时**，
   要下结论得按 2.6 的规矩报中位数 + 最小/最大。

   **顺带抓到一个静默空列**：`workbuddy-cli` 早就在 `USAGE_SOURCES` 里，
   却没进 `mode_summary` / `matrix` 的 COALESCE，WorkBuddy 那行 token **整列是空的**，
   跟六-3 端上漏记长得一模一样。现已改为按来源独立汇总，并加行为回归
   （`web/tests/test_queries.py`：每个来源的数值、覆盖率和缺失语义），
   **接新产品时忘了改查询会当场红**，不用等跑完对比才发现。

3.5 **工具调用用例。已完成 2026-09-21。**

   `cases/catalog.yaml` 新增 `tools` 用例集；断言用 `Expectations.min_tool_calls`
   （不声明只记录，声明了才判，规矩同 `max_input_tokens`）。
   判不过要分清是谁的问题：工具开着模型没调 → **Fail**；
   我们自己把工具关了却跑要求用工具的 Case → **Invalid**。
   后者记 Fail 等于拿自己的配置错误去算产品的失败率（二-4）。

   **`tool-calls` 这条以前恒 PASS**，采了不判，和 ErrorCalls 当初一模一样。

   ⚠️ **两个产品各有一个「模型明明调了工具、我们却记成 0」的观测盲区**，
   都是跑起来才撞出来的，而且症状完全一样——**新断言会稳定误判成产品没调工具**：

   | 产品 | 盲区 | 真实形状 |
   |---|---|---|
   | YonWork | `/api/chat/send` 的 SSE **只有 text 块** | 会话 JSONL 里有 `{"type":"toolCall"}` + `toolResult` |
   | WorkBuddy | 只盯 assistant 消息的 content 块 | 工具调用是**顶层记录** `{"type":"function_call","name":"Bash"}` |

   YonWork 那条靠 Driver 协议新增的 `enrich()` 补：跑完从会话 JSONL 补齐，
   SSE 给了就以主通路为准，补不到照实留空。
   ⚠️ `batch` 对 `enrich` 的 `AttributeError` **不吞**——驱动少实现协议方法是
   编程错误，吞掉会让新驱动静默少一份原材料。

   **WorkBuddy 还有两个配置让工具用例永远跑不通**：
   - `--max-turns 1` 和工具天然冲突（调工具一轮、作答又一轮）。症状极难查：
     stdout 空白、`Max turns (1) exceeded` 只在 stderr、**退出码仍是 0**。
     已改成按 `allow_tools` 取值，并把 stderr 带进报错——原来只说
     「没有输出（退出码 0）」，CLI 写的原因全丢了。
   - `--permission-mode dontAsk` 的语义是「不问，**直接拒**」。headless `-p` 弹不了窗，
     所以开着工具用它，模型会老老实实回「Bash 工具被拒绝执行权限」，一次都调不成。

   ⚠️ **`bypassPermissions` 只跟显式的 `allow_tools` 走**（2026-09-21 与用户确认）。
   那是真放权：勾选的那一批跑批期间，模型可以在这台 Windows 上执行任意命令。
   默认关闭，前置检查里会打一行警告。用例 prompt 是自己写的、Git 版本化的，
   所以风险可控但不为零——**不要因为「方便」就把它设成默认**。

   **实测（Web 提交，tools × 1 轮，两个产品都开工具）**：

   | | 判定 | 工具 | 外层 | CLI 自报内部 | token | 实际模型 |
   |---|---|---|---|---|---|---|
   | yonwork | Pass | 1（Bash 类） | 10.31s | 不适用 | 33,752 | deepseek-v4-flash |
   | workbuddy | Pass | 1（`Bash`） | 36.38s | 33.93s | 25,683 | minimax-m3 |

   ⚠️ 同样是 n=1、默认模型不同，**只证明链路和断言通了**，不是性能结论。
   ⚠️ WorkBuddy 的「当前目录」是 `\\wsl.localhost\...` UNC 路径，
   CMD 不支持、会回落到 Windows 目录（stderr 里有警告）。这一轮它实际列的
   还是本仓库，但**跨产品比「当前目录」类用例前要先把工作目录定死**，
   否则两边问的不是同一个目录。这条没解决，只是记下来。

⚠️ **`web/tests` 以前不是真离线的**（2026-09-21 发现并修）：`_render` 一律调
`_active_job()` → `job_store.ensure_schema()`，本机 MySQL 恰好起着时它会**真的连库执行 DDL**。
跑一次 web 单测就把 `plan_*` 三列 ALTER 进了实验库才发现。库没起时被
`DatabaseError` 兜住返回 None，所以一直没人察觉，而且测试行为会随「容器开没开」变化。
已在 `WebApiTests.setUp` 里把 `_active_job` 打桩。**新增碰 `_render` 的测试别把这个桩去掉。**

### 下一项：整轮模型调用监控（2026-09-22，账本已实现，接入跑批待做）

两款产品共用逐请求账本，覆盖已验证路由上的主模型、工具续答、重试和子代理。
先验证入口与任务关联，再实现采集代理、驱动对账和报告；保留串行锁。
任务关联已成为本项的正确性前置条件，不再一概推迟到并发阶段。
具体待办和验收矩阵见 [整轮模型调用监控待办](docs/model-call-monitoring-todo.md)。
主模型真实转发 4/4 通过：YonWork 原生 `x-yonwork-run-id`、WorkBuddy 原生
`X-Conversation-ID` 可精确关联轮次；NewAPI 响应 `x-oneapi-request-id` 可关联后台
`request_id`，不必逐轮改账户或依赖时间窗。辅助模型/子代理仍待动态验证。
复现、临时账户同步问题和清理证据见 [入口验证](docs/model-entry-validation.md)。

**账本与采集代理已实现并接进跑批**（`runner/modelproxy/` + `batch.run_batch`，
36 项离线单测）。**默认关闭。两个产品的单次文本问答都已真实跑通一轮**
（2026-09-22，`BenchmarkId → 产品原生 run 头 → x-oneapi-request-id → NewAPI 后台`
全程精确关联，没用时间窗）。入库和 Web 报告还没做；工具续答/子代理/取消没跑。

⚠️ **WorkBuddy 拿 `models.json` 的 `id` 当发给网关的模型名**（实测两次，改 `name` 无效）。
所以它**不能**像 YonWork 那样新建一条同模型条目做直连/代理对照——
新 id 会被网关当成未知模型回 503。只能改现有那条的 url/apiKey，
备份在 `models.json.bak-collector`。
设计、归属策略和测量边界见 [逐请求账本](docs/model-request-ledger.md)。三件要内化的：

- **关联只认原生头全等**，命中不了就记 `unattributed`，**绝不按时间窗猜**。
  加产品改 `proxy.CORRELATION_HEADERS` 一处。
- **归属策略和隔离探针刻意相反**：探针拒可疑请求（要证明能隔离），
  采集代理**照常转发**未归属和迟到请求（不能影响被测对象）。
  子代理不带关联头时拒掉 = 我们打断了产品，还会把这次打断算成产品的失败。
  只有凭据不对和路径不认识才拒，记 `rejected`。
- **`upstream_attempts` 恒为 None**。一次客户端请求 ≠ 一次上游尝试，
  网关内部重试看不见，填 1 就是拿观测不到的东西冒充证据。

⚠️ `close_run()` 只改归属标记，**不表示「不会再有请求」**；迟到请求仍归旧轮。

⚠️ **代理跟着 Worker 进程内起，不做单独的 compose 服务**：独立服务挂掉会让产品的
模型调用全部失败，等于我们把被测对象弄坏了，那些失败还会被记成产品的失败。
所以入口端口必须**固定**（`BENCH_COLLECTOR_PORT`，默认 3312），产品配置里存的是 URL。
默认关闭；回退时**光设回 0 不够**，产品的 baseUrl 也得改回直连 NewAPI。

已入库并上报告：`model_requests` 表 + `runs.model_calls_status`，
`/run/<id>` 有逐请求时间线，`/suite/<id>` 有覆盖汇总。
⚠️ **`model_requests.benchmark_id` 允许 NULL，外键挂在 `batch_id` 上**——
未归属请求属于批次不属于任何一轮，硬塞给某一轮正是这套账本要消灭的东西。
⚠️ 页面把整轮耗时和请求耗时之和**并列但明说不能相减**，并真的去查请求有没有时间重叠。

⚠️ **`RunRecord.model_calls` 三态，别把 `disabled` 和 0 次调用混了**：
`disabled`（没开采集）/ `observed`（确实采到）/ `unavailable`（开着却一个请求都没经过）。
最后一态几乎一定是产品的 baseUrl 没指过来，记 0 就成了六-3 那种静默漏记。
**驱动不会自动改产品配置**，baseUrl 要手动指一次（端口固定就是为了只做一次）。

### 明确往后放的（写下来是为了不再反复捡起）

- **放开并发跑批（原 §7-5 的扩展方向）**：仍往后放。CLI / Worker 共用 MySQL 锁，
  但锁不能隔离外部流量；时间窗不等于精确关联。NewAPI 请求的任务关联在上述监控待办处理，
  device-api 的完整关联改造与并发支持另外评估。
- **`infra/schema.sql` 与 `job_store.py` 的双份表定义**：已验证逐字段一致。
  重复是必要的（schema.sql 只在数据目录为空时执行一次），交叉引用注释已加在
  `schema.sql` 的 `benchmark_jobs` 上方。**加列时两处都要改，再补一段按需 ALTER**，
  否则旧库不会自动获得新列（3.3 的 `plan_*` 就是这么处理的）。
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


## 测量正确性修订（2026-09-21）

- 工具补采区分完整/缺失/错误，不能把读不到日志算成产品没调工具。
- 耗时列改为外层、自报内部、差值；中位数 + 范围 + 有效样本数，暂不做冷启动或纯模型归因。
- token 按来源独立汇总并显示覆盖率，不混加；请求模式保持区分，不视为两种实际模型。
- CLI 实际跑批与 Worker 共用 MySQL 锁；外部流量仍需隔离。
- 历史判定不自动重写。实现、验收和下一批数据的条件见 `docs/measurement-correctness.md`。
