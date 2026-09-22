# 逐请求账本与采集代理

日期：2026-09-22。范围：监控待办第 2 项的代码实现。
上游依据见 [入口与任务关联验证](model-entry-validation.md)。

## 状态

`runner/modelproxy/` 已实现并通过 29 项离线单测（全套 226 项）。
**尚未接入任何驱动、Worker、Web 或数据库**，所以现在还没有一轮真实 benchmark
产出过账本。下面写的都是这一层自己的行为，不是整轮覆盖的结论。

| 文件 | 职责 |
|---|---|
| `ledger.py` | 记录结构、JSONL 落盘、重放合并、原材料汇总 |
| `proxy.py` | `产品 → 采集代理 → NewAPI → 上游模型` 里的中间一跳 |
| `config.py` | `.env` / 环境变量配置，默认关闭 |
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

**代理跟着 Worker 进程内起，不是单独的 compose 服务。**
独立服务一旦挂了，产品的模型调用会全部失败——等于我们把被测对象弄坏了，
而且那些失败还会被记成产品的失败。进程内起则代理的生命周期和驱动跑批的那个进程绑定，
Worker 不在时本来也没有批次在跑。代价是入口端口必须**固定**（产品配置里存的是 URL），
这和「保留串行锁、一次只允许一个 Worker」正好相容。

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

接进跑批之前想先验证产品能不能连上这个入口，用长驻模式手动发一条消息：

```bash
.venv/bin/python -m runner.modelproxy serve      # Ctrl-C 停止
```

此时没有轮次注册，经过的请求一律记 `unattributed`，但足以证明链路通。

### 回退

```bash
# 1. .env 里设回 BENCH_COLLECTOR_ENABLED=0
# 2. 重启 Worker
docker compose restart worker        # 或 Ctrl-C 后重跑 ./scripts/host_worker.sh
```

⚠️ **只改开关不够。** 产品配置里的 baseUrl 还指着我们，必须同时改回直连 NewAPI
（YonWork 的 provider、WorkBuddy 的 `models.json`），否则产品会打到一个没人监听的
端口，每一轮都失败——而且那个失败长得像产品的问题。

## 明确还没做的

- 接驱动 / Worker / Web / MySQL：一轮真实 benchmark 还不会产出账本（待办第 3、4 项）。
  配置和健康检查已经就位，但**没有任何代码调用 `CollectorConfig` 去起代理**。
- 诊断原文的显式开关、脱敏和保存期限：没实现，目前只能记元数据。
- 连接复用、并发压测：本项保留串行锁，没有测过并发下的行为。
- 真实的工具续答、重试、子代理、取消场景：待办第 1 项的路由与收尾验证仍未完成，
  **整轮覆盖状态保持未确认**。
