# Benchmark Companion

Benchmark Companion 是一个用于 WorkBuddy 桌面端人工 Benchmark 的 Windows 小工具。它不自动操作 WorkBuddy，只负责提示词、计时、NewAPI Token 查询、可靠落盘和 Excel 同步。

## 使用流程

1. 启动工具，确认工作簿和统计脚本路径。
2. 选择 `NewAPI` 或 `WorkBuddyDefault`，点击“开始新批次”。未计时时可直接选择另一个模型并开启新批次，无需逐条跳过当前批次。
3. 点击“复制提示词”，切到 WorkBuddy 粘贴并发送。
4. 发送完成后按全局 `F8` 开始计时。
5. 回答结束按 `F9`；异常按 `F10`；未开始的任务可点击“跳过”。
6. 工具先把结果写入本地 SQLite，再查询 Token 并同步 Excel。

工具会自动识别第一行前四列为 `CaseName`、`Prompt`、`Runs`、`Enabled` 的提示词 Sheet。默认使用 `Cases`，也可以在“新批次 Sheet”中选择 `long-text` 等其他兼容工作表；选择会被记住，并从下一个新批次开始生效。新增 Sheet 后点击“刷新”即可载入。

`APICalls=0` 会标记为 `NoData`，Token 数值不会以全 0 写进 Excel。可以点击“重新查询 Token”重试。Excel 被占用时结果仍保留在本地，关闭 Excel 后点击“同步待处理结果”即可。

## 数据文件

- 本地数据库：`data/benchmark_companion.db`
- 应用日志：`data/benchmark_companion.log`
- Excel 写入前每日备份：`data/backups/`
- 配置：`config.json`

Results 会在首次成功同步时扩展四列：`Status`、`TokenStatus`、`Note`、`RecordId`。`RecordId` 用于重试同步时防止重复记录。

`StartTime` 和 `EndTime` 以真正的 Excel 日期值保存，显示格式为 `2026/9/14 13:49`，仍可正常排序和筛选。

## 开发运行

```powershell
.\setup.ps1
.\run.ps1
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 打包

```powershell
.\build.ps1
```

打包结果位于 `dist\BenchmarkCompanion.exe`。请让 `config.json` 与 EXE 保持在同一目录。
