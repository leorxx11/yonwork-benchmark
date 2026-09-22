# NewAPI 间歇性挂死排查（2026-09-22）

症状：跑批跑着跑着变慢甚至超时，界面上看像「DeepSeek 连不上」。
**结论：是 NewAPI 自己的 HTTP 连接池，不是网络、不是上游、不是渠道配置。**

## 定位过程

| 怀疑 | 怎么测的 | 结果 |
|---|---|---|
| 容器出不了网 | 容器内 wget api.deepseek.com | ❌ 401 秒回，DNS/TLS 正常 |
| 某个解析 IP 坏了 | 容器和宿主机分别打两个 IP | ❌ 都连得上 |
| MTU 丢大包 | 容器内发 8KB POST | ❌ 照样秒回 401 |
| 渠道配置错 | 只有一个渠道；重启后连打 8 次 | ❌ 8/8 正常 |
| 上游 DeepSeek 挂了 | 宿主机用**真 key** 直连 | ❌ 1.1s / 1.65s 正常 |
| **NewAPI 自己** | 容器内用**真 key** 直连 vs 走 NewAPI 转发 | ✅ 直连 1～2s（3/3），转发挂几分钟 |

决定性的一组对照：

```
重启前：3/3 超时（25s）
重启后：3/3 成功（0.73s）
```

同一个容器、同一条网络、同一个 key、同一个上游，**唯一变量是进程重启**。

## 机制

Go 的 `http.Transport` 会复用连接。连接在空闲期间被中间设备静默丢弃后，
客户端并不知道，仍把新请求写进去，直到读超时才失败。表现就是：

- 失败**成簇**（14:55 / 15:32 / 16:00 三簇，簇间完全正常）
- 同一簇里多条失败共用同一个本地端口
- 一整簇在**同一秒**集体失败——那是读超时到期，排队的请求一起放掉
- 直连 wget 每次新建连接，所以永远秒回

⚠️ 一度因为「失败跨了 5 个本地端口、两个 IP」而否掉这个解释，那是错的：
连接**池**本来就有多条连接，多个端口中招恰恰是池被污染的表现，不是反证。

## 处理

`compose.yml` 给 new-api 加了四个参数（NewAPI 支持，从二进制里确认过）：

```yaml
IDLE_CONN_TIMEOUT: 30    # 空闲连接尽早丢弃，别等它变质
MAX_IDLE_CONNS: 8
CONNECT_TIMEOUT: 10
RELAY_TIMEOUT: 180       # 把「挂几分钟」变成有界失败
STREAMING_TIMEOUT: 180
```

**`RELAY_TIMEOUT` 是这里最关键的一条，理由和性能无关**：挂住比失败更糟。
一轮 benchmark 挂在上游几分钟，最后会被记成产品 `Timeout`——
那是一条假缺陷。有界失败至少能被断言层和逐请求账本如实记下来。

复发时：`docker compose restart new-api`。
⚠️ 别用裸 `docker compose up -d`，worker 容器会被拉起来抢宿主机 Worker 的锁。

## 这件事对基准测试的意义

逐请求账本在这次事故里证明了自己。有一轮 `C3#1 → Timeout`，账本记的是：

| 请求 | HTTP | 终止 | 耗时 |
|---|---|---|---|
| 1 | 200 | `stream-truncated`（IncompleteRead） | 301s |
| 2 | — | `timeout`（TimeoutError） | 300s |
| 3 | — | 未完成（`late`，仍归本轮） | — |

判定记的是 Timeout（算在产品头上），**但账本证明是上游三次都不回数据**。
没有这层账本，这一轮就是一条「YonWork 超时」的假缺陷。

⚠️ **跑批前先探活**，别拿一个正在挂死的网关跑基准：

```bash
for i in 1 2 3; do curl -s --noproxy '*' --max-time 25 -o /dev/null \
  -w "%{http_code} %{time_total}s\n" http://127.0.0.1:3000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'; done
```

## 未解决的

- 加了那几个参数之后**还没跑满一整批**验证复发率，不能说已经修好。
- 到底是哪一跳丢的空闲连接（Docker 网桥 / WSL / Windows / 中间网络）没有定位，
  也没抓包。对我们够用，但不是根因。
