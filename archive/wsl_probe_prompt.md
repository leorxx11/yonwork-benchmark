# 任务：探明 YonWork 桌面端的可自动化入口

你运行在 Windows 上的 WSL2 里。目标应用 YonWork 是一个 **Windows 端 Electron 应用**，
装在 `D:\yonwork\`（WSL 路径 `/mnt/d/yonwork/`）。

我在做这个产品的基准测试自动化，现在用 Power Automate Desktop 驱动 UI，想换掉。
**请只做调查和只读分析，不要修改 `D:\yonwork` 下的任何文件，不要安装任何东西到该目录。**

---

## 环境约束（先读，踩过坑）

1. **WSL2 的 `127.0.0.1` 不是 Windows 的 loopback**。Chromium 调试端口只绑 Windows 侧的
   `127.0.0.1`，所以在 WSL 里 `curl 127.0.0.1:<port>` **必然连不上，这是假阴性，不能据此下结论**。
   凡是涉及端口探测、进程列举、启动/终止 GUI 进程的，一律通过 interop 在 Windows 侧执行：
   ```bash
   powershell.exe -NoProfile -Command "Get-Process YonWork | Select-Object Id,ProcessName"
   ```
2. **读文件反过来**：走 `/mnt/d/yonwork/` 原生读，比 interop 快很多。
3. **YonWork 有单实例锁**。已实测：已有实例在跑时，带命令行参数启动新进程，
   它读完 argv 打完日志就 `exiting duplicate process`，**参数被完全丢弃**。
   任何需要传参的验证，必须先确认没有任何 YonWork 进程存活（含托盘）。
4. 不要为了测试反复重启应用，每次重启前告诉我一声。

---

## 已知线索

正常启动时 stdout 里出现过这几行（原文）：

```
[plugin-registry] loaded channel extension plugins { "count": 1, "plugins": [ { "pluginId": "yon-im", "channelType": "zhiyou" } ] }
[YonWork] TLS verify disabled (YONCLAW_TLS_MODE=insecure); set YONCLAW_TLS_MODE=default-strict to restore
[external-cdp] fixed CDP port provided via argv; skipping appendSwitch
[renderer-accessibility-crash-guard] enabled { "platform": "win32", "override": null }
[YonWork] Another instance already holds the single-instance lock; exiting duplicate process
```

推断（需要你证实或推翻）：存在一个名为 `external-cdp` 的模块，且**不带 argv 时它会自己
appendSwitch 加一个 CDP 端口** —— 也就是默认启动可能本来就开着调试端口。
另外 `YONCLAW_*` 看起来是一个环境变量命名空间，可能还有别的开关。

`D:\yonwork\resources\` 下的目录：
`app.asar`(122MB) `app.asar.unpacked` `cli` `gateway` `context` `preset` `skills`
`tool-policy` `openclaw` `openclaw-plugins` `openclaw-workspace-templates` `python` `python-wheels` `reference` `meta` `bin`

`D:\yonwork\resources\cli\` 下：
`openclaw` `openclaw.cmd` `yonworkctl` `yonworkctl.cmd` `yonworkctl.mjs`(31563 bytes)

---

## 要回答的三个问题，按优先级

### Q1（最高价值）：有没有命令行入口能跑完一次完整对话？

读 `/mnt/d/yonwork/resources/cli/yonworkctl.mjs`（31KB 明文 JS）和两个 openclaw 脚本。

我要知道的是：**能不能从命令行发一条 prompt 给 agent，等它跑完，拿到结果**。
具体请回答：
- 有哪些子命令？各自的参数和用途？（把 `--help` 跑出来，或者从源码里把命令表整理出来）
- 有没有形如 `send` / `chat` / `run` / `task` / `exec` 的命令？
- 它和桌面应用是什么关系——独立进程，还是通过 IPC/HTTP 连到正在运行的 YonWork？
- 需要什么鉴权？token 从哪读？
- `openclaw` 又是什么，和 `yonworkctl` 什么分工？

**如果 Q1 成立，Q2/Q3 的优先级立刻下降**，因为命令行入口能完全绕开 UI。

### Q2：`external-cdp` 的行为

- 在源码里搜 `external-cdp`、`remote-debugging-port`、`appendSwitch`、`inspect`。
  源码位置：先看 `resources/app.asar.unpacked/`，要解 `app.asar` 的话，
  项目里有个 `extract_asar.py`（无需 Node）：
  `python3 extract_asar.py grep /mnt/d/yonwork/resources/app.asar -p external-cdp -p appendSwitch`
- 默认启动时是否真的会自己加 CDP 端口？端口号是**固定的还是随机的**？
  （固定才好用于跑批；随机的话查它会不会写 `DevToolsActivePort` 文件）
- 有没有环境变量能控制（`YONCLAW_*` 或其它）？
- **实测验证**（记得通过 powershell.exe，且先确认无残留进程）：
  正常启动后，列出 YonWork 各进程的 LISTEN 端口，逐个打 `/json/version`，
  看哪个返回 `{"Browser":"Chrome/..."}`，再打 `/json/list` 看有没有 `"type":"page"` 的 target。

### Q3：有没有本地 HTTP 服务

`resources/gateway` 是什么？有没有本地监听的 HTTP API（比如发消息、查会话、查用量）？
有的话给出端点和鉴权方式。

---

## 输出要求

写一份报告，包含：

1. **每个问题的结论**，以及支撑结论的**证据**（文件路径 + 行号 + 代码片段，或命令 + 实际输出）。
   区分清楚哪些是你**实测验证**的，哪些是**读源码推断**的。
2. **可复现命令清单**：我能直接复制去跑的那种。
3. **推荐路线**：基于你查到的东西，驱动 YonWork 跑基准测试最稳的方式是什么？
   按稳定性排序，说明每条路的前提条件和风险。
4. 没查出来的、不确定的，**明确说不知道**，不要猜。
