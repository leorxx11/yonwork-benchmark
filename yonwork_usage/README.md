# YonworkUsageCollector

`yonwork_usage.py` 是一个 Python 3.10+ 单文件 CLI，用显式传入的 YonWork `llm-observer` JSONL 日志采集串行 Benchmark Run 的 Token 使用量。它只读取指定日志，不查找 profile、不扫描日志目录、不处理跨天切换，也没有后台监听器。

## 使用

Benchmark 开始前记录当前字节偏移：

```powershell
python yonwork_usage.py begin `
  --benchmark-id "Case01-Run1" `
  --log-path "C:\Users\Administrator\AppData\Roaming\yonwork\profiles\47jm5\userData\runtime\openclaw\llm-observer\llm-observer-2026-09-15.jsonl"
```

状态保存在脚本旁的 `.state/<benchmark-id>.json`。任务结束后采集：

```powershell
python yonwork_usage.py collect --benchmark-id "Case01-Run1"
```

默认最多轮询 10 秒，每 500 ms 只从上次文件位置继续读取。可覆盖超时：

```powershell
python yonwork_usage.py collect --benchmark-id "Case01-Run1" --timeout 10
```

成功时 stdout 只有一行紧凑 JSON，可直接交给 Power Automate：

```json
{"success":true,"benchmarkId":"Case01-Run1","runId":"e3232b0b-958b-4b54-8d94-372c47c127f1","sessionId":"63bb7bd1-250b-4af7-b932-fef47e695b5c","provider":"yonyou-default","model":"deepseek-v4-flash","harness":"openclaw","runInputTokens":336497,"runOutputTokens":3398,"runTotalTokens":339895,"finalInputTokens":42645,"finalOutputTokens":1026,"finalTotalTokens":43671,"tokenAmplification":7.78,"inputTs":"2026-09-15T06:48:37.655Z","outputTs":"2026-09-15T06:49:44.596Z"}
```

CLI 先选择保存偏移之后第一个满足以下条件的 `llm_input`：

- `agentId == "main"`
- `workspaceDir` 以 `userData\workspaces\default` 结尾
- 不是 cron、team-personal、team-chat-summary 或 openclaw-weixin 会话

之后只接受 `runId` 完全相同的 `llm_output`。`run*Tokens` 直接来自累计值 `output.usage`；`final*Tokens` 来自 `output.lastAssistant.usage`，不会自行累加日志记录。

## Inspect

`inspect` 只读完整的指定日志：

```powershell
python yonwork_usage.py inspect --log-path "C:\path with spaces\llm-observer-2026-09-15.jsonl"
```

结果包含 input/output 记录数、唯一和已配对 run 数、未配对 runId，以及按唯一 runId 统计的 `normal`、`cron`、`team-chat`、`weixin`、`other` 类型：

```json
{"success":true,"llmInputCount":16,"llmOutputCount":16,"uniqueRunIds":16,"pairedRunCount":16,"unpairedInputRunIds":[],"unpairedOutputRunIds":[],"typeCounts":{"normal":16,"cron":0,"team-chat":0,"weixin":0,"other":0}}
```

## 错误和退出码

成功退出码为 `0`，失败为非 `0`。无论成功或失败，stdout 都只有一行合法 JSON；诊断信息写入 stderr。主要错误码：

- `LOG_FILE_NOT_FOUND`
- `STATE_NOT_FOUND`
- `INPUT_NOT_FOUND`
- `OUTPUT_TIMEOUT`
- `INVALID_JSONL`
- `USAGE_NOT_FOUND`

日志末尾尚未写完且没有换行的半行会保留并在轮询中等待完成，不会立即当作损坏记录。已经换行但不是合法 JSON 对象的记录返回 `INVALID_JSONL`。

## 测试

测试只使用临时 JSONL，不读取或修改真实 YonWork 文件：

```powershell
python -m unittest discover -s tests -v
```
