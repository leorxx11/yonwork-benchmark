# YonWork 桌面端可自动化入口 —— 调查报告

调查时间：2026-09-20　目标版本：YonWork 1.0.8（`app.asar` 内部 `package.json` name=`yonclaw`，openclaw 运行时 2026.6.11）
调查方式：只读。未修改 `D:\yonwork\` 下任何文件，未向该目录安装任何东西。

> **凭据已脱敏**（2026-09-20）。原文里的 host-api token、accessToken、gateway token、用户名
> 已替换成 `<REDACTED:…>`，保留了格式说明以便对照。真实值只在本机运行时文件里，
> 不进这个仓库。往回填真实值 = 把凭据写进版本库，别这么做。

**结论先行：Q1 成立，而且有三条独立的命令行/HTTP 通路，都已实测跑通完整对话。Q2/Q3 的优先级按你的规则下降，但两者也都查清了，且各自带出一个高危安全问题。**

---

## 0. 先修正一条环境前提（重要，影响你后续所有脚本）

> 你的原文：「WSL2 的 `127.0.0.1` 不是 Windows 的 loopback……在 WSL 里 `curl 127.0.0.1:<port>` **必然连不上，这是假阴性**」

**这条在这台机器上不成立。** 你的 WSL2 跑在 **mirrored 网络模式**下：

```
$ cat /mnt/c/Users/z2233/.wslconfig
[wsl2]
networkingMode=Mirrored
```

mirrored 模式下 WSL 与 Windows 共享网络命名空间，**WSL 的 `127.0.0.1` 就是 Windows 的 loopback**。实测：

```
$ curl -s --noproxy '*' -m 8 http://127.0.0.1:3211/healthz
{"success":true,"data":{"status":"ok","pid":6568,"port":3211,"startedAt":"..."}}

$ curl -s --noproxy '*' -m 8 http://127.0.0.1:9222/json/version
{ "Browser": "Chrome/144.0.7559.236", ... }
```

之前「连不上」的真正原因是**你 shell 里的 HTTP 代理环境变量把请求劫持了**：

```
http_proxy=http://127.0.0.1:7897   https_proxy=http://127.0.0.1:7897
```

`no_proxy` 里虽然写了 `127.*`、`10.*`，但 curl 的 `NO_PROXY` 不支持这种 `*` 通配写法，所以请求仍然走了 7897 代理，返回空。第一次探测时 curl 的 verbose 输出直接暴露了这点：

```
*   Trying 127.0.0.1:7897...
* Established connection to 127.0.0.1 (127.0.0.1 port 7897)
```

**行动项：所有探测命令加 `--noproxy '*'`（或在脚本里 `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY`）。** 加上之后不需要任何 interop，WSL 原生 curl 直连即可，比走 `powershell.exe` 快一个数量级。

> 注意：`10.70.242.1`（默认网关）在 mirrored 模式下是**你局域网的路由器**，不是 Windows 主机。不要往那个 IP 探测。

另一条 interop 相关的坑（实测）：**WSL 的环境变量默认不会传给 Windows 进程**，必须用 `WSLENV` 声明：

```
$ YONCLAW_TEST_VAR=hello node.exe -e "console.log(process.env.YONCLAW_TEST_VAR)"
undefined
$ WSLENV=YONCLAW_TEST_VAR YONCLAW_TEST_VAR=hello node.exe -e "console.log(process.env.YONCLAW_TEST_VAR)"
hello
```

---

## Q1：有没有命令行入口能跑完一次完整对话？

### 结论：有，而且有三条。全部**实测跑通**。

`yonworkctl` 是主进程 HTTP API 的**纯客户端**，自身不含业务逻辑。源码开头写得很清楚（`resources/cli/yonworkctl.mjs:2-11`）：

```js
/**
 * yonworkctl —— YonClaw 主进程 HTTP API 的命令行客户端
 * - CLI 是主进程的纯 HTTP 客户端，不做任何业务逻辑
 * - 通过约定路径的 host-api-runtime.json 发现主进程端口与 Bearer Token
 * - SSE 端点（chat send / events tail）逐行透传到 stdout
 */
```

### 1.1 子命令表

完整注册表在 `resources/cli/yonworkctl.mjs:225-331`，共 60+ 条，按域分组：

| 域 | 命令 | 映射的 HTTP 路由 |
|---|---|---|
| 系统 | `status` / `doctor` / `env` / `routes` | `GET /api/app/system-info`、`POST /api/app/openclaw-doctor`、`GET /api/app/runtime-env`、`GET /api/diagnostics/host-api-routes` |
| 日志 | `logs tail` / `logs dir` / `logs export` | `GET /api/logs` 等 |
| Gateway | `gateway status\|health\|start\|stop\|restart\|reload` | `/api/gateway/*` |
| Agent | `agents list\|create\|update\|delete\|load` | `/api/agents`、`/api/agent-market/*` |
| Skill | `skills list\|installed\|search\|install\|update\|uninstall\|load` | `/api/skills/*`、`/api/clawhub/*` |
| Cron | `cron list\|create\|update\|delete\|toggle\|trigger` | `/api/cron/*` |
| Provider | `providers list\|add\|update\|delete\|default\|set-default\|validate` | `/api/provider-accounts*` |
| **Chat** | **`chat send`（SSE）** / `chat abort` | **`POST /api/chat/send`** / `POST /api/chat/abort` |
| Session | `sessions list\|history\|delete\|rename` | `/api/sessions/*` |
| Channel | `channels ...`（16 条） | `/api/channels/*` |
| 设置/用量/认证 | `settings get\|set`、`usage history`、`auth status\|sync\|clear` | 同名路由 |
| 事件 | `events tail`（SSE 长连接） | `GET /api/events` |
| **万能透传** | **`api <path> [--method M] [--json J]`** | 任意 `/api/*` |

选项：`--json <json-string>`、`--method`、`--timeout-ms`（默认 30000）、`--agents`、`--force`、`--help`、`--version`。
退出码（`yonworkctl.mjs:20-29`）：`0` 成功 / `1` 内部错误 / `2` 参数错误 / `3` 鉴权失败 / `4` 未找到 / `5` 请求错误 / `6` 超时 / `7` 主进程未运行。

**有 `send` 类命令：`chat send`。** 这就是你要的那条。

### 1.2 它和桌面应用的关系

**不是独立进程，是 HTTP 客户端**，必须有正在运行的 YonWork 主进程。发现逻辑 `yonworkctl.mjs:43-62`：

1. 若设了 `YONCLAW_HOST_API_URL` → 直接用，跳过文件发现；
2. 否则读 `host-api-runtime.json`，校验 `pid` 存活，返回 `http://127.0.0.1:${port}` + `token`。

运行时文件实测内容：

```
$ cat "/mnt/c/Users/z2233/AppData/Roaming/yonwork/host-api-runtime.json"
{
  "port": 3211,
  "token": "<REDACTED:64 位十六进制>",
  "pid": 6568,
  "startedAt": "2026-09-20T12:26:13.937Z",
  "version": "1.0.8"
}
```

### 1.3 ⚠️ 一个会直接卡死你的 BUG：运行时文件路径不匹配

- **主进程写入** `%APPDATA%\yonwork\host-api-runtime.json`
  证据 —— `dist-electron/main/index-B7bMilCM.js`（偏移 ~8908895）：`function DH(){ ... const t=O.app.getPath("appData"), n=mk(t); return h.join(n,_G) }`，而 `function mk(e){return h.join(e,Cn)}`，`Cn = Cd.slug = "yonwork"`（偏移 ~2698）。
- **yonworkctl 读取** `%APPDATA%\yonclaw\host-api-runtime.json`
  证据 —— `resources/cli/yonworkctl.mjs:68-70`：`if (env.APPDATA) dirs.push(join(env.APPDATA, 'yonclaw'));`

实测两边确实不匹配：

```
$ ls .../AppData/Roaming/yonwork/host-api-runtime.json
-rwxrwxrwx ... 177 Sep 20 20:26 .../yonwork/host-api-runtime.json      ← 存在
$ ls .../AppData/Roaming/yonclaw/host-api-runtime.json
ls: cannot access ...: No such file or directory                        ← 不存在
```

日志也印证：

```
[2026-09-20T12:26:13.938Z] [INFO ] [host-api] runtime info persisted to
  C:\Users\z2233\AppData\Roaming\yonwork\host-api-runtime.json (pid=6568)
```

**后果：在这个构建上裸跑 `yonworkctl <任何命令>` 永远返回「YonWork is not running」+ 退出码 7，哪怕应用正开着。** 实测：

```
$ node.exe ./yonworkctl.mjs status
[yonworkctl] YonWork is not running. Start the application first.
EXIT=7
```

**两个绕法，都实测有效**（注意必须配 `WSLENV`）：

```bash
# 绕法 A：指定用户数据目录
WSLENV=YONCLAW_USER_DATA_DIR \
YONCLAW_USER_DATA_DIR='C:\Users\z2233\AppData\Roaming\yonwork' \
  node.exe ./yonworkctl.mjs status
# → {"platform":"win32","buildInfo":{"appVersion":"1.0.8",...}}  EXIT=0

# 绕法 B：直接给 URL，完全跳过文件发现（推荐）
WSLENV=YONCLAW_HOST_API_URL \
YONCLAW_HOST_API_URL='http://127.0.0.1:3211' \
  node.exe ./yonworkctl.mjs agents list
# → {"success":true,"agents":[{"id":"main","name":"YonWork","isDefault":true,...}]}
```

### 1.4 鉴权：**默认根本不校验**

`index-B7bMilCM.js`（偏移 ~8908537）：

```js
function zKe(){return(process.env.YONCLAW_HOST_API_BIND||"").trim()||"0.0.0.0"}
function EKe(){return(process.env.YONCLAW_HOST_API_AUTH_MODE||"trusted").trim().toLowerCase()==="token"?"token":"trusted"}
```

请求处理里（同文件，偏移 ~8910xxx）：

```js
const u=yKe(o,c),                       // 提取 token
      f=n==="trusted"||u===Wa();        // trusted 模式 → 恒为 true
if(!xKe(o,c)&&!DKe(o,c)&&!f){ ...401... }
```

即 **`AUTH_MODE` 默认 `trusted`，所有路由跳过 token 校验**。应用自己在启动日志里就警告了：

```
[host-api] ⚠️ 当前为 trusted 免鉴权模式（本分支默认；可用 YONCLAW_HOST_API_AUTH_MODE=token 恢复），
           所有接口跳过 token 校验，仅应在受信任的本地 / 容器环境使用。
```

**实测确认（不带任何 token）：**

```
$ curl -s --noproxy '*' http://127.0.0.1:3211/api/diagnostics/host-api-routes -w "HTTP=%{http_code}"
HTTP=200   ← 无 token 直接 200
```

所以：**token 从 `host-api-runtime.json` 读，但当前构建下你根本不需要它。** 脚本里带上 `Authorization: Bearer <token>` 也无害，将来若切到 `token` 模式仍能工作，建议带着。

### 1.5 `/api/chat/send` 的请求/响应契约（实测）

路由实现在 `dist-electron/main/gateway-ne0Qzful.js`（偏移 ~182257）。

**请求体字段**（从 handler 读取的字段整理）：`sessionKey`、`message`、`deliver`、`language`、`modelSelection`、`attachments`、`idempotencyKey`、`clientMessageId`、`source`。

> **坑：`idempotencyKey` 是事实上的必填项。** 幂等去重函数无条件对它调 `.trim()`：
> ```js
> function nn(e,t){return `${e.trim()}\0${t.trim()}`}
> function ic(e,t){const s=nn(e,t), ...}
> function F(e){if(ic(e.sessionKey,e.idempotencyKey))throw new rc}
> ```
> 不传就 500。实测复现：
> ```
> {"success":false,"error":"TypeError: Cannot read properties of undefined (reading 'trim')"}
> ```
> 补上 `idempotencyKey` 后立即正常。**另外：`runId` 会直接取你传的 `idempotencyKey`**，所以给它一个有意义的唯一值（如 `bench-<case>-<ts>`），跑批时天然可追溯。

**SSE 事件序列**（实测原始输出）：

```
: connected
: yonclaw-sandbox-chat-stream-marker=yonclaw-sandbox-chat-stream-marker-20260709

event: chat.run-id
data: {"runId":"bench-probe-1789907358","result":{"runId":"...","status":"started"}}

event: chat.message
data: {"message":{"runId":"...","sessionKey":"agent:main:bench-probe-1","seq":2,
       "state":"delta","deltaText":"P","message":{"role":"assistant",...}}}

event: chat.message
data: {... "seq":3,"state":"delta","deltaText":"ONG" ...}

event: chat.message
data: {... "seq":6,"state":"final","stopReason":"stop",
       "message":{"role":"assistant","content":[{"type":"text","text":"PONG"}]}}

event: chat.complete
data: {"runId":"bench-probe-1789907358"}
```

**跑批时怎么判「这一轮结束了」：** 认 `event: chat.complete`（流随即被服务端关闭），或认 `state:"final"` 的那条 `chat.message`——它带完整答案文本。终止判定逻辑 `xc()`（`gateway-ne0Qzful.js` 偏移 ~123622）：`phase ∈ {completed,finalized,failed}` 或 `state ∈ {completed,finalized,final,error}` 或 `stopReason` 非 `tooluse` 即为终止；`stream==="compaction"` 和 `stopReason==="tooluse"` **不算**终止（这两个是跑批时最容易误判的坑）。

**端到端耗时实测：** 裸 curl 6s、`yonworkctl chat send` 3s（同一 prompt 量级，模型 `yonyou-default/deepseek-v4-flash`）。

### 1.6 `openclaw` 是什么，和 `yonworkctl` 怎么分工

**是两个完全不同层级的东西：**

| | `yonworkctl` | `openclaw` |
|---|---|---|
| 是什么 | YonWork **桌面应用**的 HTTP 客户端 | 底层 **Agent 运行时**本体的 CLI（OpenClaw 2026.6.11，`Multi-channel AI gateway`） |
| 代码 | `resources/cli/yonworkctl.mjs`（31KB 明文） | `resources/openclaw/openclaw.mjs` + `dist/`（完整 npm 包） |
| 依赖桌面应用 | **必须**在跑 | **不必须**（可 `--local` 嵌入式直跑） |
| 版本 | 1.0.0 | 2026.6.11 (e085fa1) |

`openclaw` 是 YonWork 内嵌的引擎——桌面应用 fork 出的 Gateway 进程跑的就是它。它自带**完整文档**在 `resources/openclaw/docs/`（`cli/` 下 50+ 页，`automation/` 下 13 页），强烈建议直接读。

跑完整一轮对话的命令是 `openclaw agent`：

```
Usage: openclaw agent [options]
Run an agent turn via the Gateway (use --local for embedded)
  --agent <id>          Agent id (overrides routing bindings)
  -m, --message <text>  Message body for the agent
  --message-file <path> Read the agent message body from a UTF-8 file
  --model <id>          Model override for this run
  --thinking <level>    off|minimal|low|medium|high|xhigh|adaptive|max
  --timeout <seconds>   Override agent command timeout (default 600)
  --json                Output result as JSON
  --local               Run the embedded agent locally
  --session-key <key>   Explicit session key (agent:<id>:<key>)
```

文档明确写了它是给脚本用的（`docs/cli/agent.md`）：
> `--json` keeps stdout reserved for the JSON response. Gateway, plugin, and embedded-fallback diagnostics are routed to stderr so scripts can parse stdout directly.

**实测跑通**（配置指向应用的 profile 目录）：

```
$ OPENCLAW_CONFIG_DIR=<profile>\userData\runtime\openclaw \
  node.exe openclaw.mjs agent --agent main --message "Reply with exactly: CLI-OK" --json
...
  "stopReason": "stop",
  "executionTrace": { "winnerProvider":"yonyou-default","winnerModel":"deepseek-v4-flash",
                      "fallbackUsed":false,"runner":"embedded" },
  "requestShaping": { "authMode":"auth-profile","thinking":"off" },
  "transport": "embedded", "fallbackFrom": "gateway"
[agent] run 5a1a5fa6-... ended with stopReason=stop
elapsed=35s
```

两点要注意：

1. **它没连上正在跑的 Gateway，回落到了 embedded**（`"transport":"embedded","fallbackFrom":"gateway"`），所以耗时 35s 而不是 3s。原因未深究——大概率是 Gateway 端口/token 归属于应用进程。
2. **配置是 profile 隔离的，不在 `~/.openclaw`。** 应用真正用的配置在：
   ```
   C:\Users\z2233\AppData\Roaming\yonwork\profiles\profile-<sha256>\userData\runtime\openclaw\openclaw.json
   ```
   `~/.openclaw` 下只有 `state/openclaw.sqlite` 和 `completions/`，**没有 `openclaw.json`**。不指 `OPENCLAW_CONFIG_DIR` 的话，`openclaw` 会用一份空配置，看不到你的 agent 和 provider。

   该文件我做了改动前后 md5 比对，**确认未被修改**（`697a81b1d10a1850dde81d6ce3f2111a` 前后一致）。

---

## Q2：`external-cdp` 的行为

### 结论：默认启动**确实自己开 CDP**，端口**固定 9222**，而且额外把它**暴露到 `0.0.0.0`**。

### 2.1 源码（`dist-electron/main/index-B7bMilCM.js`，偏移 8740623–8742000）

```js
const Ike = 9222;                                          // 默认端口，硬编码
function Kee(){ return process.env.YONCLAW_CDP_ENABLED !== "0" }   // 默认开启
function zD(){                                             // 从 argv 取端口
  for(const e of process.argv){
    const t=/^--remote-debugging-port=(\d+)$/.exec(e);
    if(t){ const n=Number.parseInt(t[1],10); if(Number.isFinite(n)&&n>=0) return n }
  }
  return null
}
function Vee(){                                            // 端口解析优先级
  const e=zD();
  return e!==null&&e>0 ? e : Cke(process.env.YONCLAW_CDP_PORT, Ike)
}
function jke(){
  if(!Kee()){ d.info("[external-cdp] disabled by YONCLAW_CDP_ENABLED=0"); return }
  if(zD()!==null&&zD()>0){ d.info("[external-cdp] fixed CDP port provided via argv; skipping appendSwitch"); return }
  const e=Vee();
  O.app.commandLine.appendSwitch("remote-debugging-port", String(e));   // ← 自己加
  d.info("[external-cdp] CDP remote-debugging-port set",{port:e})
}
function Ske(){                                            // 额外的 TCP 代理
  if(!Kee()) return Promise.resolve(null);
  const e=Vee(),
        t=(process.env.YONCLAW_CDP_PROXY_BIND||"").trim()||"0.0.0.0";   // ← 默认 0.0.0.0
  const n=Pl.createServer(s=>{ const i=Pl.connect(e,"127.0.0.1"); s.pipe(i).pipe(s); ... });
  n.listen(e,t,()=>{ d.warn(`[external-cdp] ⚠️ CDP TCP proxy listening on ${t}:${e} -> 127.0.0.1:${e}（对可达网络开放，任何机器都可驱动本应用，风险极高）`) })
}
```

**你的推断正确并已证实：不带 argv 时它自己 `appendSwitch` 加 CDP 端口。**

### 2.2 端口固定还是随机？**固定 9222。**

`Ike = 9222` 硬编码；`DevToolsActivePort` 文件也会写（`%APPDATA%\yonwork\DevToolsActivePort`），实测内容：

```
9222
/devtools/browser/290c9c78-6005-4acd-8e96-242d6050b149
```

> ⚠️ 实测发现该文件里的 **browser UUID 是过期的**（当前实例是 `9a5b71d1-...`，文件里还是上一次运行的 `290c9c78-...`）。**端口是可靠的，UUID 不可靠**。跑批时请用 `/json/version` 取实时 `webSocketDebuggerUrl`，不要读这个文件的第二行。

### 2.3 控制开关（源码整理）

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `YONCLAW_CDP_ENABLED` | 设 `0` 关闭 CDP（连同代理） | 开启 |
| `YONCLAW_CDP_PORT` | 覆盖端口 | 9222 |
| `YONCLAW_CDP_PROXY_BIND` | TCP 代理绑定地址 | **`0.0.0.0`** |
| `--remote-debugging-port=N`（argv） | 最高优先级，且跳过 appendSwitch | — |

另外全量枚举了 `YONCLAW_*` 命名空间，**共 130+ 个**（`grep -o "YONCLAW_[A-Z0-9_]*"`）。与自动化最相关的：
`YONCLAW_HOST_API_PORT` / `YONCLAW_HOST_API_BIND` / `YONCLAW_HOST_API_TOKEN` / `YONCLAW_HOST_API_AUTH_MODE` / `YONCLAW_USER_DATA_DIR` / `YONCLAW_HEADLESS` / `YONCLAW_DEVTOOLS` / `YONCLAW_GATEWAY_INSPECT_PORT` / `YONCLAW_REQ_PROXY_PORT` / `YONCLAW_TLS_MODE` / `YONCLAW_DISABLE_ALL_CHANNELS` / `YONCLAW_BLOCK_DELETE_COMMANDS`。
（`YONCLAW_HEADLESS` 看着对跑批很有用，但我**没有验证**它的实际效果。）

### 2.4 实测验证（裸启动，不带任何参数/环境变量）

启动：`powershell.exe -NoProfile -Command "Start-Process -FilePath 'D:\yonwork\YonWork.exe'"`

**各进程 LISTEN 端口：**

```
YonWork PIDs: 6528,6568,9020,11272,15224,19340,23616,27108

LocalAddress LocalPort OwningProcess
------------ --------- -------------
0.0.0.0           3211          6568      ← Host API
0.0.0.0           9222          6568      ← CDP TCP 代理
127.0.0.1         9222          6568      ← Chromium CDP 本体
127.0.0.1        12576          6568      ← YonClaw Req Proxy
```

**`/json/version`：**

```
$ curl -s --noproxy '*' http://127.0.0.1:9222/json/version
{
   "Browser": "Chrome/144.0.7559.236",
   "Protocol-Version": "1.3",
   "User-Agent": "... YonWork/1.0.8 Chrome/144.0.7559.236 Electron/40.9.1 ...",
   "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/9a5b71d1-085f-4886-8d36-d467308596be"
}
```

**`/json/list` —— 有 `"type":"page"` 的 target：**

```
2 targets
  type=webview      title='文库'    url=https://c1.yonyoucloud.com/yonbip-ec-file/ucf-wh/file/index.
  type=page         title='YonWork' url=file:///D:/yonwork/resources/app.asar/dist/index.html
```

**应用自己的启动日志（直接证据，最近 5 次启动全都一样）：**

```
[2026-09-20T12:26:13.938Z] [WARN ] [external-cdp] ⚠️ CDP TCP proxy listening on
  0.0.0.0:9222 -> 127.0.0.1:9222（对可达网络开放，任何机器都可驱动本应用，风险极高）
```

> 日志里查不到 `[external-cdp] CDP remote-debugging-port set` 这行——因为 `appendSwitch` 必须在 `app.whenReady()` 之前执行，那时文件日志器还没初始化。**但「裸启动后 127.0.0.1:9222 确实在 LISTEN」这个实测事实已经充分证明了默认走的就是 appendSwitch 分支。**

### 2.5 ⚠️ 安全问题（两个，都不是我造成的，是默认配置）

1. **CDP 代理默认监听 `0.0.0.0:9222`** —— 局域网内任何机器都能通过 CDP 完全控制这个应用（执行任意 JS、读取全部会话）。应用自己的日志把这句话叫「风险极高」。
2. **Host API 默认监听 `0.0.0.0:3211` 且是 `trusted` 免鉴权模式** —— 组合起来就是：**局域网内任何人无需任何凭据即可调用全部 364 条 `/api/*` 路由**。实测无 token 调 `/api/auth-runtime/session/status`，直接拿到了完整登录态：

   ```
   {"success":true,"hasSession":true,"snapshot":{
     "accessToken":"<REDACTED:UUID + yonclaw__ 后缀>",
     "userId":"<REDACTED:UUID>","tenantId":"<REDACTED>",
     "user":{"userName":"<REDACTED:真实姓名>"}, "expiresAt":1791203175 }}
   ```

   **建议无论是否采用本报告的方案，都先把这两个关掉**：设 `YONCLAW_HOST_API_BIND=127.0.0.1`、`YONCLAW_HOST_API_AUTH_MODE=token`、`YONCLAW_CDP_PROXY_BIND=127.0.0.1`。在 mirrored 模式下 WSL 仍可用 `127.0.0.1` 访问，**不影响你的自动化**。

---

## Q3：有没有本地 HTTP 服务

### 结论：有 4 个本地监听服务。`resources/gateway` 本身**不是**服务。

**先澄清 `resources/gateway/`：** 里面只有 6 个文件，全是 `memory-fs` 模块加载器（`archive.mjs` / `cjs-hook.mjs` / `loader.mjs` / `register.mjs` / `zip-reader.mjs` / `zip-reader.d.mts`）——用来从 zip 里直接 require 模块的，**和 HTTP 无关**。真正的 Gateway 是 OpenClaw fork 出的独立 Node 进程。

| # | 服务 | 监听 | 鉴权 | 用途 |
|---|---|---|---|---|
| 1 | **Host API** | `0.0.0.0:3211` | **默认无**（trusted） | 主力：364 条 `/api/*`，含 `chat/send`、`sessions`、`usage`、`events` |
| 2 | **OpenClaw Gateway** | `127.0.0.1:29179` | token `yonclaw-a477...` | JSON-RPC (`/rpc`) + WebSocket (`/ws`)，Agent 运行时 |
| 3 | **Chromium CDP** | `127.0.0.1:9222` + `0.0.0.0:9222` | 无 | 浏览器级自动化 |
| 4 | **YonClaw Req Proxy** | `127.0.0.1:12576`（随机） | 未查 | 出站请求代理，有 HTML 页面 |

端口常量来源（`index-B7bMilCM.js` 偏移 ~35293 / ~41523）：

```js
const tue=18789,        // 旧版 gateway 端口
      p0=29179,         // gateway 默认端口
      v8="127.0.0.1", wI="/ws", gI="/rpc";
const xs={ CLAWX_DEV:5173, CLAWX_GUI:23333,
           CLAWX_HOST_API: s9("YONCLAW_HOST_API_PORT","CLAWX_PORT_CLAWX_HOST_API",3211),
           OPENCLAW_GATEWAY: s9("OPENCLAW_GATEWAY_PORT","CLAWX_PORT_OPENCLAW_GATEWAY",p0) };
```

Gateway 实测状态：

```
$ curl -s --noproxy '*' http://127.0.0.1:3211/api/gateway/status
{"state":"running","port":29179,"reconnectAttempts":0,"degraded":false,"pid":11172,"uptime":0}
$ curl -s --noproxy '*' http://127.0.0.1:3211/api/gateway/health
{"ok":true,"uptime":334}
```

> Gateway 的 `/rpc` 我用 `{"method":"status"}` 试了一下返回 `404 Not Found`——**它的 JSON-RPC 报文格式和鉴权头我没查清，暂列为未知。** 反正 Host API 已经够用，没继续深挖。

### 对跑基准测试特别有用的两个端点（实测）

**`GET /api/usage/recent-token-history`** —— 每轮的 token 数与成本，跑批算分直接用：

```json
[{"timestamp":"2026-09-20T12:29:37.687Z","sessionId":"349610a9-...","agentId":"main",
  "model":"deepseek-v4-flash","provider":"yonyou-default","content":"OK-42",
  "inputTokens":20832,"outputTokens":35,"cacheReadTokens":256,"cacheWriteTokens":0,
  "totalTokens":21123,"costUsd":0}, ...]
```

**`GET /api/events`** —— 全局事件 SSE 长连接，可做跑批期间的旁路监控。实测能建连，空闲 4s 内无事件（符合预期）。

---

## 2. 可复现命令清单

### 前置（每个脚本开头都放）

```bash
# 关键：绕开 shell 里的 HTTP 代理，否则请求会被 127.0.0.1:7897 劫持
export no_proxy='*' NO_PROXY='*'
# 或逐条 curl 加 --noproxy '*'

BASE=http://127.0.0.1:3211                      # mirrored 模式下 WSL 直连有效
NODE=/mnt/d/yonwork/resources/bin/node.exe      # 应用自带 node v22.22.2
CLI=/mnt/d/yonwork/resources/cli/yonworkctl.mjs
TOKEN=$(python3 -c "import json;print(json.load(open('/mnt/c/Users/z2233/AppData/Roaming/yonwork/host-api-runtime.json'))['token'])")
```

### 进程与端口（Windows 侧，走 interop）

```bash
# 确认有无残留进程（带参启动前必做）
powershell.exe -NoProfile -Command "Get-Process YonWork -ErrorAction SilentlyContinue | Select-Object Id,ProcessName"

# 列出 YonWork 各进程的 LISTEN 端口
powershell.exe -NoProfile -Command "
\$pids = (Get-Process YonWork -ErrorAction SilentlyContinue).Id
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { \$pids -contains \$_.OwningProcess } |
  Select-Object LocalAddress,LocalPort,OwningProcess | Sort-Object LocalPort | Format-Table -AutoSize"

# 裸启动
powershell.exe -NoProfile -Command "Start-Process -FilePath 'D:\yonwork\YonWork.exe'"
# 优雅退出
powershell.exe -NoProfile -Command "Get-Process YonWork -ErrorAction SilentlyContinue | Stop-Process"
```

### 健康探测

```bash
curl -s --noproxy '*' $BASE/healthz
curl -s --noproxy '*' $BASE/api/gateway/health
curl -s --noproxy '*' http://127.0.0.1:9222/json/version
curl -s --noproxy '*' http://127.0.0.1:9222/json/list
```

### 跑一轮完整对话（主力，推荐）

```bash
SK="agent:main:bench-$(date +%s)"
IDK="bench-case001-$(date +%s)"

curl -sN --noproxy '*' -X POST "$BASE/api/chat/send" \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"sessionKey\":\"$SK\",\"message\":\"你的 prompt\",\"deliver\":false,
       \"idempotencyKey\":\"$IDK\",\"clientMessageId\":\"cm-1\"}"
```

### 解析 SSE，取最终答案（跑批骨架）

```bash
python3 - "$SK" "你的 prompt" <<'PY'
import json, sys, urllib.request
sk, msg = sys.argv[1], sys.argv[2]
body = json.dumps({"sessionKey": sk, "message": msg, "deliver": False,
                   "idempotencyKey": sk, "clientMessageId": "cm-1"}).encode()
req = urllib.request.Request("http://127.0.0.1:3211/api/chat/send", data=body,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
ev, final = None, None
for raw in urllib.request.urlopen(req, timeout=600):
    line = raw.decode("utf-8", "ignore").rstrip("\r\n")
    if line.startswith("event:"): ev = line[6:].strip()
    elif line.startswith("data:"):
        d = json.loads(line[5:].strip())
        if ev == "chat.message" and d.get("message", {}).get("state") == "final":
            final = "".join(c.get("text", "")
                            for c in d["message"]["message"]["content"] if c.get("type") == "text")
        elif ev == "chat.complete":
            print(json.dumps({"runId": d.get("runId"), "answer": final}, ensure_ascii=False)); break
        elif ev == "chat.error":
            print(json.dumps({"error": d}, ensure_ascii=False)); break
PY
```

### 会话与用量

```bash
curl -s --noproxy '*' $BASE/api/sessions/list-metadata
curl -s --noproxy '*' $BASE/api/usage/recent-token-history
curl -s --noproxy '*' -X POST $BASE/api/sessions/delete \
     -H 'Content-Type: application/json' -d "{\"sessionKey\":\"$SK\"}"
```

### yonworkctl（记得 WSLENV）

```bash
WSLENV=YONCLAW_HOST_API_URL YONCLAW_HOST_API_URL="$BASE" $NODE $CLI status
WSLENV=YONCLAW_HOST_API_URL YONCLAW_HOST_API_URL="$BASE" $NODE $CLI agents list
WSLENV=YONCLAW_HOST_API_URL YONCLAW_HOST_API_URL="$BASE" $NODE $CLI routes       # 全量路由表
WSLENV=YONCLAW_HOST_API_URL YONCLAW_HOST_API_URL="$BASE" $NODE $CLI api /api/usage/recent-token-history
WSLENV=YONCLAW_HOST_API_URL YONCLAW_HOST_API_URL="$BASE" $NODE $CLI chat send \
  --json "{\"sessionKey\":\"$SK\",\"message\":\"hi\",\"idempotencyKey\":\"$IDK\"}"
```

### 只读分析工具（本次用到的）

```bash
python3 scripts/extract_asar.py list    /mnt/d/yonwork/resources/app.asar --limit 40
python3 scripts/extract_asar.py extract /mnt/d/yonwork/resources/app.asar -o ./yonwork-asar
python3 ctx.py 'external-cdp' yonwork-asar/dist-electron/main/*.js -c 400 -m 12   # 压缩包 grep 带上下文
grep -oh '"/api/[a-zA-Z0-9/_-]*"' yonwork-asar/dist-electron/main/*.js | tr -d '"' | sort -u
```

---

## 3. 推荐路线（按稳定性排序）

### 路线 A ★★★★★ —— 直接打 Host API `/api/chat/send`（**强烈推荐**）

**做法：** WSL 里原生 curl/python 直连 `http://127.0.0.1:3211`，解析 SSE 到 `chat.complete`。

**前提：** ① YonWork 主进程在跑；② 已登录（`/api/auth-runtime/session/status` → `hasSession:true`）；③ 脚本绕开 http_proxy；④ 请求体必带 `idempotencyKey`。

**优点：** 完全绕开 UI；无 Node/interop 依赖，纯 HTTP；延迟最低（实测 3–6s/轮）；`runId` 可由你指定，天然可追溯；配套 `usage history` 直接给 token 和成本；有 `chat abort` 可打断。

**风险：**
- 依赖 `trusted` 免鉴权默认值。**建议现在就把 token 带上**（`Authorization: Bearer`），这样即便将来改成 `token` 模式脚本也不用动。
- 端口 3211 被占用时会**回落到随机端口**（源码：`s.listen(0,r)`）。脚本应从 `host-api-runtime.json` 读 `port`，别硬编码。
- SSE 终止判定别搞错：`stream==="compaction"` 和 `stopReason==="tooluse"` 都**不是**终止。

### 路线 B ★★★★☆ —— `yonworkctl chat send`

**做法：** 同上，但走官方 CLI。

**前提：** 必须设 `YONCLAW_HOST_API_URL`（或 `YONCLAW_USER_DATA_DIR`）**并配 `WSLENV`**，否则因 1.3 的路径 BUG 永远退出码 7。

**优点：** 官方支持的接口，版本升级时语义更稳；退出码规范清晰，适合 CI 判成败；`api` 子命令可透传任意路由。

**风险：** 比路线 A 多一层 Node 启动开销；受那个路径 BUG 影响，环境变量配置是必需品而非可选项；stdout 是原始 SSE，仍得自己解析。

### 路线 C ★★★☆☆ —— `openclaw agent --json`

**做法：** `OPENCLAW_CONFIG_DIR` 指向 profile 的 `runtime/openclaw`，跑 `openclaw agent --agent main -m "..." --json`。

**优点：** 唯一**不需要桌面应用在跑**的路线；`--json` 输出结构化，含 `executionTrace`（哪个 provider/model 胜出、是否 fallback）——做基准测试这个信息很有价值；有 `--model` / `--thinking` / `--timeout` 可直接控制变量。

**风险：**
- 实测它**没连上运行中的 Gateway，回落到 embedded**，35s vs 3s。要么查清 Gateway 连接（我没查），要么接受 embedded 的性能特征。
- **测的不是同一个东西**：embedded 跑的是独立 agent 进程，和用户在 UI 里的真实链路不完全等价。如果基准测试的目标是「产品表现」，这条会失真；如果目标是「模型/agent 能力」，反而更干净。
- 必须显式指 `OPENCLAW_CONFIG_DIR`，否则用空配置。
- openclaw CLI 有可能改写配置（本次实测**未改**，md5 一致，但不保证所有子命令都如此）。

### 路线 D ★★☆☆☆ —— CDP 驱动 UI（Playwright `connectOverCDP`）

**做法：** 连 `http://127.0.0.1:9222`，取 `type=page` 的 target 操作界面。

**优点：** 默认就开着，零配置；是唯一能验证**真实 UI 渲染**的路线；比 Power Automate Desktop 稳得多（选择器基于 DOM 而非坐标/图像）。

**风险：** 仍然是 UI 自动化，前端改版就坏；比 A/B 慢一个数量级；`DevToolsActivePort` 文件里的 UUID 会过期，必须走 `/json/version` 取实时地址。

**建议：只在需要验证 UI 本身时用，不要用它跑对话基准。**

### 综合建议

**主力用 A，CI 判定用 B 的退出码，UI 回归单独用 D。** C 作为「脱离桌面应用跑模型基准」的补充通道，但要先把 Gateway 连接问题查清，否则测的不是同一条链路。

无论选哪条，**先把 2.5 的两个默认暴露关掉**——在 mirrored 模式下不影响你的自动化。

---

## 4. 没查出来 / 不确定的

明确说不知道的部分：

1. **OpenClaw Gateway 的 `/rpc` 协议细节。** `POST /rpc {"method":"status","params":{}}` 返回 404。正确的 JSON-RPC 报文格式、鉴权头字段名（token 值形如 `yonclaw-<32 位十六进制>`，实际值见本机运行时文件）我没查清。
2. **路线 C 为什么回落到 embedded。** 只看到结果 `"fallbackFrom":"gateway"`，没定位失败原因（端口归属？token？单实例？）。
3. **`YONCLAW_HEADLESS` 的实际效果。** 名字很诱人（跑批不弹窗），但我没测。
4. **`127.0.0.1:12576` 的 Req Proxy 有哪些端点。** 只确认了它返回一个标题为 "YonClaw Req Proxy" 的 HTML 页面。
5. **单实例锁丢弃 argv 的行为。** 采信你的实测结论，本次没有复现（会需要第二次启动）。
6. **`0.0.0.0:9222` 代理在 Windows 上为何能与 `127.0.0.1:9222` 共存而不 EADDRINUSE。** 实测确实两个都在 LISTEN，但没深究 Windows socket 语义。
7. **`/api/chat/send` 的完整字段语义。** `modelSelection`、`attachments`、`source`、`language` 只确认了字段名存在于 handler，**没有逐个验证取值格式**。`deliver:false` 已验证（不外发渠道）。
8. **多 agent 场景。** 当前环境只有一个 agent（`main`），多 agent 下的 `sessionKey` 路由没测。

---

## 附：本次调查产生的文件

都在 `/home/leorxx/code/test/`，**没有任何一个写进 `D:\yonwork\`**：

| 文件 | 说明 |
|---|---|
| `yonwork-asar/` | `app.asar` 解包结果，151MB / 10226 文件。后续排查还会用到，不用可直接删 |
| `ctx.py` | 我写的小工具：在压缩过的单行 bundle 里 grep 并打印字符级上下文（`grep -n` 对 minified 代码没用） |
| `yonwork-automation-report.md` | 本报告 |

**当前状态：YonWork 仍在运行中**（PID 6568 等，本次只启动了一次，未反复重启）。我创建的两个测试会话 `agent:main:bench-probe-1/2` 已通过 `/api/sessions/delete` 删除。路线 C 的 `openclaw agent` 另外产生了一次 embedded run（run id `5a1a5fa6-...`），它不在应用会话列表里。需要我关掉应用的话说一声。
