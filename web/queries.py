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
               r.benchmark_id, r.case_name, r.verdict, r.duration_ms, r.engine_ms
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
        }
        for verdict in VERDICTS:
            row[verdict.lower() + "_count"] = sum(r["verdict"] == verdict for r in measured)
        result.append(row)
    return result


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
