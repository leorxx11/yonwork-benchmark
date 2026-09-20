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

**坑 3 —— WSL 环境变量不会传给 Windows 进程**
调 `node.exe` 等 Windows 程序时必须用 `WSLENV` 声明：
`WSLENV=FOO FOO=bar node.exe ...`，否则 `process.env.FOO` 是 undefined。

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
4. **每轮必须用全新 `sessionKey`**（如 `agent:main:<benchmarkId>`）。复用会让第 N 轮看见
   第 N-1 轮的上下文，**基准数据静默作废且不报错**，这是最难查的一类污染。
   等价于 PAD 里每轮点「新建任务」。

**成本基线**：最小 prompt（`Reply with exactly: PONG`）的 `inputTokens` 实测 **20832**，
说明每轮约 20k 系统提示词底噪。
⚠️ 但这是**单次**实测，之后又测到 16,005 / 16,075 / 21,128——波动比阈值本身还大。
别拿单个常数当阈值，见七-4。

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
| `runner/` | 驱动 + 五层断言 + 入库，主链路 | 在用 |
| `web/` | 只读看板，FastAPI + Jinja + HTMX | 在用 |
| `infra/` | 结果库 MySQL 8.4（:3307），JSONL 可随时重放 | 在用 |
| `newapi/` | **被测对象**的模型网关（:3000），不是我们的基础设施 | 在用 |
| `cases/yonwork_benchmark.xlsx` | prompt 清单 | prompt 源保留，结果改 JSONL |
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

**`benchmark-companion/` 不是遗留**：它服务的是 **WorkBuddy**，不是 YonWork。
`runner/` 覆盖的是 YonWork 那两个模式，**WorkBuddy 那两个模式目前只有这条人工通路**。
四个模式里有一半的数据质量和另一半不在一个等级上，这是七-1 排第一的原因。

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

这四条比自动化工具本身更值钱，应走公司内部渠道上报（先确认是否为测试构建有意放宽）。
前两条是调查阶段查到的，后两条是搭 runner 的过程中撞出来的。

1. **高危：本地服务默认对局域网开放且免鉴权。**
   Host API 默认 `BIND=0.0.0.0`、`AUTH_MODE=trusted`（跳过全部 token 校验）；
   CDP TCP 代理默认 `0.0.0.0:9222`（应用日志自己写着「任何机器都可驱动本应用，风险极高」）。
   实测无任何凭据调 `/api/auth-runtime/session/status` 拿到完整登录态
   （accessToken、userId、tenantId、用户名）。叠加后 = 同网段任何人可无凭据调用全部 364 条路由。
   **本机缓解（不影响自动化，mirrored 模式下 127.0.0.1 照常可用）：**
   `YONCLAW_HOST_API_BIND=127.0.0.1`、`YONCLAW_HOST_API_AUTH_MODE=token`、`YONCLAW_CDP_PROXY_BIND=127.0.0.1`
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

⚠️ `docs/yonwork-automation-report.md` 的凭据**已于 2026-09-20 脱敏**
（host-api token / accessToken / gateway token / 用户名 → `<REDACTED:…>`）。
别往回填真实值。

---

## 七、待办（按优先级）

§7-2 的 `runner/` 骨架已完工：驱动 + 五层断言 + 三来源用量 + MySQL + 看板全部跑通，
99 个离线单测。链路命令见根目录 `README.md`。剩下的：

0. **关掉第六节-1 的两个默认暴露——这条一直没做。**
   2026-09-20 复查，`netstat` 显示 `0.0.0.0:3211` 和 `0.0.0.0:9222` 仍在 LISTEN（PID 6568）。
   这台是个人笔记本，接公司 WiFi 时同网段任何人都能无凭据调用全部 364 条路由、
   或通过 CDP 完全控制应用。**改完不影响自动化**，mirrored 模式下 127.0.0.1 照常可用。
1. **摸清 WorkBuddy 有没有可编程入口（CLI / HTTP）。** 建议这周花半天。
   现在 YonWork 两个模式走 `runner/` 全自动，WorkBuddy 两个模式只有
   `benchmark-companion/` 的人工热键通路。**不确认这件事，「四个模式」的对比就是不对等的**，
   一半数据带完整断言和三来源用量，另一半只有人工计时。
   它同时决定 P4 的工作量估算——如果只有 UI，那按八节的规矩这条路直接封死，
   得改成「只对比 YonWork 两个模式」并把理由写清楚。
2. **suite runner**：一条命令把所有模式跑完。需要先从 `batch.py` 里抽一层 `Driver` 接口，
   否则 WorkBuddy 接进来时会把 YonWork 的假设写死。
3. **跑批时也采 ErrorCalls。** 五节存活的那条断言（`ErrorCalls > 0` → `Fail`；
   `APICalls == 0` → `Invalid`）目前只有事后 `reconcile` 拉 NewAPI 才有数，
   跑批当时记的是「未采集」。**别用 0 冒充**——那等于把「没采到」说成「没出错」。
4. **成本阈值要重定。** 三-末那个 20832 底噪是单次实测，之后实测到
   16,005 / 16,075 / 21,128，波动比阈值本身还大，`input-token-jump` 会误报。
   要么按模式分别定基线，要么改成同模式历史分位数。
5. **device-api 和 newapi 两路改成按 runId 匹配**（现在是时间窗）。
   串行跑批没问题，**并发跑批前必须先解决**，否则会张冠李戴。
   会话 JSONL 那路走 `idempotencyKey`，不受影响。
6. **上报第六节的四条产品缺陷**，走公司内部渠道，先确认是否为测试构建有意放宽。

## 八、不要做的事

- 不要写 UI 自动化（pywinauto / PAD / 坐标点击）。
- 不要把断言逻辑写进驱动层。
- 不要修 `archive/` 里的任何东西，尤其是 `yonwork自动化.txt` 里的 PAD 问题，那个流程已废弃。
- 不要修改 `D:\yonwork\` 下的任何文件。
- 不要硬编码端口 3211 或任何 token。
