"""跑 S1–S7 场景集，落 JSONL，按预注册判据出结论。

判据、报告规则、场景分类全部定死在 `docs/history/client-probe-protocol.md`，
**那份文件在看到任何数据之前就定稿了**。这里只负责执行和套用，不再临时改标准。
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..case_catalog import load_case_set
from ..client import ChatClient, session_key_for
from ..discovery import HostEndpoint, discover
from .cdp import ProbeError
from .probe import ProbeRecord, probe_once


CASE_SET = "client-probe"

# 场景分类。**不放在 catalog.yaml 里**：那份文件是给主跑批用的用例定义，
# 「受不受控」是探针自己的方法论问题，混进去会让主跑批也要理解这个概念。
#
# 不受控 = 输出长度和结构取决于模型当轮行为，所以**不参与 P1/P3 的归一化比较**，
# 只看 Long Task 和 text_updates。长 Markdown 才压 renderer，
# 工具调用压的是 Agent，混在一起比会得出错误结论。
UNCONTROLLED = frozenset({"S5", "S6"})

# S7 要的就是「会话里已经有很长历史」，所以它不能用每轮新建会话那条路。
LONG_HISTORY = "S7"
LONG_HISTORY_TURNS = 6  # 先往舞台会话里灌这么多轮，再测


@dataclass(frozen=True, slots=True)
class Thresholds:
    """预注册判据。改这里等于改实验标准，要单独提交并说明改之前看过什么数据。"""

    first_lag_ms: float = 1000.0   # P1：各受控场景首字滞后中位数
    long_task_ms: float = 200.0    # P2：全场景 Long Task 最大值
    complexity_ratio: float = 3.0  # P3：归一化完成滞后的倍率


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _spread_is_too_wide(values: list[float]) -> bool:
    """(max − min) > 中位数 ⇒ 样本不足，不拿它下结论。

    ⚠️ **跨度为 0 一律算够稳**，哪怕中位数也是 0。
    原来写的是 `middle <= 0 or ...`，那是给滞后指标想的（中位 0 秒没意义），
    套到 Long Task 上就成了误报：`0/0/0` 表示三轮都没观测到主线程阻塞，
    是**最稳的结果**，却被判成样本不足。判得太松会漏掉问题，
    判得太严会把干净结论也一起废掉，两头都不可信。
    """
    if len(values) < 2:
        return True
    spread = max(values) - min(values)
    if spread == 0:
        return False
    return spread > statistics.median(values)


def build_long_history(
    endpoint: HostEndpoint, turns: int = LONG_HISTORY_TURNS
) -> tuple[str, str]:
    """养一个长历史会话，返回 (sessionKey, 侧边栏标题)。

    S7 的变量只有「历史多长」，所以灌历史用的 prompt 要短且便宜，
    不能让它自己变成渲染负担。
    """
    stamp = f"{int(time.time())}"
    key = session_key_for(f"s7-stage-{stamp}")
    title = f"探针长历史 {stamp}"
    for index in range(turns):
        client = ChatClient(endpoint, timeout_seconds=120)
        client.send(
            benchmark_id=f"s7-stage-{stamp}-{index}",
            prompt=(f"{title}\n请只回复：OK" if index == 0 else "请只回复：OK"),
            session_key=key,
        )
    return key, title


def run_scenarios(
    *,
    catalog: Path = Path("cases/catalog.yaml"),
    only: Iterable[str] | None = None,
    out_dir: Path = Path("results/client-probe"),
    endpoint: HostEndpoint | None = None,
) -> Path:
    """按 catalog 里的 client-probe 用例集逐个跑，一轮一行落 JSONL。

    **逐轮落盘**，和主跑批同一个理由：跑到一半崩了，已经烧掉的 token 不能白费。
    字段形状对齐 `results.jsonl`（`benchmark_id` 等），将来想入库直接复用 `ingest.py`。
    """
    endpoint = endpoint or discover()
    case_set = load_case_set(catalog, CASE_SET)
    wanted = set(only) if only else None

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"probe-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"

    long_key: str | None = None
    long_title: str | None = None
    for case in case_set.cases:
        if wanted and case.case_name not in wanted:
            continue
        if case.case_name == LONG_HISTORY and long_key is None:
            print(f"  养 {LONG_HISTORY_TURNS} 轮长历史会话…", flush=True)
            long_key, long_title = build_long_history(endpoint)

        for run_no in range(1, case.runs + 1):
            benchmark_id = f"probe-{case.case_name}-r{run_no}-{int(time.time())}"
            label = f"{case.case_name}#{run_no}"
            try:
                if case.case_name == LONG_HISTORY:
                    # 必须显式把长历史会话点开：auto=False 的 fast-fail 只验
                    # 「有会话开着」，不验是哪一个，不点就会静默测到别的会话上。
                    record = probe_once(
                        benchmark_id=benchmark_id, prompt=case.prompt,
                        scenario=case.case_name, session_key=long_key,
                        endpoint=endpoint, auto=False, open_title=long_title,
                    )
                else:
                    record = probe_once(
                        benchmark_id=benchmark_id, prompt=case.prompt,
                        scenario=case.case_name, endpoint=endpoint,
                    )
            except BaseException as exc:  # noqa: BLE001
                # 一轮坏掉只作废这一轮，别拖垮整个场景集——和 batch.py 同一条规矩，
                # 也同样保留 Ctrl-C 的出路。
                #
                # 这里**必须兜得够宽**：原来只接 (ProbeError, OSError)，
                # 结果 S3 抛的是 `websockets.ConnectionClosedError`（不是 OSError），
                # 整个场景集在第 7 轮就整批中止了——正是 PAD 时代第 1 条问题的形状。
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                print(f"  {label:8} 作废：{type(exc).__name__}: {exc}", flush=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(
                        {"benchmark_id": benchmark_id, "scenario": case.case_name,
                         "run_no": run_no,
                         "failed": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False) + "\n")
                continue

            payload = record.as_json()
            payload["run_no"] = run_no
            payload.pop("events", None)  # 原始事件太占地方，指标已经折算完了
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            print(
                f"  {label:8} 首字滞后 {record.ui_lag_first_ms}ms"
                f" / 完成滞后 {record.ui_lag_complete_ms}ms"
                f" / {record.answer_chars} 字 / {record.text_updates} 次更新"
                f" / LongTask {record.long_task_count}×{record.long_task_max_ms}ms"
                f" / 工具 {record.tool_calls}"
                + (f"  ⚠ {'；'.join(record.notes)}" if record.notes else ""),
                flush=True,
            )
    return path


def summarize(path: Path, thresholds: Thresholds = Thresholds()) -> dict[str, Any]:
    """按预注册判据出结论。**报中位数 + 最小/最大，不报均值。**"""
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    scenarios: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("failed"):
            continue
        scenarios.setdefault(row["scenario"], []).append(row)

    report: dict[str, Any] = {"scenarios": {}, "verdict": {}, "failed": [
        r for r in rows if r.get("failed")
    ]}

    def column(items: list[dict[str, Any]], key: str) -> list[float]:
        return [r[key] for r in items if isinstance(r.get(key), (int, float))]

    for name, items in sorted(scenarios.items()):
        first = column(items, "ui_lag_first_ms")
        complete = column(items, "ui_lag_complete_ms")
        chars = column(items, "answer_chars")
        tasks = column(items, "long_task_max_ms")
        # 归一化：渲染 1000 字的额外开销。用**完成**滞后，不是首字滞后——
        # 首字跟内容多少基本无关，除不出意义。
        per_1k = [
            c / (n / 1000) for c, n in
            zip(complete, chars) if n and n > 0
        ]
        report["scenarios"][name] = {
            "runs": len(items),
            "uncontrolled": name in UNCONTROLLED,
            "first_lag_ms": _stat(first),
            "complete_lag_ms": _stat(complete),
            "complete_lag_per_1k": _stat(per_1k),
            "answer_chars": _stat(chars),
            "text_updates": _stat(column(items, "text_updates")),
            "long_task_max_ms": _stat(tasks),
            "long_task_count": _stat(column(items, "long_task_count")),
            "tool_calls": _stat(column(items, "tool_calls")),
            "history_turns": _stat(column(items, "history_turns")),
            "sample_too_thin": _spread_is_too_wide(first),
        }

    controlled = {
        name: data for name, data in report["scenarios"].items()
        if not data["uncontrolled"]
    }
    # P1：只用受控场景，且跳过样本不足的（不足的照实标出来，不拿来下结论）
    p1_usable = {
        name: data for name, data in controlled.items()
        if not data["sample_too_thin"] and data["first_lag_ms"]["median"] is not None
    }
    p1_bad = {
        name: data["first_lag_ms"]["median"] for name, data in p1_usable.items()
        if data["first_lag_ms"]["median"] >= thresholds.first_lag_ms
    }
    tasks = {
        name: data["long_task_max_ms"] for name, data in report["scenarios"].items()
        if data["long_task_max_ms"]["max"] is not None
    }
    p2_worst = max((d["max"] for d in tasks.values()), default=None)
    # Long Task 是绝对量，样本不足时**不能判通过**——那等于拿一个还没稳定的
    # 数去下「不是瓶颈」这种结论。宁可记 inconclusive，也不要一个好看的 pass。
    p2_thin = sorted(name for name, d in tasks.items() if d["thin"])

    ratios = [
        data["complete_lag_per_1k"]["median"] for data in controlled.values()
        if data["complete_lag_per_1k"]["median"] is not None
    ]
    p3_ratio = (max(ratios) / min(ratios)) if len(ratios) >= 2 and min(ratios) > 0 else None

    report["verdict"] = {
        "P1_first_lag": {
            "threshold_ms": thresholds.first_lag_ms,
            "violations": p1_bad,
            "skipped_thin": [n for n, d in controlled.items() if d["sample_too_thin"]],
            "pass": not p1_bad and bool(p1_usable),
        },
        "P2_long_task": {
            "threshold_ms": thresholds.long_task_ms,
            "worst_ms": p2_worst,
            "thin_scenarios": p2_thin,
            "inconclusive": bool(p2_thin),
            "pass": (
                not p2_thin
                and p2_worst is not None
                and p2_worst < thresholds.long_task_ms
            ),
        },
        "P3_complexity": {
            "threshold_ratio": thresholds.complexity_ratio,
            "ratio": round(p3_ratio, 2) if p3_ratio else None,
            "pass": p3_ratio is not None and p3_ratio < thresholds.complexity_ratio,
        },
    }
    report["verdict"]["renderer_not_a_bottleneck"] = all(
        report["verdict"][key]["pass"] for key in ("P1_first_lag", "P2_long_task", "P3_complexity")
    )
    return report


def _stat(values: list[float]) -> dict[str, Any]:
    """中位数 + 最小/最大 + 样本是否够。**不给均值**——本项目的分布上均值没有意义。

    `thin` **逐指标算**，不是整场景一个标志。2026-09-21 就栽在这：
    原来只对 `first_lag_ms` 判样本不足，结果 S4 的 Long Task 是 182/88/79
    （中位 88、跨度 103），按协议早该标样本不足，却被当成「最差 182ms」
    写进了结论——而它恰恰是唯一支撑「renderer 不是瓶颈」的那个数。
    协议里写明「绝对量不抵消，必须报区间」，最需要这条检查的就是它。
    """
    if not values:
        return {"median": None, "min": None, "max": None, "n": 0, "thin": True}
    return {
        "median": round(_median(values) or 0, 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "n": len(values),
        "thin": _spread_is_too_wide(values),
    }
