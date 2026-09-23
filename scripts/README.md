# scripts —— 一次性 / 辅助工具

不在跑批链路上，但各自解决一个具体问题。

| 脚本 | 什么时候用 | 跑在哪 |
|---|---|---|
| `install_docker_wsl.sh` | 新机器装 Docker Engine。跑一次，重复跑无害 | WSL，要 sudo |
| `newapi_stats.ps1` | 手动拉 NewAPI 后台用量核对 | Windows PowerShell |
| `install_trace_bridge.py` | 装/卸/核对 `plugins/benchmark-trace-bridge`；YonWork 更新或重启后跑 `verify`（加 `--batch` 核对一批的归属）。改完重启 YonWork（别从 WSL 拉起） | WSL |
| `extract_asar.py` | 查 YonWork 源码：解包 / 列目录 / 带上下文 grep `app.asar` | WSL，无需 Node |

## newapi_stats.ps1 不是冗余

`/api/usage/recent-token-history` 是**端上**数据，这个脚本拉的是 **NewAPI 后台**数据。
两端按同一轮次比对，正好是「后台与端上不一致」那个系统性问题的检测工具。

日常对账已经进了 `runner/reconcile.py`（口径一致：消费 + 错误都算一次调用），
这个脚本留作手工核对和交叉验证——它不依赖我们自己的任何代码，
所以当 runner 的数字可疑时，用它来判断到底是谁错了。

**它要 `NEWAPI_ACCESS_TOKEN` 环境变量**，不再硬编码 token：

```powershell
$env:NEWAPI_ACCESS_TOKEN = "<令牌>"
.\newapi_stats.ps1 -StartTime "2026-09-20 21:00:00" -EndTime "2026-09-20 21:10:00"
```

从 WSL 调它要带 `WSLENV`，否则 Windows 进程收不到这个变量（CLAUDE.md 坑 3）：

```bash
WSLENV=NEWAPI_ACCESS_TOKEN NEWAPI_ACCESS_TOKEN=<令牌> \
  powershell.exe -NoProfile -File scripts/newapi_stats.ps1
```

## extract_asar.py

```bash
python3 scripts/extract_asar.py list    /mnt/d/yonwork/resources/app.asar --limit 40
python3 scripts/extract_asar.py extract /mnt/d/yonwork/resources/app.asar -o ./yonwork-asar
python3 scripts/extract_asar.py grep    /mnt/d/yonwork/resources/app.asar -p external-cdp
```

查产品行为时这是唯一可靠的一手来源——`modelSelection` 到底要哪两个字段，
就是在这里 grep `/api/chat/apply-model` 的处理函数查出来的。
**只读，不要改 `D:\yonwork\` 下任何文件。**
