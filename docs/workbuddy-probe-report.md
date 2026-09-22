# WorkBuddy 桌面端可编程入口摸底报告

调查时间：2026-09-21（Asia/Shanghai）
目标安装：`D:\WorkBuddy`；安装版本标记 `37.10.3-24`；桌面包 `5.5.6`；内置 CLI `2.137.1`

标记约定：

- **[实测]**：在这台机器、这个安装版本上实际执行并看到结果。
- **[代码]**：读取本机已安装包源码所得，未必做了端到端调用。
- **[官方]**：腾讯当前公开文档；若与本机构建冲突，以本机实测为准。
- **[未实测]**：明确保留项，不用推测补齐。

> 报告不含真实 token、accessToken、用户名、租户 ID。命令中的 `<WINDOWS_USER>`、
> `<REDACTED:...>` 都是占位符。调查没有修改 `D:\WorkBuddy`，也没有停止原有桌面进程。

## 一句话结论

**能无人值守跑批，但最佳入口不是桌面当前监听的三个端口，而是每个 case 启动一次内置 headless CLI：它能无 UI 地完成整轮对话并返回答案、内外部耗时、input/output token、请求模型和实际模型。**

若把“像 YonWork”严格限定为“复用桌面常驻 HTTP API”，结论则是**部分能**：另起的 `codebuddy --serve` 确实能走 REST/ACP 完成对话，但本版本 REST SSE 不返回 token、实际模型或单轮耗时，并存在订阅竞态；不适合作为当前基准主链路。

### 路线决策

| 路线 | 完整对话 | 完整回答 | token / 实际模型 / 耗时 | 会话隔离与关联 | 结论 |
|---|---:|---:|---:|---:|---|
| A. 内置 CLI `-p --output-format json` | **[实测] 是** | **[实测] 是** | **[实测] 是** | **[实测] 可控** | **主路线** |
| B. 桌面现有 11983 / 11986 / 18488 | **[实测+代码] 否** | 否 | 否 | 不适用 | 都不是聊天 API |
| C. `--serve` 的 Web UI / REST / ACP | **[实测] 是** | **[实测] 是** | REST **[实测] 不完整** | **[实测] 可关联、不可幂等** | 可作备选，不作计量主路线 |

建议 runner 契约：**一个 case = 一个新进程 + 一个全新 session id + `--no-session-persistence`**。不要用 `--continue`、`--resume`，也不要复用 session id。

---

## 最小可复现跑批示例（主路线）

### 1. 从 WSL2 调 WorkBuddy 自带运行时

下面是本次跑通命令的可复制脱敏版。只需替换 `WB_WINDOWS_USER`；应在 Windows 可见目录下启动，以避免 Windows `cmd.exe` 对 WSL UNC 当前目录的警告。

```bash
WB_WINDOWS_USER='replace-me'
WB_CONFIG_DIR="/mnt/c/Users/${WB_WINDOWS_USER}/.workbuddy"
WB_RUN_ID='bench-smoke-20260921-001'   # 实际跑批每次生成全新 UUID

cd "/mnt/c/Users/${WB_WINDOWS_USER}"

WSLENV='ELECTRON_RUN_AS_NODE:CODEBUDDY_CONFIG_DIR/p:WORKBUDDY_CONFIG_DIR/p:ACC_PRODUCT_CONFIG_PATH/p:WORKBUDDY_DATA_FOLDER_NAME:CODEBUDDY_INTERNET_ENVIRONMENT:CODEBUDDY_HOST:CODEBUDDY_FORCE_HEADLESS_BUNDLE:CODEBUDDY_DISABLE_COMPILE_CACHE:DISABLE_AUTOUPDATER:CODEBUDDY_DISABLE_GIT_REPO_SCAN:CODEBUDDY_DISABLE_PROMPT_SUGGESTION:CODEBUDDY_DISABLE_CRON:CODEBUDDY_DISABLE_AUTO_MEMORY:CODEBUDDY_DISABLE_SYSTEM_REMINDER:CODEBUDDY_DISABLE_GIT_BASH:CODEBUDDY_DISABLE_API_KEY_HELPER' \
ELECTRON_RUN_AS_NODE=1 \
CODEBUDDY_CONFIG_DIR="$WB_CONFIG_DIR" \
WORKBUDDY_CONFIG_DIR="$WB_CONFIG_DIR" \
ACC_PRODUCT_CONFIG_PATH="$WB_CONFIG_DIR/cache/acc-product-config-v3.json" \
WORKBUDDY_DATA_FOLDER_NAME='.workbuddy' \
CODEBUDDY_INTERNET_ENVIRONMENT='internal' \
CODEBUDDY_HOST='workbuddy-desktop' \
CODEBUDDY_FORCE_HEADLESS_BUNDLE=1 \
CODEBUDDY_DISABLE_COMPILE_CACHE=1 \
DISABLE_AUTOUPDATER=1 \
CODEBUDDY_DISABLE_GIT_REPO_SCAN=1 \
CODEBUDDY_DISABLE_PROMPT_SUGGESTION=1 \
CODEBUDDY_DISABLE_CRON=1 \
CODEBUDDY_DISABLE_AUTO_MEMORY=1 \
CODEBUDDY_DISABLE_SYSTEM_REMINDER=1 \
CODEBUDDY_DISABLE_GIT_BASH=1 \
CODEBUDDY_DISABLE_API_KEY_HELPER=1 \
/mnt/d/WorkBuddy/WorkBuddy.exe \
  'D:\WorkBuddy\resources\app.asar.unpacked\cli\bin\codebuddy' \
  -p \
  --output-format json \
  --no-session-persistence \
  --setting-sources user \
  --strict-mcp-config \
  --no-plugin-management \
  --permission-mode dontAsk \
  --tools '' \
  --max-turns 1 \
  --model default \
  --session-id "$WB_RUN_ID" \
  'Reply with exactly: PONG'
```

为什么需要这段环境：`WorkBuddy.exe` 通过 `ELECTRON_RUN_AS_NODE=1` 充当安装包自带的 Node 运行时；两个 config dir 和产品配置路径让 CLI 读取 WorkBuddy 当前账号与产品模型配置，而不是另建 Linux `~/.codebuddy` 环境。由于目标是 Windows 进程，变量必须列入 `WSLENV`；路径变量带 `/p`。

`--tools ''` 适合纯文本基准，避免模型自行读写文件或执行命令。若某个 case 明确评测工具能力，应为该 case 单独配置允许工具和沙箱，而不是全局放开。

### 2. 真实输出

以下是上述 `default` 实测输出中的 assistant 与最终 result；仅省略用户消息、随机 message UUID、trace id 和时间戳，数值未改：

```json
[
  {
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "content": [
      { "type": "output_text", "text": "PONG" }
    ],
    "providerData": {
      "requestModelId": "default",
      "model": "deepseek-v4.1-flash",
      "rawUsage": {
        "prompt_tokens": 3548,
        "completion_tokens": 2,
        "total_tokens": 3550,
        "completion_thinking_tokens": 0,
        "credit": 0.1
      }
    }
  },
  {
    "type": "result",
    "subtype": "success",
    "is_error": false,
    "result": "PONG",
    "session_id": "wbprobe-default-20260921t1040",
    "duration_ms": 2088,
    "duration_api_ms": 2087,
    "num_turns": 2,
    "total_cost_usd": 0,
    "usage": {
      "input_tokens": 3548,
      "output_tokens": 2,
      "cache_creation_input_tokens": 3548,
      "cache_read_input_tokens": 0,
      "cache_creation": null,
      "server_tool_use": null,
      "service_tier": null
    },
    "permission_denials": []
  }
]
```

进程退出码为 `0`，完整 stdout 是一个 JSON 数组。

### 3. runner 必须采用的成功判定

不要只看退出码。至少同时满足：

1. stdout 能解析为 JSON；
2. 最后存在 `type == "result"` 的记录；
3. `subtype == "success"` 且 `is_error == false`；
4. `session_id` 等于本次自己生成的 run id；
5. 至少一个 assistant `message` 为 `status == "completed"`；
6. 记录 `providerData.requestModelId` 与 `providerData.model`，并按实际模型判定；
7. 外层另设 wall-clock timeout，并同时记录外层耗时、`duration_ms`、`duration_api_ms`。

`total_cost_usd` 在本构建代码中固定为 `0`，**不能当真实费用**。当前 WorkBuddy 账号返回的 `rawUsage.credit` 可记录为产品积分，但它不是美元金额；自建 NewAPI 的货币成本应以网关计费记录为准。

---

## Q1：能否非交互跑完一轮完整对话？

### A. 内置 headless CLI：能，完整跑通

**[实测]** `resources/app.asar.unpacked/cli/package.json` 注册了 `codebuddy`、`cbc` 等 bin；`bin/codebuddy` 在 `-p/--print`、输入输出格式、ACP、serve 等模式下进入 `codebuddy-headless.js`。

帮助中确认的关键参数：

```text
-p, --print
--output-format <text|json|stream-json>
--input-format <text|stream-json>
--include-partial-messages
--model <model>
--session-id <id>
--no-session-persistence
--max-turns <n>
--serve --host <host> --port <port> --auth <password|none>
--acp [stdio|streamable-http]
```

默认模型与显式模型都实际完成了 PONG：

| 请求 | 回答 | 请求模型 | 实际模型 | input / output | credit | 内部耗时 |
|---|---|---|---|---:|---:|---:|
| `--model default` | `PONG` | `default` | `deepseek-v4.1-flash` | 3548 / 2 | 0.10 | 2088 ms |
| `--model glm-5.3-flash` | `PONG` | `glm-5.3-flash` | `glm-5.3-flash` | 3256 / 29 | 0.04 | 2901 ms |

第二次结果还报告了 `reasoning_tokens=24`。`num_turns` 分别为 2 和 3，所以该字段不能解释为“用户问了几轮”；它更接近内部事件/代理轮数。

腾讯官方也把 `codebuddy -p` 定义为“查询后退出”，并正式记录 JSON、stream-json、model、max-turns 和 no-session-persistence 等参数：
[CLI 命令参考](https://cloud.tencent.com/document/product/1831/137026)、[无头模式](https://cloud.tencent.com/document/product/1831/137040)。

### B. 当前桌面三个 HTTP 端口：不能对话

**[实测]** 当前监听快照：

```text
127.0.0.1:11983  LISTENING  PID 7696
127.0.0.1:11986  LISTENING  PID 7696
127.0.0.1:18488  LISTENING  PID 6532
```

进程关系：PID 6532 是桌面主进程；PID 7696 是其子进程，命令行为：

```text
D:\WorkBuddy\WorkBuddy.exe D:\WorkBuddy\resources\app.asar\main\daemon-app-server-entry.js --stdio
```

实际请求：

```bash
curl --noproxy '*' -i http://127.0.0.1:18488/workbuddy/probe
```

```http
HTTP/1.1 200 OK
Access-Control-Allow-Origin: *
Cache-Control: no-store
Content-Type: application/json; charset=utf-8

{"ok":true,"app":"workbuddy-desktop","version":"5.5.6","platform":"win32"}
```

```bash
curl --noproxy '*' -i http://127.0.0.1:11983/healthz
curl --noproxy '*' -i http://127.0.0.1:11983/
```

两者均返回：

```http
HTTP/1.1 404 Not Found
```

**[代码]** 11983 的路由是 MCP Apps HTML、静态资源、sandbox preview 和地图代理；11986 只处理连接器 OAuth 的 `GET /oauth/callback`；18488 只处理 `GET/OPTIONS /workbuddy/probe`，其他请求 404。三者都没有 chat/send、ACP 或模型执行入口。

11986 没有带伪造 `code/state` 做动态请求，因为那会触碰 OAuth 状态；用途由本机构建的请求处理器和启动日志确认。

### C. CLI Web UI / `--serve`：能对话，但计量不完整

**[实测]** 另启了一个仅本次调查使用的实例：随机端口、`127.0.0.1`、临时 password、`--no-session-persistence`。它在运行时注册文件中报告 endpoint，随后完成 REST 对话：

实际启动参数如下；前面的 WorkBuddy/WSLENV 配置前缀与“最小可复现跑批示例”相同，另把两个 gateway 变量加入 `WSLENV`。密码必须换成临时高熵值：

```bash
# 在上文完整 WSLENV 值末尾追加：
# :CODEBUDDY_GATEWAY_AUTH:CODEBUDDY_GATEWAY_PASSWORD
# 同时保留上文其余环境变量赋值，然后把 CLI 参数尾部替换为：
CODEBUDDY_GATEWAY_AUTH='password' \
CODEBUDDY_GATEWAY_PASSWORD='<REDACTED:临时高熵密码>' \
/mnt/d/WorkBuddy/WorkBuddy.exe \
  'D:\WorkBuddy\resources\app.asar.unpacked\cli\bin\codebuddy' \
  --serve --host 127.0.0.1 --port 0 --auth password \
  --no-session-persistence --setting-sources user \
  --strict-mcp-config --no-plugin-management \
  --permission-mode dontAsk --tools '' --max-turns 1 \
  --model default --session-id '<UNIQUE_SERVE_SESSION_ID>'
```

端口从 `.workbuddy/sessions/<pid>.json` 读取，不解析 banner，也不写死。

```bash
curl --noproxy '*' -X POST 'http://127.0.0.1:<PORT>/api/v1/runs' \
  -H 'X-CodeBuddy-Request: 1' \
  -H 'Authorization: Bearer <REDACTED:临时测试密码>' \
  -H 'Content-Type: application/json' \
  --data '{
    "id":"wb-http-message-20260921t1055",
    "type":"message",
    "source":{
      "platform":"generic",
      "sender":{"id":"benchmark-probe","name":"Benchmark Probe"},
      "conversation":{"id":"wb-http-conversation-20260921t1055","type":"direct"}
    },
    "payload":{"text":"Reply with exactly: HTTP-PONG-3"}
  }'
```

真实响应（UUID 脱敏）：

```json
{"data":{"runId":"<REDACTED:UUID>","status":"accepted"}}
```

紧接着订阅 `/api/v1/runs/<RUN_ID>/stream`：

```text
event: message
data: {"version":"1.0","replyTo":"wb-http-message-20260921t1055","status":"completed","content":{"markdown":"HTTP-PONG-3"},"agent":{"sessionId":"<REDACTED:UUID>","toolCalls":[]}}

event: done
data: {}
```

因此 Web UI 背后确有可直接调用的 HTTP API；它不是另一套模型链路。官方将 REST `/api/v1/*` 和 ACP `/api/v1/acp` 都列为公开 Beta 接口：[HTTP API (Beta)](https://cloud.tencent.com/document/product/1831/137064)、[Web UI](https://cloud.tencent.com/document/product/1831/137050)。

但当前安装版本有三项决定性不足：

- SSE 的 completed 事件没有 token、实际模型或单轮耗时；实测 `/api/v1/stats/session` 返回 `"tokenUsageByModel": {}`、`"apiDuration": 0`。
- POST 后若没有立即订阅，任务完成即删除内存流；第一次晚约数秒订阅得到 `404 RUN_NOT_FOUND: Run not found or already completed`。这是不可回放竞态。
- client message `id` 只在输出中作为 `replyTo`；重复提交同一个 id 仍得到新的 runId，见 Q4。

**ACP 状态：部分实测。** 已实测 connect、initialize、SSE JSON-RPC，确认有 `session/new`、`session/prompt`、`session/cancel`、`session/set_model` 等方法；未在桌面托管、已认证 sidecar 上再做一次完整 prompt。REST 已足够证明 C 路线能对话，但 ACP 是否能在这个部署形态下稳定提供完整 usage，仍属未验证。

---

## Q2：三个端口分别是什么，如何动态发现？

| 当时端口 | 进程 | 用途 | 分配方式 | 动态发现 |
|---|---|---|---|---|
| 18488 | 桌面主进程 | 网页检测 WorkBuddy 是否安装/运行 | 固定候选表 `18488,18489,18490`，顺位占用 | 依次 GET `/workbuddy/probe`，校验 JSON；也可读 main log |
| 11983 | daemon 子进程 | MCP Apps HTML / 静态资源 / sandbox preview | `listen(0, "127.0.0.1")` 随机端口 | daemon log 的 `McpAppsHost ... {port}`；内部 RPC 可取，未发现公共 runtime JSON |
| 11986 | daemon 子进程 | Connector OAuth loopback callback | `listen(0, "127.0.0.1")` 随机端口 | daemon log 的 `ConnectorOAuthManager ... listening`；未发现公共 runtime JSON |

所以原先“18488 大概率随机”的判断需要修正：它是固定三端口候选段；11983、11986 才是随机端口。

### 与 YonWork runtime JSON 最接近的机制

**[实测+代码]** `codebuddy --serve` / daemon 会写：

```text
%USERPROFILE%\.workbuddy\sessions\<pid>.json
```

形状如下，不含密码：

```json
{
  "pid": 12292,
  "sessionId": "wbprobe-http-20260921t1055",
  "kind": "interactive",
  "url": "http://127.0.0.1:<EPHEMERAL_PORT>",
  "endpoint": "http://127.0.0.1:<EPHEMERAL_PORT>",
  "mode": "local",
  "version": "2.137.1",
  "lastHeartbeat": 1789959116098
}
```

它只适用于 CLI serve/worker，不记录 11983/11986，也不携带 desktop sidecar 的 Bearer secret。发现算法必须：

1. 枚举 `sessions/*.json`；
2. 校验 PID 当前仍存在且命令行确为对应 CLI；
3. 校验 heartbeat 新鲜；
4. 实际访问 health；
5. 失败则当作 stale，绝不能只信文件。

本机原先就有一个 PID 已不存在的旧注册文件；本次测试进程在强制停止后也留下了注册文件，进一步证明 stale 是正常异常路径。本次新建的那一份已按精确路径清理，原有旧记录未动。

### 数据目录修正

实际 WorkBuddy runtime 根目录是：

```text
C:\Users\<WINDOWS_USER>\.workbuddy
```

Electron user-data 是其下的 `.workbuddy\app`。不是任务原文猜测的 `%APPDATA%\WorkBuddy`。模型、sessions、logs、SQLite、connectors 等也都以 `.workbuddy` 为根。

---

## Q3：鉴权、绑定地址与安全边界

### 当前三个桌面端口

**[实测]** 三个端口全部只监听 `127.0.0.1`，没有 `0.0.0.0` 或局域网地址。因此不存在 YonWork 那种“默认全网卡 + 免鉴权”的同网段直接调用问题。

- 18488 无鉴权且 CORS `*`，但只返回 app、版本和平台探活元数据。
- 11983 只承载 MCP App 内容；敏感静态代理另有随机 secret/实例路径。不是通用业务 RPC。
- 11986 只接受 `GET /oauth/callback`，检查 Host、Origin、路径，并依赖随机 OAuth state/PKCE 流程。

### `--serve` / Web UI API

**[实测]** 独立 serve 使用 password 模式时：

- 默认 host 为 `127.0.0.1`；
- `/api/v1/*` 接受 `Authorization: Bearer <password>` 或登录 Cookie；
- 本构建普通 API 请求还必须带固定头 `X-CodeBuddy-Request: 1`，缺失时实测 `403 Missing required header`；
- 临时 password 鉴权下 health 实测 200；
- `--auth none` 会明确打印高风险警告。

官方安全文档同样说明默认 password、Bearer/Cookie、自定义请求头和 CORS：[安全概述](https://cloud.tencent.com/document/product/1831/137056)。手工指定 `--host 0.0.0.0` 时官方文档称会强制 password；该非默认绑定未在本机实测。

**[代码] 桌面托管 sidecar 比普通 serve 更严格：** WorkBuddy 进程内生成 32 字节随机值并做 base64url 编码，向子进程注入 `CODEBUDDY_GATEWAY_AUTH=password` 和 `CODEBUDDY_GATEWAY_PASSWORD`，同时关闭 API docs。secret 只在父子进程内存中，不写 settings 或 runtime JSON。这是源码注释所称的 `CNVD-ZC-2026-6234` 修复。

### 仍需注意的本机边界

**[代码，未做利用测试]** 桌面源码明确说明：

- ACP 主通道 `/api/v1/acp` 对 loopback 请求免 Bearer；
- `/internal/*` 不经过 REST `requireAuth`，供 CLI 子进程和 stdio MCP 使用；
- `X-CodeBuddy-Request: 1` 是抗浏览器 CSRF 的固定标志，不是秘密。

所以其安全模型是“阻止局域网和普通跨站网页”，不是“隔离同一 Windows 用户下的恶意原生进程”。能发现随机端口的同机进程仍可能触达 ACP/internal 能力。该风险低于 `0.0.0.0` 免鉴权，但对本机恶意进程不能算强边界。

---

## Q4：基准测试的四个关键问题

### 4.1 每轮如何完全隔离？

#### 推荐 CLI 路线

**[实测]** 每个 case：

- 新启动一个 WorkBuddy headless 进程；
- 生成从未用过的 `--session-id`；
- 加 `--no-session-persistence`；
- 不传 `--continue`、`--resume`；
- 测纯文本时 `--tools '' --strict-mcp-config --no-plugin-management`；
- 进程结束后丢弃全部内存态。

两次独立 PONG 输出只含各自的 user/assistant/result；没有新 transcript 或存活 session 注册项。官方对 `--no-session-persistence` 的准确表述是“不创建或更新本地 transcript，但仍能只读加载旧会话”，所以 session id 必须随机且不可碰撞，不能把该参数误解为“所有用户目录都零写入”。

#### REST serve 路线

**[实测]** 在本次独立 serve 中，用三个不同的 `source.conversation.id` 发起请求，日志显示得到三个不同 agent session id，且都是 `isShared=false`。同一 conversation id 会被 session map 复用，这是为连续对话设计的行为。

如果以后采用 REST，必须让 `conversation.id == benchmark run UUID`，每个 case 唯一；但考虑到计量缺失和 SSE 竞态，当前仍不建议采用。

### 4.2 有没有自传 run id / 幂等 id？

需要拆成“可关联”和“可幂等重试”两件事：

- **CLI [实测]：可精确关联。** `--session-id` 原样出现在 user、assistant、result；assistant 还带 `traceId`、`conversationRequestId`、`messageId`。不必按时间窗匹配。
- **REST [实测]：可精确关联。** client `id` 在 SSE 中作为 `replyTo`，POST 又返回服务端 runId，最终事件给 agent sessionId。建议同时存四元组 `(attempt_id, client_message_id, run_id, session_id)`。
- **两者都不是幂等键。** CLI 重跑会再次调用模型；REST 重复使用已用过的 client `id`，实测仍返回一个新的、不同的 runId。

腾讯当前 HTTP API 文档把入站 `id` 描述为“用于去重与追踪”，但安装版 `2.137.1` 的实际行为没有去重。这是明确的文档/构建偏差。runner 必须自建去重表；未知结果的重试应生成新的 attempt id，并视作可能发生了第二次计费调用。

### 4.3 怎么判断一轮结束？

#### CLI JSON

唯一可靠终点是最终 `type: "result"`：

```json
{"type":"result","subtype":"success","is_error":false,...}
```

以下都不能单独当终点：

- assistant `status: "completed"`：后面仍有 result；
- reasoning 消息结束；
- tool call / tool result 完成；
- 进程退出码 0：无效模型实测也是 0；
- stdout 暂时无新数据。

stream-json 模式同理，要读到最终 result。官方无头文档也写明事件序列最后是带统计的 `result`。

#### REST / ACP

- REST：先收到 `status: "completed"` 的消息，再收到 SSE `event: done`；chunk、tool-call 都不是结束。
- REST 严重竞态：任务完成后 stream 对象立刻删除，晚订阅返回 404，而不是回放结果。
- ACP **[代码+协议，完整 prompt 未实测]**：应等对应 `session/prompt` 的 JSON-RPC response，并以 `session_end` 更新作为会话终态；普通 message/tool update 不是终态。

### 4.4 token、耗时、费用和实际模型从哪读？

CLI 的最终 result 提供：

- `usage.input_tokens`
- `usage.output_tokens`
- cache read/create token
- `duration_ms`
- `duration_api_ms`

每个 assistant message 的 `providerData` 提供：

- `requestModelId`：调用方请求的模型/别名；
- `model`：服务端实际执行模型；
- `rawUsage.prompt_tokens / completion_tokens / total_tokens`；
- reasoning token、cache token、`credit`；
- trace/conversation request id。

判定模型时必须用 `providerData.model`，不能只相信命令行参数。`default -> deepseek-v4.1-flash` 已证明别名会解析成具体模型。

边界：

- `total_cost_usd` 当前恒为 0，不可用；
- `credit` 是产品积分，不应改名为美元成本；
- 一次包含子代理/多模型/重试的复杂 run 尚未实测。runner 应保留所有 assistant/model 记录并按实际 model 聚合，不能只拿最后一条；
- 外层 wall time 包含 CLI 冷启动，更接近用户体验；内部 `duration_*` 适合拆分模型/代理耗时。两者都应存。

REST `/runs` 的 SSE 在本版本只给回答和 session/toolCalls，`/stats/session` 实测也没有本次 token，因此它无法独立满足这项要求。

---

## Q5：能否指定模型/供应商，错参会不会静默回落？

### 内置模型

**[实测]** `--model <id>` 有效：

```text
--model glm-5.3-flash
requested: glm-5.3-flash
actual:    glm-5.3-flash
answer:    PONG
```

**[实测]** 无效模型不会静默回落，stderr 明确报 400 并打印账号可用模型：

```text
400 model [definitely-not-a-real-model-9f2c] service info not found
Currently supported models for your account:
  - fast-model
  - balanced-model
  - deep-model
  - hy4-preview
  - hy3
  ...
Please use --model <model_id> to specify a valid model.
```

但外层进程退出码仍然是 **0**，且没有最终 result JSON。这正是必须做结构化结果校验的原因。

### 自建 NewAPI / 自定义供应商

> 2026-09-22 更新：用户已完成配置，WorkBuddy → 本机 NewAPI 单轮文本请求实测成功，
> CLI 与后台 token 数一致。下文“未实测”是原始调查时状态；最新证据及未覆盖范围见
> [接入验收](workbuddy-newapi-validation.md)。

**[官方+代码，未实测]** 支持两种配置入口：

1. WorkBuddy 的 `%USERPROFILE%\.workbuddy\models.json` 定义 model id、完整 `/chat/completions` URL、vendor、API key 和能力；调用时仍只传 `--model <id>`。
2. 临时环境变量 `CODEBUDDY_BASE_URL`、`CODEBUDDY_API_KEY`、`CODEBUDDY_MODEL`；从 WSL 启 Windows 进程时这些变量也必须放进 `WSLENV`。

官方格式见[模型配置](https://cloud.tencent.com/document/product/1831/137010)和[环境变量](https://cloud.tencent.com/document/product/1831/137014)。CLI 没有单独的 `--provider` 参数；“供应商”由所选模型配置的 URL/vendor 决定。

当前本机 `models.json` 的自定义模型条目数为 0。为了遵守“不改配置/登录态”，本次没有添加 NewAPI 模型，也没有真实 API key 可做临时环境测试。因此：

- **可配置自建网关：已由官方文档和本机代码确认；**
- **这台机器上的 NewAPI 完整调用：未实测；**
- 将来接好后，仍须以 `providerData.requestModelId` 对比 `providerData.model`，并到 NewAPI 侧用请求/trace 记录核对成本。

---

## 安全问题单列

### 没发现 YonWork 同等级的默认局域网暴露

当前所有桌面监听都在 `127.0.0.1`。普通 `--serve` 默认也是 loopback + password；桌面托管 sidecar 使用内存随机 secret。没有发现默认 `0.0.0.0` 免鉴权聊天/RCE。

### 中风险：ACP / internal 信任本机进程

桌面源码明确让 loopback ACP 免 Bearer，并让 `/internal/*` 绕过 `requireAuth`。这不是 LAN 漏洞，但意味着随机端口和固定安全头不是对恶意本机原生进程的安全边界。**[代码，未利用测试]**

### 高风险配置：显式 `--auth none`

它会让同机任意进程调用文件、进程、PTY、Agent 等接口。官方和实际启动输出都给出警告。跑批不要为了省事关闭鉴权；若选择 serve，应使用临时高熵 password、loopback 绑定和短生命周期进程。

### 低风险信息暴露：LocalProbe

18488–18490 探活端点允许任意网页跨域读取 app 名、版本、平台，可用于本机软件指纹识别；没有业务状态和执行能力。

### 本地日志含身份与遥测字段

WorkBuddy 日志可能包含 user/machine/session/trace 标识。runner 不应整份上传日志；只提取必要、脱敏的 run/session/usage/model 字段。

---

## 没查清楚的与不应继续猜的

1. **NewAPI 实际链路与费用。** 没有现成自定义模型配置；继续需要写配置或注入真实 key，超出本次只读边界。
2. **ACP 在桌面托管、已登录 sidecar 上的完整 prompt + usage。** 已跑通 ACP 建连/初始化并核对方法表，但未完成该部署形态下的模型轮次。REST 和 CLI 已足以完成路线决策。
3. **多模型、子代理、工具重试时最终 usage 是否覆盖全部调用。** 单模型 PONG 已验证，复杂聚合尚未验证；接 runner 时应设计一个专门计量 case。
4. **Windows WorkBuddy 进程直接读取 WSL-only 项目文件的完整兼容性。** 从 WSL UNC 当前目录启动会有 `cmd.exe` 警告但纯 prompt 成功；涉及仓库文件的 case 最好放在 Windows 可见路径后另测。
5. **凭据落盘内容。** 只确认桌面 gateway secret 不落盘；没有解密或查看 connector/auth 凭据，因此不对所有账号凭据的 at-rest 保护下结论。
6. **REST `id` 文档声称去重而安装版不去重的版本边界。** 当前 `2.137.1` 实测结果明确；升级后必须回归，不能沿用假设。

---

## 调查副作用与清理

- 没有写入或修改 `D:\WorkBuddy`；没有杀原有 WorkBuddy 桌面/daemon 进程。
- live CLI 实验会按产品自身逻辑写用户目录日志、trace 和产品配置缓存；`--no-session-persistence` 只禁止 transcript，不等于全盘只读。
- 调用前后 hash 对比发现变化的 local-storage 项均可按结构识别为 product config/feature/model metadata 缓存；没有把其中的用户标识或内容写入报告，也没有主动修改 settings/login。
- 本次单独启动的 serve 测试进程已停止；它留下的单个失效 `sessions/<pid>.json` 已在确认 PID 消失、sessionId 匹配后精确删除。该文件不可恢复，但只含本次临时 endpoint/heartbeat 元数据；原有旧注册文件未动。

## 最终落地建议

第一阶段直接把路线 A 封装成 Python `subprocess` adapter：

1. 每 case 生成 UUID，作为 `--session-id`；
2. 每 case 新进程，固定 `--no-session-persistence`；
3. stdout/stderr 分流，stdout 严格解析 JSON；
4. 只以成功 result 作为完成；退出码仅辅助；
5. 保存请求模型、实际模型、usage、credit、内部耗时与外部 wall time；
6. invalid-model、timeout、缺 result、实际模型不符全部硬失败；
7. NewAPI 配置就绪后再加第二套环境，并以网关账单补齐真实成本。

不用碰 UI，也不用依赖 11983/11986/18488。若以后为了吞吐改成长驻服务，优先评估 ACP 的独立 `session/new`，并先补齐 usage 和可靠回放；不要直接把当前 `/api/v1/runs` SSE 当成稳定队列。
