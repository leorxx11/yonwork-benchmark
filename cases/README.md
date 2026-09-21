# cases —— 版本化测试用例

`catalog.yaml` 是主用例源。它是普通文本，能正常做 Git diff、评审、分支合并和版本回退，
也不会像二进制 Excel 那样依赖某台机器上的编辑器。

```text
catalog.yaml
  case_sets:
    smoke       # 快速验证链路
    long-text   # 文件读取和长上下文

fixtures/
  西游记[lunarora.com].txt

yonwork_benchmark.xlsx
  旧数据和 CLI 兼容输入，不再是默认来源
```

完整字段说明见 [runner/README.md](../runner/README.md)。新增用例时直接编辑 `catalog.yaml`；
未知字段、重复名称、非法 runs 或断言类型会在提交任务前报错。

## 本机路径

YonWork 是 Windows 进程，因此文件类 prompt 不能直接引用容器内或 WSL 内路径。catalog 使用：

```yaml
prompt: 请读取 ${YONWORK_XIYOUJI_PATH} 并完成摘要。
```

`scripts/bootstrap.sh` 会把 fixture 复制到 Windows Documents，并把该 Windows 路径写入 `.env`。
直接运行 CLI 时，需要自行设置同名环境变量。若变量缺失，runner 会明确报错，不会把占位符原样
发给模型。

## 结果放在哪里

runner 只读用例，不向 catalog 或 Excel 回写。逐轮事实来源是 `results/*/results.jsonl`，汇总结果
在 SQLite、XLSX 和 MySQL 中。旧工作簿的 `Results` 页仅用于查看 PAD 时代历史数据，不能与当前
口径混用。
