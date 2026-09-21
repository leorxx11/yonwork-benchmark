# 任务：摸清 WorkBuddy 桌面端的可编程入口

你运行在 Windows 上的 WSL2 里。目标应用 **WorkBuddy** 是一个 Windows 端 Electron 应用，
装在 `D:\WorkBuddy\`（WSL 路径 `/mnt/d/WorkBuddy/`），版本 `37.10.3-24`。

**只做只读调查和分析。不要修改 `D:\WorkBuddy\` 下的任何文件，不要往该目录安装任何东西，
不要改应用的配置或登录态。**

---

## 这个调查要回答什么决策

我在给 AI Agent 产品做基准测试自动化（批量跑 prompt，采耗时/token/成本，自动判定通过与否）。
同类产品 YonWork 已经打通：走它自带的本地 HTTP API，纯 Python + HTTP，不碰界面。

WorkBuddy 现在只有一条人工通路（人工复制提示词、粘贴、按热键计时），
**数据质量和 YonWork 那条不在一个等级上**。这次调查决定两件事：

1. WorkBuddy 能不能用同样的方式无人值守地跑批？
2. 如果只能走界面，那按既定原则这条路直接封死，改成「只对比 YonWork」。

所以我要的不是「有哪些功能」，而是**能不能非交互地跑完一轮对话，并拿到可判定的原材料**。

---

## 环境约束（先读，三个坑都踩过）

**坑 1 —— HTTP 代理会劫持 loopback 请求。**
shell 里有 `http_proxy`，且 `no_proxy` 用的 `127.*` 通配 curl 不认。
不绕过的话请求被代理吞掉返回空，**看起来像「服务没起来」，实际是假阴性**。
所有探测命令必须加 `--noproxy '*'`，或脚本开头 `export no_proxy='*' NO_PROXY='*'`。

**坑 2 —— 这台机器的 WSL2 是 mirrored 网络模式。**
`.wslconfig` 里 `networkingMode=Mirrored`，**WSL 的 `127.0.0.1` 就是 Windows 的 loopback**，
原生 curl/python 直连即可，不需要 `powershell.exe` interop。
（⚠️ 网上常见的「WSL 里 curl 127.0.0.1 必然连不上」那条说法在这台机器上**不成立**，
别照着它下结论。但列举 Windows 进程和监听端口仍然要用 interop，
因为 `ss` 只看得到 Linux 侧的 socket。）

**坑 3 —— WSL 的环境变量不会传给 Windows 进程。**
调 `node.exe` 等 Windows 程序时必须用 `WSLENV` 声明：
`WSLENV=FOO FOO=bar node.exe ...`，否则 `process.env.FOO` 是 undefined。
这一条最容易让人误判成「这个 CLI 不认环境变量」。

**读文件反过来**：走 `/mnt/d/WorkBuddy/` 原生读比 interop 快很多。

---

## 我已经摸到的（别重复劳动，从这里往下挖）

```
D:\WorkBuddy\
  WorkBuddy.exe                 主进程，当前有 5 个进程在跑
  version                       37.10.3-24
  resources/
    app.asar                    297 MB，20474 个文件
    app.asar.unpacked/
      cli/                      ★ 重点
        bin/
        dist/codebuddy.js           23 MB
        dist/codebuddy-headless.js  20 MB   ★★ 名字里带 headless
        dist/web-ui/                        ★  CLI 自带 web 界面
        package.json
        product.json / product.internal.json / product.cloudhosted.json
        product.selfhosted.json / product.ioa.json   ← 多套环境配置
        vendor/ripgrep、vendor/sandbox
      main/  native/  node_modules/  resources/
    vendor/  node.zip、python.zip、PortableGit.zip
  share-target/WorkBuddyShareTarget.exe
```

- 内核疑似腾讯 **CodeBuddy**（`node_modules/@tencent/...`、`qimei.dll`）。
  它有公开文档和公开的 CLI 用法，**可以直接查官方资料**，不必全靠逆向。
- 数据目录：`%APPDATA%\WorkBuddy`（WSL: `/mnt/c/Users/z2233/AppData/Roaming/WorkBuddy`），
  另有 `%LOCALAPPDATA%\@genieworkbuddy-desktop-updater`、`%LOCALAPPDATA%\CodeBuddyExtension`。
- **应用当前正在运行，且已经在监听三个 loopback 端口**（实测 `netstat`）：
  `127.0.0.1:11983`、`127.0.0.1:11986`（PID 7696）、`127.0.0.1:18488`（PID 6532）。
  端口号看着是随机分配的，**大概率每次启动都变，不要写死**。

---

## 必须回答的问题（按优先级，答不出来就明说答不出来）

### Q1（决定性）能不能非交互地跑完一轮完整对话？

至少验证到「发一句 prompt，拿到完整回答，进程正常退出」。三条候选路线：

- **A. CLI**：`cli/dist/codebuddy.js` 和 `codebuddy-headless.js`。
  用应用自带的 node 跑（`resources/vendor/node.zip` 或系统 node）。
  先找 `--help`。重点看有没有 `--print` / `--json` / 非交互 / 单次执行这类模式。
- **B. 本地 HTTP**：上面那三个端口分别是什么。有没有路由表可以从
  `codebuddy.js` / `main` 里 grep 出来。有没有类似 `/api/chat/send` 的对话入口。
- **C. CLI 自带的 web-ui**：`cli/dist/web-ui/` 说明 CLI 能起 HTTP 服务，
  那它背后多半有一套可直接调的接口。

**对每条路线都要说清楚：能不能用、怎么调、实测跑通没有、贴出实际的命令和输出。**
没跑通的不要写成「应该可以」。

### Q2 这三个端口是什么

分别属于哪个进程、做什么用、**怎么动态发现**（配置文件？日志？命令行参数？）。
YonWork 是把端口和 token 写进一个运行时 JSON 文件，WorkBuddy 有没有等价物。
**端口一律不要硬编码。**

### Q3 鉴权怎么做

有没有 token / 有没有校验 / 默认绑 `0.0.0.0` 还是 `127.0.0.1`。
**顺带确认一下有没有安全问题**：同网段能不能无凭据调用。
（YonWork 在这点上默认 `0.0.0.0` + 免鉴权，是个高危缺陷，我想知道 WorkBuddy 是不是同样。）

### Q4 做基准测试必须知道的四件事

这四件事直接决定数据能不能用，**任何一件答不出来，自动化就是不完整的**：

1. **怎么让每一轮之间完全隔离**——有没有 session / conversation 的概念，
   怎么保证第 N 轮看不见第 N-1 轮的上下文。
   （YonWork 这里踩过大坑：复用 sessionKey 会让基准数据**静默作废且不报错**。）
2. **有没有可以自己传入的幂等 id / run id**，能把「我发的这一轮」和
   「日志/产物/用量记录里的那一轮」精确关联上。
   如果没有，就只能按时间窗匹配，那样并发跑批必然张冠李戴。
3. **怎么判断一轮结束**——退出码？流式响应的终止事件？
   如果是 SSE/流，**哪些事件看着像结束其实不是**（YonWork 有两个这样的假终止信号，
   认错会把轮次提前截断）。
4. **token 用量从哪读**——有没有接口或日志文件给出 input/output/total token
   和实际使用的模型。**尤其要确认「实际跑的模型」能不能读到**
   （YonWork 存在指定模型失败却静默回落默认模型、HTTP 200 照常返回的情况）。

### Q5 能不能指定模型

能不能在调用时指定用哪个模型/供应商（我要对比默认模型和自建网关两种配置）。
如果能，**参数写错时会报错还是静默忽略**？这一条务必实测，别只看代码。

---

## 方法建议

- `app.asar` 有 297 MB，**别整个解包**。用带上下文的 grep 定位，需要时再抽单个文件。
  仓库里有个无需 Node 的工具：
  ```bash
  python3 scripts/extract_asar.py list /mnt/d/WorkBuddy/resources/app.asar --limit 40
  python3 scripts/extract_asar.py grep /mnt/d/WorkBuddy/resources/app.asar -p '/api/' -p 'listen('
  ```
  `cli/` 和 `main/` 已经在 `app.asar.unpacked/` 下解开了，可以直接读，不用走 asar。
- 先读 `cli/package.json` 的 `bin` 字段和 `product*.json`，那里通常直接写着入口和环境差异。
- CLI 优先跑 `--help` / `--version`，别上来就猜参数。
- **允许启动 CLI 进程做实验**，但不要杀掉正在跑的 `WorkBuddy.exe`，
  也不要触碰它的登录态和用户数据。
- 实在要看界面行为时可以用 CDP 只读观察，**但不要写任何 UI 自动化脚本**——
  那条路线已经被否决了，交上来也不会用。

---

## 交付物

一份 Markdown 报告，**结论先行**，包含：

1. **一句话结论**：WorkBuddy 能不能像 YonWork 那样纯 API 驱动跑批。能 / 不能 / 部分能。
2. Q1–Q5 逐条回答。**区分清楚哪些是实测验证过的，哪些是读代码推断的**——
   这两者要分开标注，推断的部分写明「未实测」。
3. 一个**最小可复现的跑批示例**：从零到跑完一轮，完整命令 + 真实输出。
   这是整份报告最重要的部分。
4. 一节「没查清楚的」：明确列出卡在哪、试过什么、为什么没继续。
   **不要用猜测填坑**，答不出来比编一个答案有用。
5. 如果发现安全问题（默认对外监听、免鉴权、凭据明文落盘等），单独一节列出来。

**报告里不要出现真实 token、accessToken、用户名、租户 id。**
一律替换成 `<REDACTED:格式说明>`，保留格式即可，我需要知道形状但不需要值。
