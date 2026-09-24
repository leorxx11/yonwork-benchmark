# YonWork hook 开销开关对照（2026-09-24）

范围：本机 YonWork 1.0.10、OpenClaw 2026.7.1-2，主模型为
`custom-3859c0c4-a539-4aeb-ae0d-105fbccba99f/deepseek-flash`，
实际请求经采集入口 `:3312` 到 NewAPI。测的是**整轮外层耗时**，不是 hook 回调本身的执行时间。

## 做法

按 **开 → 关 → 恢复开** 顺序运行三批。每次改动插件后都从 uTools/开始菜单重启 YonWork，
检查当前网关的加载记录、Host API 只监听 loopback 且校验 token。每批用同一份临时 YAML：
先用「请只输出一个字：好」预热 1 轮，再用相同提示正式跑 6 轮；每轮新 sessionKey，
`--model 统一代理 --timeout 240`。容器 Worker 保持停止，由 CLI 持全局串行锁。
临时用例在 `/tmp/benchmark-hook-cost-20260924.yaml`，未改主用例库。

```yaml
version: 1
case_sets:
  - id: hook-cost
    name: Hook 开销对照
    cases:
      - id: Warmup
        prompt: 请只输出一个字：好
        runs: 1
        enabled: true
        assertions: {min_length: 1}
      - id: Repeat
        prompt: 请只输出一个字：好
        runs: 6
        enabled: true
        assertions: {min_length: 1}
```

各批使用同一命令，仅替换批次 ID：

```bash
.venv/bin/python -m runner --cases /tmp/benchmark-hook-cost-20260924.yaml \
  --case-set hook-cost --model 统一代理 --batch-id hook-cost-on-20260924-1551 --timeout 240
```

| 阶段 | 批次（本机 `results/`） | 正式轮次外层耗时中位数 | 范围 | 请求归属 |
|---|---|---:|---:|---|
| hook 开 A1 | `hook-cost-on-20260924-1551` | 2.248s | 2.174～2.604s | 7/7（含预热） |
| hook 关 B | `hook-cost-off-20260924-1554` | 2.090s | 1.815～2.442s | 0/7；7 条 `unattributed` |
| hook 恢复开 A2 | `hook-cost-on2-20260924-1557` | 2.325s | 2.155～3.454s | 7/7（含预热） |

三批共 21/21 Pass，正式轮次均输出「好」，各有 2 个 output token。
两个启用批次都能证明每轮恰好 1 条模型请求；关闭批次只知道 7 轮期间入口共收到 7 条，
没有逐轮标识，不能把它们逐条指派给各轮。
正式轮次 `session-jsonl` input token 范围分别为 2251～2257、2249～2258、2251～2258。
每批的 7 条代理请求都正常结束；21 条 NewAPI 后台消费记录按 `request_id` 与代理逐条匹配，
input/output token 也逐条相等。模型与 provider 三批一致。

启用时的两个批次各有 14 个 hook 事件，对应 7 个不同 span；`verify --batch` 均通过。
关闭组的当前网关加载记录确认没有加载插件，7 条请求仍经过采集入口和 NewAPI，
但由于 YonWork 1.0.10 无原生 run 头，全部无法精确归属到轮次。恢复后再次自检：
hook 加载、投递、版本、loopback 监听及 token 鉴权全部通过。
安装器在运行时配置旁留下本次卸载和重装的两份时间戳备份；插件最终保持启用。

## 能得出的结论

相对关闭组，两个启用组的整轮中位数分别高 **0.158s** 和 **0.236s**；这是观察到的差值，
**不能直接叫做 hook 的开销**。每组只有 6 个正式样本，三段顺序固定；区间明显重叠，
代理本身的请求耗时也在波动（各批含预热的中位数依次为 0.792s、0.826s、0.944s）。
外层耗时包含 YonWork 编排、模型、网络及采集链路；它没有单独计时 hook 回调。
这次对照提示可能存在百毫秒量级差异，尚不足以给出可归因的数值或上界。

**可以确认的功能效果**：加载 hook 时 14/14 请求精确归属；卸载后同一路由 7/7 未归属；
重新安装后归属恢复。这个对照没有覆盖工具续答、子代理、取消或约 167s 长流断流，
短请求没有复现断流也不代表故障已修复。

原始证据：上述三个 `results/<批次>/results.jsonl`、`model-requests.jsonl`，
以及 `scripts.install_trace_bridge verify --batch results/<启用批次>`。
