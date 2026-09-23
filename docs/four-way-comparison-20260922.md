# 四模式横向对比（2026-09-22）

实验名 `四模式横向对比 20260922 v4`，suite `dce8ec64a4cd62b0395bad97f9b8183b`。
4 个 Case × 3 轮 × 4 通路 = 48 轮，**47 Pass / 1 Error**。
用例集 `cases/catalog.yaml:four-way`。

## 设计：同一类模型 × 四条通路

|  | 产品自带 | 经我们配的 NewAPI |
|---|---|---|
| **YonWork** | ① `yonyou-default-auto/deepseek-v4-flash` | ② `custom-3859c0c4/deepseek-flash` |
| **WorkBuddy** | ③ `deepseek-v4.1-flash` | ④ `custom-local:deepseek-flash` |

⚠️ **「四条通路都是同一个模型」不成立，只有一半成立。**

| 通路 | 模型名 | 谁托管 |
|---|---|---|
| ① | `deepseek-v4-flash` | 用友自己的网关 |
| ③ | `deepseek-v4.1-flash` | 腾讯 CodeBuddy 的网关 |
| ②④ | `deepseek-flash` | DeepSeek 官方 API（经本机 NewAPI） |

三套命名、三个托管方。`v4-flash` / `v4.1-flash` / `flash` 是不是同一个底座，
**从外部无法验证**——各家可能有自己的版本、微调或路由。所以：

- **②vs④ 是严格同模型**（同一个 NewAPI、同一个官方端点）→ 唯一纯粹的跨产品对比
- **①vs② / ③vs④** 是「自带 vs 外接」，同产品内，但**模型也换了** → 差值不能归因给单一因素
- **①vs③** 是「两家自带的 DeepSeek」→ 只能叫开箱配置对比

## 通路可信度：四列全部可验证

`provider-match` 断言 **48/48 Pass**，每一列都被证实跑在预期通路上：

| 通路 | 请求经过采集入口 | 会话记录的实际通路 |
|---|---:|---|
| ① YonWork 自带 | **0** | `yonyou-default` |
| ② YonWork→NewAPI | 13 | `custom-3859c0c4` |
| ③ WorkBuddy 自带 | **0** | `Deepseek-V4.1-Flash` |
| ④ WorkBuddy→NewAPI | 13 | `custom-local:deepseek-flash` |

自带通路 0 条请求、外接通路有请求——两侧互为佐证。

⚠️ 这条校验是这次新加的，**因为上一批（v3）栽过**：当时 YonWork「默认模型」那一列
实际跑在统一代理上，12 轮全 Pass、从结果上完全看不出来。根因是驱动把「默认」
处理成不传 `modelSelection`，而 1.0.10 遇到空 selection 不应用、引擎沿用旧配置。
详见 `docs/yonwork-1.0.10-correlation-probe.md` 和该次修复提交。

## 耗时（外层 wall，毫秒）

| 通路 | 中位 | 去掉卡顿后中位 | 范围 | 卡顿轮次 |
|---|---:|---:|---|---:|
| ① YonWork 自带 v4-flash | 5,460 | 5,460 | 4,186～7,449 | **0** |
| ② YonWork→NewAPI flash | 3,384 | **3,142** | 2,310～64,680 | 2 |
| ③ WorkBuddy 自带 v4.1-flash | 7,593 | 6,692 | 5,668～26,404 | 2 |
| ④ WorkBuddy→NewAPI flash | 5,993 | 5,940 | 5,253～177,020 | 1 |

「卡顿」= 超过 20s 的轮次，成因见下。**报中位数 + 范围，不报均值**（CLAUDE.md 四-3）。

按 Case 的去卡顿中位：

| Case | ① | ② | ③ | ④ |
|---|---:|---:|---:|---:|
| C1 短文本 | 5,700 | 3,890 | 8,089 | 5,575 |
| C2 40 行表格 | 7,284 | 4,086 | **18,201** | 5,993 |
| C3 严格 JSON | 4,522 | 2,755 | 6,288 | **12,501** |
| C4 重复 30 行 | 5,219 | 2,887 | 6,260 | 6,851 |

⚠️ 每格 n=3，C2/C3 那两个突出值**不足以下结论**，只能说值得单独复测。

## token（12 轮合计）

| 通路 | 来源 | token | 覆盖 |
|---|---|---:|---|
| ① YonWork 自带 | session-jsonl | 241,042 | 12/12 |
| ② YonWork→NewAPI | session-jsonl / newapi | 241,565 / 241,565 | 12/12 |
| ③ WorkBuddy 自带 | workbuddy-cli | 47,940 | 12/12 |
| ④ WorkBuddy→NewAPI | workbuddy-cli / newapi | 44,078 / 52,604 | 11/12 |

**这批数据里最硬的一条结论：**

> 同样 4 个 Case、同样 12 轮，**YonWork 的 token 消耗是 WorkBuddy 的约 5 倍**
> （241k vs 44～48k）。而且这个比例在自带和外接两条通路上都成立
> （①241,042 / ③47,940 = 5.0；②241,565 / ④44,078 = 5.5），
> **说明它是产品自身的属性（上下文构造、system prompt、编排），与通路无关。**

②④ 是严格同模型，所以 5.5 倍这个数没有模型差异的干扰。

⚠️ ④ 的 `newapi` 列 52,604 比 `workbuddy-cli` 的 44,078 多 19%——多出来的是
失败和重试的请求，NewAPI 按时间窗匹配会把它们算进来。**跨来源不能相加，也不能互相校正。**

## 那 1 个 Error 不是产品的问题

`workbuddy/deepseek-flash C3#3` 判 Error（CLI 无输出、退出码 0）。逐请求账本：

```
#78  attributed  http=200  stream-truncated  167,122ms  IncompleteRead
```

请求发出去了、网关回了 200，然后**流在 167 秒后断掉**。整个实验 3 次非正常终止
全是这一类：

| 请求 | 终止 | 耗时 |
|---|---|---:|
| #56 | `upstream-error` | 60,269ms |
| #75 | `transport-error` | 166,455ms |
| #78 | `stream-truncated` | 167,122ms |

#56 那个 60.3s 正好撞上 `RELAY_RESPONSE_HEADER_TIMEOUT=60`，说明那个参数在生效
（把无界挂起变成了有界失败）。另两个发生在流阶段，由 `RELAY_TIMEOUT=180` 兜底。

**没有逐请求账本的话，这一轮就是一条「WorkBuddy 偶尔要 3 分钟」的假缺陷。**
上游不稳定的详情见 `docs/newapi-stall.md`——**已缓解，未根治**。

## 能说什么 / 不能说什么

**能说：**

- YonWork 的 token 消耗约为 WorkBuddy 的 5 倍，且与通路无关（两条通路各自独立成立）
- ②vs④ 的耗时差（3.1s vs 5.9s 去卡顿中位）是同模型同网关下的产品差异
- 四条通路都经 `provider-match` 证实跑在预期配置上，不是 v3 那种混列
- 上游卡顿被逐请求账本如实记录，没有污染判定归因

**不能说：**

- **不能说这是「同一个模型的四路对比」**——只有 ②④ 同模型
- **不能把 ①vs② 或 ③vs④ 的差值叫「代理开销」或「产品自带通路更快/更慢」**：
  模型和托管方同时变了
- **不能用这批数据比「DeepSeek 在哪家更快」**：v4-flash / v4.1-flash / flash 是三个名字
- 每个 (Case, 通路) 只有 n=3，Case 级的差异只能当线索
- 耗时含上游不稳定的影响；②④ 各有 1～2 轮卡顿，虽已剔除但样本本就不大
- WorkBuddy 每轮起一个新进程（自报内部耗时 ③6,692→3,338ms、④5,940→2,854ms），
  外层与自报之差**不等于冷启动**，未归因

## 已知的观测缺口

- **② 的逐请求账本全是未归属**（13 条请求，0 条归属）。YonWork 1.0.10 起不再发
  `x-yonwork-run-id`，只剩 `traceparent`。账本已开始保存 traceparent 值，
  但补关联要等产品 hook 方案落地，见 `docs/yonwork-1.0.10-correlation-probe.md`。
  ④ 不受影响（WorkBuddy 仍发 `X-Conversation-ID`，13 条全部归属）。
- ①③ 不经过采集入口，所以**天然没有逐请求数据**——这是设计使然，不是漏采。
- YonWork 界面显示的默认模型与 Host API 的 `isDefaultModel` 标记可能不一致，未核实。
  本次 ① 用的是**显式指定** `deepseek-v4-flash`，不依赖那个标记。

## 复现

```bash
docker compose up -d collector      # 采集入口常驻
./scripts/host_worker.sh            # WorkBuddy 需要宿主机 Worker
.venv/bin/python scripts/probe_newapi.py --stream --rounds 3   # 跑批前探活
```

Web 提交四个模式：`yonwork/统一代理`、`workbuddy/deepseek-flash`、
`yonwork/deepseek-v4-flash`、`workbuddy/deepseek-v4.1-flash`，用例集 `four-way`。
