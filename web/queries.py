from __future__ import annotations

from typing import Any

from runner.db import connect
from runner.models import Verdict


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


def mode_summary(suite_id: str) -> list[dict[str, Any]]:
    """一行一个模式（产品 × 模型），不是一行一个批次。

    同一个模式可能跑过多个批次（重跑、补跑），横向对比时它们应该合并；
    按批次分组会让「四个模式」变成「N 个批次」，一眼看过去就错了。

    注意这里只做**聚合**，不做任何判定：verdict 是 assertions/ 早就算好的。
    """
    return _rows(
        """
        SELECT b.product, b.model_mode,
               MIN(b.model_ref) AS model_ref,
               COUNT(DISTINCT b.batch_id) AS batch_count,
               MIN(b.started_at) AS started_at,
               COUNT(r.benchmark_id) AS total,
               SUM(r.verdict = 'Pass')    AS pass_count,
               SUM(r.verdict = 'Fail')    AS fail_count,
               SUM(r.verdict = 'Timeout') AS timeout_count,
               SUM(r.verdict = 'Error')   AS error_count,
               SUM(r.verdict = 'Invalid') AS invalid_count,
               ROUND(AVG(r.duration_ms))  AS avg_ms,
               MAX(r.duration_ms)         AS max_ms,
               -- 耗时必须拆开看，否则就是拿冷启动跟常驻服务比：
               -- WorkBuddy 每轮起一个新进程（实测外层 16.7s / 内部 3.1s），
               -- YonWork 是常驻 Host API，engine_ms 恒为 NULL。
               ROUND(AVG(r.engine_ms))    AS avg_engine_ms,
               ROUND(AVG(r.duration_ms - r.engine_ms)) AS avg_startup_ms,
               COUNT(r.engine_ms)         AS engine_rows,
               -- 来源优先级和 models.USAGE_SOURCES 一致：
               -- 端上 HTTP → 会话 JSONL → WorkBuddy CLI → NewAPI 后台。
               -- ⚠️ 这里**必须覆盖每个产品的来源**。漏掉 workbuddy-cli 时
               -- WorkBuddy 那一行的 token 会整列显示成空，跟端上漏记（六-3）
               -- 长得一模一样——2026-09-21 第一次跑横向对比时就是这么撞上的。
               SUM(COALESCE(d.total_tokens, j.total_tokens, w.total_tokens, n.total_tokens))
                   AS total_tokens,
               SUM(d.cost_usd)            AS cost_usd,
               COUNT(d.id)                AS usage_rows
        FROM batches b
        LEFT JOIN runs r ON r.batch_id = b.batch_id
        LEFT JOIN usage_samples d
               ON d.benchmark_id = r.benchmark_id AND d.source = 'device-api'
        LEFT JOIN usage_samples j
               ON j.benchmark_id = r.benchmark_id AND j.source = 'session-jsonl'
        LEFT JOIN usage_samples w
               ON w.benchmark_id = r.benchmark_id AND w.source = 'workbuddy-cli'
        LEFT JOIN usage_samples n
               ON n.benchmark_id = r.benchmark_id AND n.source = 'newapi'
        WHERE b.suite_id = %s
        GROUP BY b.product, b.model_mode
        ORDER BY b.product, b.model_mode
        """,
        (suite_id,),
    )


def matrix(suite_id: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Case × 模式。返回 (模式列, 每个 Case 一行)。"""

    rows = _rows(
        """
        SELECT r.case_name, r.run_no, r.benchmark_id, r.verdict, r.duration_ms,
               CONCAT(b.product, ' / ', b.model_mode) AS mode_key,
               COALESCE(d.total_tokens, j.total_tokens, w.total_tokens, n.total_tokens)
                   AS total_tokens,
               CASE WHEN d.total_tokens IS NOT NULL THEN ''
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
