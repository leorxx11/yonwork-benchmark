# NewAPI / 统一代理超时排查（2026-09-22，复查修订）

**当前已应用：修正无效环境变量、DeepSeek 渠道使用 HTTP/1.1、关闭上游空闲连接复用。**
网关恢复可调用，WorkBuddy 和 YonWork 实际普通对话均通过；
回归仍有一次 TLS 握手失败，不能声称彻底根治。

旧版文档的「确定是连接池，不是网络」证据不足；「从二进制确认过参数」的判断也有误。
重启恢复只能说明进程/连接状态相关，不能独自证明哪一跳丢包，更不能排除网络问题。

## 本次复现与处理

实际链路：`YonWork / WorkBuddy → 采集代理 :3312 → NewAPI :3000 → DeepSeek`。
NewAPI 镜像仍为固定摘要，对应 **v1.0.0-rc.38**，没有升级镜像或更换上游密钥。

| 阶段 | 实测 |
|---|---|
| 原配置复发 | 16:48:48、16:48:50 两条请求均等满 180 秒，报 `Client.Timeout exceeded while awaiting headers`；`/api/status` 始终 200 |
| 同时对比三路 | 直连 DeepSeek 1.204s 完成；NewAPI 与采集代理均在 20s 探针期限内未完成 |
| 仅修改 DeepSeek 渠道协议，不重启 NewAPI | 直连 0.870s；NewAPI 0.895s；采集代理 0.871s，均 HTTP 200 |
| HTTP/1.1 + 保留连接池（每主机 8） | 非流式 9/10 成功；流式 19/20 成功，仍各出现一次 20s 超时 |
| HTTP/1.1 + 每主机空闲连接数 -1 | 非流式 29/30，流式 30/30；唯一失败为 `net/http: TLS handshake timeout`，约 10.19s 返回 HTTP 500 |
| 最终配置空闲 120s 后再次流式调用 | 两轮合计 4/4 成功，0.731～1.326s，均实际输出且收到 `[DONE]` |

因此，**只换 HTTP/1.1 不够；关闭复用缓解了本轮成簇挂住现象，但没有消除所有网络失败。**
单次 TLS 握手超时发生在新建连接阶段，不能再归因于旧空闲连接。
尚未抓包，不能判定责任在 Docker 网桥、WSL/Windows、中间网络还是上游边缘节点。

## 原配置为什么没起作用

已读取对应 tag 的官方源码，并与运行二进制中的字符串交叉核对：

| 旧变量 | 当前版本实际读取 | 旧配置的效果 |
|---|---|---|
| `IDLE_CONN_TIMEOUT` | `RELAY_IDLE_CONN_TIMEOUT` | 没生效，仍使用默认 90s |
| `MAX_IDLE_CONNS` | `RELAY_MAX_IDLE_CONNS` | 没生效，仍使用默认 500 |
| `CONNECT_TIMEOUT` | 无对应的转发环境变量 | 没生效；二进制里的 `PGCONNECT_TIMEOUT` 属于数据库代码 |
| `RELAY_TIMEOUT` | 同名 | 生效；覆盖整个请求，包含响应体读取 |
| `STREAMING_TIMEOUT` | 同名 | 生效；不是「等待响应头」的专用超时 |

来源：[NewAPI 初始化代码](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.38/common/init.go#L112-L116)、
[转发 HTTP 客户端](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.38/service/http_client.go#L79-L131)。
**容器里能看到环境变量，不等于应用读取了它；二进制子串命中也不等于它是独立配置项。**

## 已生效的部署配置

根 `compose.yml` 和独立的 `newapi/docker-compose.yml` 已同步：

```yaml
RELAY_IDLE_CONN_TIMEOUT: 30
RELAY_MAX_IDLE_CONNS: 8
RELAY_MAX_IDLE_CONNS_PER_HOST: -1
RELAY_RESPONSE_HEADER_TIMEOUT: 60
RELAY_TIMEOUT: 180
STREAMING_TIMEOUT: 180
```

`.env.example` 保留 `NEWAPI_*` 作为本项目的配置接口，再由 Compose 映射成上述变量。
`RELAY_RESPONSE_HEADER_TIMEOUT` 限制等待上游响应头，不会在 60s 时截断已经开始的流式输出；
`RELAY_TIMEOUT=180` 仍是整个 HTTP 请求的上限，长请求须按需求调整。

**还必须在 DeepSeek 渠道中设置 HTTP 协议为 `http1`。** 本机已通过管理 API 写入渠道 3，
存储位置是 `setting` JSON 内的 `http_protocol`，注意不是另一列 `settings`。
原有模型映射、URL、密钥、状态及其他配置均保留。
这个设置保存在 `newapi/data/one-api.db`，重建容器仍然保留；新机器空库新建渠道时需设置一次。

当前版本原生支持此选项：[渠道字段定义](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.38/relaykit/dto/channel_settings.go#L13-L33)、
[HTTP/1.1 实现](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.38/service/http_transport_sharded.go#L101-L124)。
`RELAY_MAX_IDLE_CONNS_PER_HOST=-1` 对应 Go `http.Transport.MaxIdleConnsPerHost < 0`：
HTTP/1.1 连接完成后不放回空闲池，下一次重新建立 TCP/TLS 连接。
这是明确的稳定性取舍，会增加连接与握手开销。
[Go 连接回池逻辑](https://go.dev/src/net/http/transport.go)。
不要用 `0` 表示禁用：这个字段的 `0` 表示使用默认值。

应用修改后的环境变量：

```bash
docker compose config --quiet
docker compose up -d --no-deps new-api
```

`docker compose restart new-api` **不会应用 Compose 环境变量修改**，只适合按当前参数重启。
不要裸跑 `docker compose up -d`，否则容器 Worker 会被拉起，与宿主机 Worker 抢锁。

回退本次连接策略：将渠道 `setting.http_protocol` 改回 `auto`，
将 `.env` 中 `NEWAPI_MAX_IDLE_CONNS_PER_HOST` 设为正数（例如 8），然后仅重建 `new-api`。
正确的 `RELAY_*` 变量名应保留，不应恢复旧的无效参数。

## 可重复探活

`/api/status`、采集器 `/healthz`、`/v1/models` 都不调用模型，不能证明推理链路可用。
新增脚本读取 `.env` 中现有采集器凭据，仅输出状态、耗时和请求 ID，不输出凭据或响应正文。
请求使用 8 个输出 token 上限；会发生少量真实模型费用，不与正式跑批同时运行。

```bash
# 每轮依次经过 NewAPI 和采集代理，任一次失败退出码为 1
.venv/bin/python scripts/probe_newapi.py --rounds 3
# 流式响应必须实际有输出且收到 [DONE]，不能只看 HTTP 200
.venv/bin/python scripts/probe_newapi.py --stream --rounds 3
# 两轮之间空闲 120s，检查空闲后的恢复
.venv/bin/python scripts/probe_newapi.py --stream --rounds 2 --idle-seconds 120
```

`--route newapi` 或 `--route collector` 可单独选择入口。
脚本默认网络读超时 20s，不自动重试，以免隐藏失败。
探针沿用现有模型令牌，采集器将其记为未归属流量，不能拿这段时间窗给正式轮次算用量。

本轮脱敏探活结果与对应 tag 的公开源码保存在 `results/newapi-stall-fix/`（不进 Git）：
`nonstream.jsonl` / `stream.jsonl` 是 HTTP/1.1 仍保留连接池的阶段；
`no-reuse-nonstream.jsonl` / `no-reuse-stream.jsonl` 是最终关闭复用的阶段。
前面的失败样本没有删除或并入最终阶段以美化成功率。

## 实际产品链路的附加问题

宿主机 Worker 仍加载旧版采集器代码，虽已启动常驻采集服务，却继续绑定 `:3312`，
导致本次最初提交的两个验收 Job 启动即报 `Address already in use`。
确认没有运行/排队任务后，已正常停止这个旧 Worker，并用 `scripts/host_worker.sh` 重启。
当前使用 `RemoteCollector`，容器 Worker 保持停止。
**以后修改 Worker/采集器 Python 代码，也要重启常驻宿主机 Worker，不能只重建 collector。**

重启后的 WorkBuddy 验收 `f2c35155820047d5bff01dc76319c09a`：Pass，整轮 5.759s，
实际模型请求 0.982s，HTTP 200、`completed`，原生轮次标识精确关联。

YonWork 首轮 `af9909c404164666a8d6ee1154552681`：Timeout，不应算作网关修复成功。
日志明确显示产品内部 Gateway 启动/连接过程异常：`main.gateway-rpc-chat-send` 耗时 53.098s，
并出现 `device.pair.list` / `sessions.subscribe` RPC timeout。
本轮没有精确归属的代理请求；同窗另有一条未归属请求在 2.529s 内成功，不能算成这轮成功。
日志显示产品已升级到 1.0.10，旧版 1.0.8 的验收结论不能自动套用。
未修改 `D:\yonwork\` 的程序文件、产品配置或登录态。

Gateway 启动后再次验收 YonWork：Job `80f222bba5a54df4a9740b03245e246b`，
**Pass，整轮 3.125s**。同窗代理收到 `deepseek-flash` 流式请求，1.239s 完成，
HTTP 200、`[DONE]`；与产品账户指向 `http://127.0.0.1:3312/v1` 的配置一致。
但该请求没有旧版的 `x-yonwork-run-id` / `x-yonclaw-run-id` 等关联头，
因此账本为 `unattributed`，本轮 `model_calls.status=unavailable`。
这是本次观察到的新版关联兼容性问题，不能将同窗请求表述为 runId 精确关联成功；
也不能直接按现有通用提示判定为「产品 baseUrl 填错」。本次没有修改归属规则。

## 证据边界

- 当前是已部署的缓解措施，不是「以后永不超时」的保证。
- 新连接仍出现过一次 TLS 握手超时，具体丢包位置未定位。
- 没有重跑完整四模式 benchmark，也没有改写过去的 Timeout 判定。
- 网关请求失败不能仅凭前端 Timeout 就算成 YonWork 缺陷；反过来，产品内部 RPC 超时也不能归咎于 NewAPI。
