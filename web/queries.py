from __future__ import annotations

from typing import Any
from statistics import median

from runner.db import connect
from runner.models import USAGE_SOURCES, Verdict


VERDICTS = [item.value for item in Verdict]


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())


def _row(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    found = _rows(sql, params)
    return found[0] if found else None


def list_suites() -> list[dict[str, Any]]:
    return _rows(
        """
        SELECT s.suite_id, s.name, s.created_at,
               COUNT(DISTINCT b.batch_id) AS batch_count,
               COUNT(r.benchmark_id)      AS run_count,
               SUM(r.verdict <> 'Pass')   AS bad_count
        FROM suite_runs s
        LEFT JOIN batches b ON b.suite_id = s.suite_id
        LEFT JOIN runs r    ON r.batch_id = b.batch_id
        GROUP BY s.suite_id, s.name, s.created_at
        ORDER BY s.created_at DESC
        """
    )


def get_suite(suite_id: str) -> dict[str, Any] | None:
    return _row("SELECT * FROM suite_runs WHERE suite_id = %s", (suite_id,))


def _distribution(values: list[Any]) -> dict[str, Any]:
    known = [value for value in values if value is not None]
    return {
        "n": len(known), "median": median(known) if known else None,
        "min": min(known) if known else None, "max": max(known) if known else None,
    }


def mode_summary(suite_id: str) -> list[dict[str, Any]]:
    """按请求模式描述已有观测。用量按来源分列，绝不逐轮回落后混加。"""
    runs = _rows(
        """
        SELECT b.product, b.model_mode, b.model_ref, b.batch_id, b.started_at,
               r.benchmark_id, r.case_name, r.verdict, r.duration_ms, r.engine_ms,
               r.model_calls_status
        FROM batches b LEFT JOIN runs r ON r.batch_id = b.batch_id
        WHERE b.suite_id = %s
        ORDER BY b.product, b.model_mode, b.batch_id, r.position
        """, (suite_id,),
    )
    samples = _rows(
        """
        SELECT u.benchmark_id, u.source, u.total_tokens, u.cost_usd, u.model
        FROM usage_samples u JOIN runs r ON r.benchmark_id = u.benchmark_id
        JOIN batches b ON b.batch_id = r.batch_id
        WHERE b.suite_id = %s
        """, (suite_id,),
    )
    # 未归属请求挂在批次上而不是某一轮（绝不按时间窗硬塞给一轮），所以按 batch_id 取。
    requests = _rows(
        """
        SELECT m.batch_id, m.benchmark_id, m.attribution, m.termination,
               m.usage_status, m.input_tokens, m.output_tokens
        FROM model_requests m JOIN batches b ON b.batch_id = m.batch_id
        WHERE b.suite_id = %s
        """, (suite_id,),
    )
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault((run["product"], run["model_mode"]), []).append(run)
    result = []
    for (product, mode), rows in groups.items():
        ids = {r["benchmark_id"] for r in rows if r["benchmark_id"] is not None}
        measured = [r for r in rows if r["benchmark_id"] is not None]
        usage = [sample for sample in samples if sample["benchmark_id"] in ids]
        sources = list(USAGE_SOURCES) + sorted({u["source"] for u in usage} - set(USAGE_SOURCES))
        breakdown = []
        for source in sources:
            selected = [u for u in usage if u["source"] == source]
            if not selected:
                continue
            tokens = [u["total_tokens"] for u in selected if u["total_tokens"] is not None]
            costs = [u["cost_usd"] for u in selected if u["cost_usd"] is not None]
            breakdown.append({
                "source": source, "token_rows": len(tokens),
                "total_tokens": sum(tokens) if tokens else None,
                "cost_rows": len(costs), "cost_usd": sum(costs) if costs else None,
                "models": sorted({u["model"] for u in selected if u["model"]}),
            })
        refs = sorted({r["model_ref"] for r in rows if r["model_ref"]})
        row = {
            "product": product, "model_mode": mode, "model_ref": " / ".join(refs),
            "batch_count": len({r["batch_id"] for r in rows}), "total": len(ids),
            "case_count": len({r["case_name"] for r in measured}),
            "usage_rows": len({u["benchmark_id"] for u in usage if u["total_tokens"] is not None}),
            "wall": _distribution([r["duration_ms"] for r in measured]),
            "internal": _distribution([r["engine_ms"] for r in measured]),
            "gap": _distribution([
                r["duration_ms"] - r["engine_ms"] for r in measured
                if r["duration_ms"] is not None and r["engine_ms"] is not None
            ]),
            "usage_sources": breakdown,
            "model_calls": _model_call_block(
                measured,
                [r for r in requests if r["batch_id"] in {x["batch_id"] for x in rows}],
            ),
        }
        for verdict in VERDICTS:
            row[verdict.lower() + "_count"] = sum(r["verdict"] == verdict for r in measured)
        result.append(row)
    return result


ATTRIBUTIONS = ("attributed", "late", "unattributed", "rejected")


def _model_call_block(runs: list[dict[str, Any]], requests: list[dict[str, Any]]) -> dict[str, Any]:
    """逐请求采集的覆盖情况。**没开采集、开着没采到、真的 0 次是三件事。**

    token 只把 `usage_status = observed` 的加起来，并同时给出覆盖数，
    所以这是「已观测小计」，不是整轮 token；一条都没观测到时给 None 而不是 0。
    """
    statuses = [r.get("model_calls_status") or "disabled" for r in runs]
    observed_usage = [r for r in requests if r.get("usage_status") == "observed"]
    counted = [r for r in requests if r.get("attribution") in ("attributed", "late")]
    inputs = [r["input_tokens"] for r in observed_usage if r.get("input_tokens") is not None]
    outputs = [r["output_tokens"] for r in observed_usage if r.get("output_tokens") is not None]
    return {
        "runs_observed": statuses.count("observed"),
        "runs_unavailable": statuses.count("unavailable"),
        "runs_disabled": statuses.count("disabled"),
        "requests": len(requests),
        **{name: sum(r.get("attribution") == name for r in requests) for name in ATTRIBUTIONS},
        "failed": sum(
            1 for r in requests
            if r.get("termination") not in (None, "completed")
        ),
        "usage_observed": len(observed_usage),
        "usage_missing": len(counted) - len([r for r in counted if r.get("usage_status") == "observed"]),
        "input_tokens_observed": sum(inputs) if inputs else None,
        "output_tokens_observed": sum(outputs) if outputs else None,
        # 网关内部重试看不见，整轮的上游尝试数只能是未知。
        "upstream_attempts": None,
    }


def matrix(suite_id: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Case × 模式。返回 (模式列, 每个 Case 一行)。"""

    rows = _rows(
        """
        SELECT r.case_name, r.run_no, r.benchmark_id, r.verdict, r.duration_ms,
               CONCAT(b.product, ' / ', b.model_mode) AS mode_key,
               COALESCE(d.total_tokens, j.total_tokens, w.total_tokens, n.total_tokens)
                   AS total_tokens,
               CASE WHEN d.total_tokens IS NOT NULL THEN '端上'
                    WHEN j.total_tokens IS NOT NULL THEN '会话'
                    WHEN w.total_tokens IS NOT NULL THEN 'CLI'
                    WHEN n.total_tokens IS NOT NULL THEN '后台'
                    ELSE '' END AS tokens_source_label
        FROM runs r
        JOIN batches b ON b.batch_id = r.batch_id
        LEFT JOIN usage_samples d
               ON d.benchmark_id = r.benchmark_id AND d.source = 'device-api'
        LEFT JOIN usage_samples j
               ON j.benchmark_id = r.benchmark_id AND j.source = 'session-jsonl'
        LEFT JOIN usage_samples w
               ON w.benchmark_id = r.benchmark_id AND w.source = 'workbuddy-cli'
        LEFT JOIN usage_samples n
               ON n.benchmark_id = r.benchmark_id AND n.source = 'newapi'
        WHERE b.suite_id = %s
        ORDER BY r.case_name, r.run_no
        """,
        (suite_id,),
    )

    modes: list[str] = []
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        mode = row["mode_key"]
        if mode not in modes:
            modes.append(mode)
        key = (f"{row['case_name']}#{row['run_no']}", mode)
        cells.setdefault(key, []).append(row)

    case_keys = sorted({key[0] for key in cells})
    table = [
        {"case": case, "cells": [cells.get((case, mode), []) for mode in modes]}
        for case in case_keys
    ]
    return modes, table


def get_run(benchmark_id: str) -> dict[str, Any] | None:
    return _row(
        """
        SELECT r.*, b.product, b.model_mode, b.model_ref, b.suite_id, s.name AS suite_name
        FROM runs r
        JOIN batches b ON b.batch_id = r.batch_id
        LEFT JOIN suite_runs s ON s.suite_id = b.suite_id
        WHERE r.benchmark_id = %s
        """,
        (benchmark_id,),
    )


def get_checks(benchmark_id: str) -> list[dict[str, Any]]:
    # 按五层的固有顺序排，不按字母序——这是断言的执行顺序。
    return _rows(
        """
        SELECT layer, name, verdict, detail
        FROM checks WHERE benchmark_id = %s
        ORDER BY FIELD(layer,'Completion','Artifact','Log','Content','Cost'), name
        """,
        (benchmark_id,),
    )


def get_usage(benchmark_id: str) -> list[dict[str, Any]]:
    return _rows(
        "SELECT * FROM usage_samples WHERE benchmark_id = %s ORDER BY source",
        (benchmark_id,),
    )


def get_model_requests(benchmark_id: str) -> dict[str, Any]:
    """这一轮的逐请求时间线，外加同批次**未归属**的请求。

    未归属请求单列，不并进这一轮的计数——它属于这个批次但不属于任何一轮，
    按时间窗硬塞给某一轮正是这套账本要消灭的东西。
    """
    owned = _rows(
        "SELECT * FROM model_requests WHERE benchmark_id = %s ORDER BY sequence",
        (benchmark_id,),
    )
    orphans = _rows(
        """
        SELECT m.* FROM model_requests m
        WHERE m.benchmark_id IS NULL
          AND m.batch_id = (SELECT batch_id FROM runs WHERE benchmark_id = %s)
        ORDER BY m.sequence
        """,
        (benchmark_id,),
    )
    return {"requests": owned, "orphans": orphans, "timing": _request_timing(owned)}


def _request_timing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """请求耗时之和，以及它**能不能**跟整轮耗时比。

    两条都要说清楚，否则这个数会被当成「整轮里模型占了多久」：

    - 请求之间有产品自己的处理时间，所以**之和永远小于整轮**，差值不是任何单一成因。
    - 请求如果在时间上重叠（并发），相加就更没有意义——那会把同一段墙钟算两遍。
      这里真的去查重叠，而不是假设串行。
    """
    durations = [r["duration_ms"] for r in rows if r.get("duration_ms") is not None]
    spans = sorted(
        (r["received_at"], r["ended_at"]) for r in rows
        if r.get("received_at") and r.get("ended_at")
    )
    overlapping = any(
        later[0] < earlier[1] for earlier, later in zip(spans, spans[1:])
    )
    return {
        "sum_ms": sum(durations) if durations else None,
        "measured": len(durations),
        "total": len(rows),
        "overlapping": overlapping,
        # 有一条没测到时长，「之和」就不是完整的和，得说出来。
        "partial": len(durations) < len(rows),
    }


def reconcile(suite_id: str) -> list[dict[str, Any]]:
    """端上 vs 后台。缺哪一侧就是哪一侧漏记——这正是要抓的东西。"""
    return _rows(
        """
        SELECT r.benchmark_id, r.case_name, r.run_no, r.verdict,
               b.product, b.model_mode,
               d.total_tokens AS device_total, d.input_tokens AS device_input,
               n.total_tokens AS newapi_total, n.input_tokens AS newapi_input,
               j.total_tokens AS jsonl_total, j.input_tokens AS jsonl_input,
               j.cache_read_tokens AS jsonl_cache_read, j.model AS jsonl_model,
               d.matched_by  AS device_match
        FROM runs r
        JOIN batches b ON b.batch_id = r.batch_id
        LEFT JOIN usage_samples d ON d.benchmark_id = r.benchmark_id AND d.source = 'device-api'
        LEFT JOIN usage_samples n ON n.benchmark_id = r.benchmark_id AND n.source = 'newapi'
        LEFT JOIN usage_samples j ON j.benchmark_id = r.benchmark_id AND j.source = 'session-jsonl'
        WHERE b.suite_id = %s
        ORDER BY r.started_at
        """,
        (suite_id,),
    )
