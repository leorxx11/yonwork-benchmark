# cases —— 提示词清单

跑批的输入源。`runner` 只读不写：结果落 JSONL，不再往表里回写。

```
yonwork_benchmark.xlsx
  ├─ Cases       ← 主用例表，runner 默认读这张
  ├─ long-text   ← 长文本用例，单独一张表（--sheet long-text）
  └─ Results     ← PAD 时代的结果回写表，**已停用**，留着只为看历史数据
fixtures/
  └─ 西游记[lunarora.com].txt   ← long-text 用例要 YonWork 去读的那个文件（2.3 MB）
```

表结构和可选的断言参数列见 `runner/README.md`。

## 两个已知的坑

1. **`long-text` 表里的路径是旧机器的**，写死成
   `C:\Users\Administrator\Desktop\benchmark\doc\西游记[lunarora.com].txt`。
   现在文件在 `cases/fixtures/` 下，而且开发机已经换成 WSL 了。
   这条用例**当前跑不通**——改之前得先确认 YonWork 的文件工具认不认
   `\\wsl.localhost\...` 这种 UNC 路径，认不认再决定是改提示词还是把 fixture 拷到 Windows 侧。
2. **`Results` 表不要再当结果来源。** 里面是 2026-09-11 前后 PAD 跑出来的数据，
   口径和现在不一样（那会儿还没有失败分类、没有断言）。新数据在 MySQL 和 `results/*.jsonl` 里。
