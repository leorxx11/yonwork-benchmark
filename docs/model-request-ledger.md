# 逐请求账本与采集代理

日期：2026-09-22。范围：监控待办第 2 项的代码实现。
上游依据见 [入口与任务关联验证](model-entry-validation.md)。

**YonWork 1.0.10 兼容性：请求仍被采集，但旧轮次头已在实测路径中消失，可能记为未归属。**
现有 HTTP 账本未存 traceparent 值；hook 映射方案仅完成探针，尚未接入正式归属，见
[1.0.10 探针与适配方案](yonwork-1.0.10-correlation-probe.md)。下文旧版精确关联结论不自动适用于新版。

## 状态

`runner/modelproxy/` 已实现，并已接进 `batch.run_batch` / CLI / Worker，
46 项离线单测（全套 254 项）。**默认关闭**，关着时跑批行为和以前完全一样。
入库与 Web 报告**已完成**。

四模式对比 v4（48 轮）之后的归属状态：

- **WorkBuddy**：13 请求全归属、覆盖 12 轮、无串轮 ✓
- **YonWork**：1.0.8 精确关联通过；**1.0.10 起归属率归零**（13 请求 0 归属），
  见 [1.0.10 探针](yonwork-1.0.10-correlation-probe.md) 和
  [待办第 5 项](model-call-monitoring-todo.md)

工具续答、子代理、取消三格未跑。下面写的是这一层自己的行为，
不是整轮覆盖的结论。

| 文件 | 职责 |
|---|---|
| `ledger.py` | 记录结构、JSONL 落盘、重放合并、原材料汇总 |
| `proxy.py` | `产品 → 采集代理 → NewAPI → 上游模型` 里的中间一跳 |
| `config.py` | `.env` / 环境变量配置，默认关闭 |
| `client.py` | 连常驻服务的 `RemoteCollector` + `build_collector` 选路 |
| `__main__.py` | `selfcheck`（离线自检 + 额外开销）/ `serve` / `health` |

## 三层关联结构

```text
run（一轮 benchmark，= BenchmarkId）
  └── request（一次客户端到代理的 HTTP 请求）   ← 账本记的是这一层
        └── upstream attempt（网关到上游的尝试） ← 看不见
```

`upstream_attempts` 恒为 `None`。一次客户端请求**不等于**一次上游尝试，
网关内部重试没有证据，填 1 就是拿观测不到的东西冒充数据。

关联靠产品原生请求头，只认**全等**，命中不了就记未归属，不按时间窗猜：

| 请求头 | 来源 |
|---|---|
| `x-yonwork-run-id` / `x-yonclaw-run-id` | YonWork 1.0.8 |
| `x-conversation-id` | WorkBuddy 5.5.6 |
| `x-benchmark-run-id` | 我们自己注入，仅在没有原生头时兜底 |

加产品时改 `proxy.CORRELATION_HEADERS` 一处，不要在别处另写匹配。
带前缀的别的 ID（`bench-1-subagent` 对 `bench-1`）**不算命中**，有单测盯着。

## 归属策略：和隔离探针刻意不同

`scripts/probe_model_entry.py` 那个一次性探针会拒掉可疑请求，因为它要证明「能隔离」。
正式采集的目标相反——**不影响被测对象**：

| 情况 | 探针 | 采集代理 | 为什么 |
|---|---|---|---|
| 凭据不对 | 401 | 401，记 `rejected` | 不是被测产品发的 |
| 路径不认识 | 404 | 404，记 `rejected` | 同上 |
| 有凭据、没有关联头 | 404 | **照常转发**，记 `unattributed` | 拒掉会打断产品 |
| 关联头命中已收尾的轮次 | 410 | **照常转发**，记 `late`，仍归旧轮 | 同上 |
| 请求体是 chunked 编码 | 不涉及 | 501，记 `rejected` | 见下 |

实测两款产品都带 `Content-Length`，所以没实现 chunked 解码。
**但不能静默转发一个空 body**：产品会收到莫名其妙的回答，而账本看起来一切正常。
将来产品换编码时要当场报错，不要变成假数据。

子代理或辅助模型如果不带关联头，拒掉就等于我们把产品打断，然后还会把这次打断
算成产品的失败（违反 CLAUDE.md 二-4）。所以未归属是**一等结果，不是失败**。

`close_run()` 只改归属标记。它表示「产品已终止本轮」，**不表示「不会再有请求」**——
安静一段时间只是等待策略，单独证明不了任务已经没有后台调用。

## 记什么、不记什么

记：产品、run_id、归属状态与命中的头名、协议、路径、请求模型、响应自称模型、
是否流式、消息条数、请求头**名字**、HTTP 状态、终止原因、首个有效输出耗时、
总耗时、usage 及其状态、`[DONE]`、输出事件数、错误类型、NewAPI 的 `x-oneapi-request-id`。

不记：提示词、回答正文、工具结果、鉴权头。请求头只留名字不留值，
唯一例外是关联头的值，而那本来就是我们自己发出去的 BenchmarkId。有单测断言
账本文件里搜不到提示词、客户端令牌和上游令牌。

**缺失一律标 missing，不补零**（同 `UsageSample` / `LogStats` 的规矩）。
`summarize()` 只把观测到的 token 加起来，并同时给出覆盖数，所以那是
「已观测小计」，不是整轮 token。

### 首个有效输出的定义

带内容或工具参数的那一片才算：`delta.content`、`delta.reasoning_content`、
`delta.tool_calls` 任一非空。**响应头、role-only 片、空 delta 都不算**——
它们先到，算进去首字会测得偏早。工具参数分片必须算，否则工具那一路会被记成
「一直没输出」。

## 落盘与重放

一次请求两行：收到请求就写 `open`，结束时写 `closed`。理由和
`report.append_jsonl` 一样——跑到一半崩了不能什么都不剩。

`load_ledger()` 按 `request_id` 合并，`closed` 覆盖 `open`；重复导入同一份文件
不会变成两条。只有 `open` 的记录标成 `incomplete`，**既不当正常请求也不丢掉**：
「采集器中断」和「本来就没有请求」必须分得开。

## 测量边界与额外开销

`python -m runner.modelproxy` 用本文件里的桩上游跑 15 轮，实测：

| | n | 中位数 | 最小 | 最大 |
|---|---:|---:|---:|---:|
| 直连桩上游 | 15 | 1.19 ms | 0.85 | 5.46 |
| 经过采集代理 | 15 | 6.41 ms | 5.74 | 7.60 |

代理引入的中位差值 **5.22 ms**。这个数的边界必须说清楚：

- 上游是同进程的桩，**不含网关和模型时间**，所以这是代理自身在回环上的**下限**开销。
- 代理每个请求新建一条上游连接，没有连接复用；5ms 里主要是这一段。
  真实跑批里模型耗时是秒级，这一段可以忽略，但**不能反过来说「开销为零」**。
- 代理测到的耗时是「代理视角的请求时长」，**不是纯模型计算时间**。

## Docker 一键环境接入

**代理是常驻的 compose 服务 `collector`。**（2026-09-22 改过一次，见下）

```bash
docker compose up -d collector      # 起了就一直在，跑不跑批都在
```

⚠️ **这里我改过决定，理由值得记下来。** 最初做成「跟着 Worker 进程内起」，
理由是「独立服务挂了会让产品的模型调用全部失败」。那个风险是真的，
但实际用下来**反向的坑更大**：不跑批时入口是死的，在产品界面里手动选这个模型
直接报「模型服务暂时不可用」——而且那个报错看起来像产品坏了，
排查时会往完全错误的方向走（2026-09-22 就这么浪费了一轮）。

改成常驻之后：

- 跑批通过 `/_control` 注册/收尾轮次，账本按 `batch_id` 落到对应目录
- 手动在产品里聊天的请求记 `unattributed`——**正常，不是故障**，它们本来就不属于任何一轮
- 原来那个风险靠 `RemoteCollector.start()` 的健康检查兜：连不上就**报错不跑批**，
  宁可不开始，也不要跑出一批「产品好像全挂了」的数据

`BENCH_COLLECTOR_MODE`：`auto`（默认，服务在就用、不在就自己起）/ `service` / `embedded`。
显式 `service` 却连不上时**不会静默回落**去抢端口——那会把「服务没起来」
变成「端口冲突」，报错指向完全错误的地方。

入口端口必须**固定**（产品配置里存的是 URL），这和「保留串行锁、
一次只允许一个 Worker」正好相容。

⚠️ collector 容器把 `./runner` 挂进去而不是烤进镜像：这一层还在迭代，
挂载之后 `docker compose restart collector` 就生效，不用重建镜像。

容器是 `network_mode: host`，绑 `127.0.0.1` 就是 WSL 的 loopback，
再由 mirrored 网络连到 Windows——和 YonWork 只绑 `127.0.0.1` 是同一个机制，
所以容器 Worker 和宿主机 Worker（`./scripts/host_worker.sh`）用同一份配置都成立。

### 配置

全部在 `.env`（模板见 `.env.example`），compose 已把它们透传给 web 和 worker。
口径是**先环境变量后 `.env`**，同 `NewApiConfig.load` 和 `BENCH_WORKBUDDY_*`——
容器靠 Compose 注入，宿主机 Worker 和 CLI 只有 `.env`，只读 `os.environ`
会让 `.env` 里写的值静默失效（2026-09-21 踩过）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `BENCH_COLLECTOR_ENABLED` | `0` | **默认关闭**，关着时跑批行为和今天完全一样 |
| `BENCH_COLLECTOR_MODE` | `auto` | `auto` / `service` / `embedded`，见上 |
| `BENCH_COLLECTOR_PORT` | `3312` | 固定端口；和 3000/3211/3307/8000/9222 冲突会报错 |
| `BENCH_COLLECTOR_BIND` | `127.0.0.1` | |
| `BENCH_COLLECTOR_UPSTREAM` | 跟随 `NEWAPI_BASE_URL` | |
| `BENCH_COLLECTOR_UPSTREAM_KEY` | 无 | 代理调 NewAPI 用，填产品原来直连的那个令牌 |
| `BENCH_COLLECTOR_CLIENT_TOKEN` | 无 | 产品配置里当 apiKey 填，用来挡同端口的其它流量 |
| `BENCH_COLLECTOR_LEDGER_DIR` | `results` | 账本落 `<dir>/<batch_id>/model-requests.jsonl` |

开了却没配两个凭据会**当场报错**，不会带病启动：少 `upstream_key` 会让每轮都收到
网关 401（看起来像产品坏了），少 `client_token` 则等于入口对本机任何进程敞开。

### 健康检查

```bash
.venv/bin/python -m runner.modelproxy health     # compose worker 的 healthcheck 就是它
```

**关着时返回 0**——关着是默认状态，不是故障。开着但入口连不上才返回 1，
那正是「配了却没生效」的信号。

没装 docker 时可以手动起同一个服务：

```bash
.venv/bin/python -m runner.modelproxy serve      # Ctrl-C 停止
```

### 回退

```bash
# 1. .env 里设回 BENCH_COLLECTOR_ENABLED=0
# 2. 停掉常驻服务并重启 Worker
docker compose stop collector
docker compose restart worker        # 或 Ctrl-C 后重跑 ./scripts/host_worker.sh
```

⚠️ **只改开关不够。** 产品配置里的 baseUrl 还指着我们，必须同时改回直连 NewAPI
（YonWork 的 provider、WorkBuddy 的 `models.json`），否则产品会打到一个没人监听的
端口，每一轮都失败——而且那个失败长得像产品的问题。

## 接进跑批

已接：`batch.run_batch` 收一个可选的 `collector`。CLI 和 Worker 用
`build_collector()` 选路——常驻服务在就连它，不在就自己起一个。
`batch.py` 对这两种一无所知，因为 `RemoteCollector` 和 `CollectorProxy` 形状一样。

```text
build_collector()（常驻服务 或 本进程内，proxy_id = batch_id）
  → 每轮发请求**之前** register(benchmark_id)
  → driver.run_turn(...)
  → finally close_run(benchmark_id)     ← 抛异常也要收，否则下一轮的请求会算进这一轮
  → 把本轮请求收进 RunRecord.model_calls
```

注册必须在发请求**之前**：归属只认原生头全等，晚一步的话本轮最早那几个请求会记成
未归属，事后补不回来。

`RunRecord.model_calls.status` 三态，**「没开采集」和「0 次调用」必须分开**：

| 状态 | 含义 |
|---|---|
| `disabled` | 采集代理没启用（默认）。报告显示「未采集」，不是 0 |
| `observed` | 这一轮确实有请求经过入口 |
| `unavailable` | 采集开着，但这一轮一个请求都没经过 |

`unavailable` 那条是这次接线里最关键的一个判断。产品答上来了却一个请求都没经过入口，
几乎一定是它的 baseUrl 没指向我们，而不是产品真的没调模型。这时记 0 就成了六-3
那种静默漏记：数字看着正常，全是假的。所以标 `unavailable` 并在 note 里写清楚该查什么，
由断言层决定怎么归类——这一层仍然只搬运不判定。

⚠️ **产品的 baseUrl 要手动指过来**，驱动不会自动改产品配置。
端口固定就是为了这一步只做一次；按
`docs/model-entry-validation.md` 的结论，**不在测量轮次中反复新建账户**。

## 实测：两个产品各一轮端到端（2026-09-22）

### YonWork 一轮（批次 `collector-e2e`）

`smoke` 的 Case02 跑 1 轮，模型选 YonWork 里新建的 `统一代理`
（baseUrl 指向采集入口）。判定 Pass，`model_calls.status = observed`。

**完整关联链一次打通，全程没有用到时间窗：**

```text
BenchmarkId  bench-Case02-r1-1a0c7afba3a
  → x-yonwork-run-id 严格等值          → 代理记 1 条 attributed 请求
  → x-oneapi-request-id 2026…65WX6aoPI → NewAPI 后台 request_id 命中 1 条
```

逐请求观测：`deepseek-flash → deepseek-flash`、HTTP 200、`completed`、
首个有效输出 1.159s、请求时长 1.837s、118 次有内容的流式分片、
`upstream_attempts` 保持 None。账本两行（`open` / `closed`），重放合并成 1 条，
文件里搜不到提示词和两种令牌。

### WorkBuddy 一轮（批次 `collector-e2e-wb3`）

`smoke` 的 Case02 跑 1 轮，模型 `deepseek-flash`（配置已指向采集入口），判定 Pass。

```text
BenchmarkId  bench-Case02-r1-1a0c7bbf682
  → X-Conversation-ID 严格等值          → 代理记 1 条 attributed 请求
  → x-oneapi-request-id 2026…6JN7pREiW  → NewAPI 后台 request_id 命中，3688/36 一致
```

首个有效输出 1.018s、请求时长 1.200s、35 次分片；整轮 wall 5.795s、CLI 自报内部 2.133s。
⚠️ 同样 n=1，**不是性能结论**；和 YonWork 那轮也不可比（不同 Case 上下文、不同产品）。

**WorkBuddy 拿 `models.json` 的 `id` 当发给网关的模型名。**（实测，两次）
所以不能像 YonWork 那样「新建一条指向代理的同模型条目」来做直连/代理对照——
新条目叫 `deepseek-flash-proxy`，网关就收到这个名字并回 503
`No available channel`。试过把 `name` 改回 `deepseek-flash` 保留新 `id`，**无效**，
证明 CLI 发的是 `id`。最终做法是把**现有那条**的 url / apiKey 指向代理，
原文件备份在 `models.json.bak-collector`，回退就是拷回来。

### 这次失败挖出来的：后台日志看不见被网关拒掉的请求

上面那两次 503 各自触发了 **9 次客户端重试**，而 WorkBuddy CLI 自己
**stdout 空白、退出码 0**，什么都没说。18 次失败请求里：

| 来源 | 看到几次 |
|---|---:|
| 逐请求账本 | 18（`upstream-error`，HTTP 503） |
| CLI 输出 | 0 |
| NewAPI `/api/log/self` | **0**（消费和错误记录都没有） |

⚠️ **这条要当心，它影响已有的结论**：`runner/newapi.py` 采 ErrorCalls 用的就是
`/api/log/self`。被网关在选通道之前直接拒掉的请求**不进这个来源**，
所以「后台错误日志为 0」**不能**推断「这一轮没有失败的请求」。
七-3.1 那套逐轮 ErrorCalls 判定在这类失败上是盲的。
（范围限定：实测的是 503 `No available channel`，且只查了 `/api/log/self`
这一个我们实际在用的来源；别的拒绝类型和管理员视角的日志没试。）

反过来说，这一轮也顺带把验收矩阵里「请求失败后重试」那格跑了个非受控版本：
9 次客户端请求逐条在账本里，`upstream_attempts` 全程保持 None——
客户端重试和网关内部尝试没有被混成一个数。

### 顺带撞出来的一个线索：两个来源的 input token 不是一回事

同一轮，三个来源对不上，而且**差值不是随机的**：

| 来源 | input tokens |
|---|---:|
| 代理（本轮实收） | 16,098 |
| NewAPI 后台 | 16,098 |
| 会话 JSONL | **7,010** |

代理拿到的 usage 里写着 `prompt_cache_hit_tokens: 9088`、
`prompt_cache_miss_tokens: 7010`，而 **7,010 恰好等于会话 JSONL 记的那个数**。
也就是说：会话 JSONL 可能只记**缓存未命中**的那部分，NewAPI 记的是含命中的完整
prompt_tokens。如果成立，CLAUDE.md 三里那个「同一轮换个来源差 7.6 倍」
就有了具体机制，而不只是一句「口径不同」。

⚠️ **n=1，这是线索不是结论。** 要确认得多跑几轮、覆盖缓存命中率不同的 Case，
并且确认会话 JSONL 那个字段的语义。在确认之前，**按来源独立汇总的规矩不变**，
不要因为这个假设去做任何换算。

**而且这是 YonWork 会话 JSONL 独有的**：WorkBuddy 那轮 `workbuddy-cli`
报 3,688，和代理、NewAPI 完全一致（其中缓存命中 3,456、未命中 232）。
所以不是「所有端上来源都只记未命中」，**别推广**。

⚠️ 这一轮的耗时数字（代理 1.837s vs 整轮 wall 3.617s）**不是性能结论**：
n=1、单 Case、单模型。两者之差也不能直接叫「产品开销」，那还包含我们这一跳。

## 入库与报告

账本进了 MySQL 的 `model_requests` 表，事实来源仍是 JSONL，库随时可重建。

**`benchmark_id` 允许 NULL，外键挂在 `batch_id` 上。** 未归属请求属于这个批次但不属于
任何一轮，硬塞给某一轮正是这套账本要消灭的东西。只从每轮的
`model_calls.requests` 入库的话，未归属请求永远进不了报告，而
「没有未归属请求」和「有但我们没记」看起来一模一样——所以入库时会再读一遍
批次的 `model-requests.jsonl` 补上它们（用 `load_ledger`，顺带拿到
`incomplete` 标记）。账本文件读不到就只入已归属的那些，**不伪造缺失的部分**。

`runs.model_calls_status` 存三态。旧库靠 `ingest._ensure_model_calls` 按需 ALTER，
**这一条不吞异常**（列缺了每条 INSERT 都会失败），和 `_widen_usage_source` 刻意不同。

报告两处：

- **`/run/<id>` 逐请求时间线**——每个请求一行：归属来源、模型、首个有效输出、
  请求耗时、HTTP、终止原因、usage、NewAPI requestId。三态各有各的说法，
  `disabled` 和 `unavailable` 都显示「未采集」，**绝不显示 0**。
  同批次的未归属请求单列在下面，不计进这一轮。
- **`/suite/<id>` 覆盖汇总**——每个模式一行：采到/未采到/未开的轮次数、请求数、
  归属分布、非正常终止数、已观测 token 小计及覆盖数。

⚠️ **整轮经过时间和请求耗时之和并列显示，页面明说不能相减**：请求之间还有产品
自己的处理时间，差值不是单一成因。而且会真的去查请求在时间上**有没有重叠**——
并发时相加会把同一段墙钟算多遍，有重叠就在页面上说出来，而不是假设串行。
「上游尝试」一栏恒显示「未知」。

实测渲染（三个批次真实数据）：成功那轮显示 1 条 `completed`；
9 次 503 那轮把 9 条 `upstream-error` 全列出来，usage 显示「未采集」而不是 0。
后者正是 CLI 自己完全没报、NewAPI 后台也查不到的那 18 次里的一半。

## 明确还没做的

- **工具调用 ID 与请求的关联没做**：时间线能说「这一轮发了几次模型请求」，
  但说不出「哪一次是工具续答」。这是待办第 3 项欠的，不是报告层能补的。
- 子代理关联证据同理，没有。
- 工具续答、子代理、取消这些场景没跑过；两轮成功的都是单次文本问答。
  重试只有上面那次**非受控**的意外样本，不算受控验收。
- 费用一栏没有：代理拿不到计费数据，所以不显示，而不是显示 0。
- 两轮各 n=1，任何耗时数字都不构成性能结论。
- 诊断原文的显式开关、脱敏和保存期限：没实现，目前只能记元数据。
- 连接复用、并发压测：本项保留串行锁，没有测过并发下的行为。
- 真实的工具续答、重试、子代理、取消场景：待办第 1 项的路由与收尾验证仍未完成，
  **整轮覆盖状态保持未确认**。
