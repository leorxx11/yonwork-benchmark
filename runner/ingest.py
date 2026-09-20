from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .db import DatabaseError, connect, to_millis, to_utc
from .models import JsonObject, now_iso
from .report import ReportError, iter_jsonl, usage_samples_of


DEFAULT_PRODUCT = "yonwork"
DEFAULT_MODEL_MODE = "default"

# 端上 /api/usage/recent-token-history。另外两个来源（session-jsonl、newapi）
# 由 P2 的采集补进同一张表，这里先把 schema 走通。
DEVICE_SOURCE = "device-api"


class IngestError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BatchMeta:
    batch_id: str
    product: str = DEFAULT_PRODUCT
    model_mode: str = DEFAULT_MODEL_MODE
    model_ref: str = ""
    agent_id: str = "main"
    app_version: str = ""
    suite_id: str | None = None


@dataclass(frozen=True, slots=True)
class IngestResult:
    batch_id: str
    runs: int
    checks: int
    usage_samples: int


def suite_id_for(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]


def _infer_model_mode(records: list[JsonObject]) -> tuple[str, str]:
    """没显式指定时，从记录里的 requested_model 推。

    'default' 是保留值，表示没传 modelSelection、跑的是智能体当前默认模型。
    """
    pairs = {
        (
            (record.get("turn") or {}).get("requested_model_label")
            or (record.get("turn") or {}).get("requested_model"),
            (record.get("turn") or {}).get("requested_model"),
        )
        for record in records
        if isinstance(record.get("turn"), dict)
    }
    pairs = {pair for pair in pairs if pair[1]}
    if not pairs:
        return DEFAULT_MODEL_MODE, ""
    if len(pairs) > 1:
        raise IngestError(
            f"同一批次里出现了多个请求模型：{sorted(pairs)}；"
            "一个批次只能是一个 (product, model_mode) 组合"
        )
    label, model_id = pairs.pop()
    return label, model_id


def _batch_window(records: list[JsonObject]) -> tuple[Any, Any]:
    starts = [to_utc((r.get("turn") or {}).get("started_at")) for r in records]
    ends = [to_utc((r.get("turn") or {}).get("ended_at")) for r in records]
    starts = [item for item in starts if item]
    ends = [item for item in ends if item]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _usage_rows(record: JsonObject) -> list[tuple[Any, ...]]:
    """一个来源一行。三来源哪个缺就是哪一侧漏记，缺的那行不补。"""
    stats = record.get("log_stats") or {}
    return [
        _usage_row(record, usage, stats) for usage in usage_samples_of(record)
    ]


def _usage_row(record: JsonObject, usage: JsonObject, stats: Any) -> tuple[Any, ...]:
    return (
        record.get("benchmark_id"),
        usage.get("source") or DEVICE_SOURCE,
        usage.get("model"),
        usage.get("provider"),
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
        usage.get("cache_read_tokens"),
        usage.get("cache_write_tokens"),
        usage.get("cost_usd"),
        stats.get("api_calls") if isinstance(stats, dict) else None,
        stats.get("error_calls") if isinstance(stats, dict) else None,
        usage.get("match", "none"),
        to_utc(usage.get("timestamp")),
        json.dumps(usage, ensure_ascii=False),
    )


def _run_row(record: JsonObject, batch_id: str) -> tuple[Any, ...]:
    turn = record.get("turn") if isinstance(record.get("turn"), dict) else {}
    tool_calls = turn.get("tool_calls") or []
    answer = turn.get("answer")
    return (
        record.get("benchmark_id"),
        batch_id,
        record.get("position", 0),
        record.get("case_name", ""),
        record.get("run_no", 1),
        record.get("prompt", ""),
        record.get("session_key", ""),
        turn.get("run_id"),
        record.get("verdict", "Error"),
        turn.get("requested_model"),
        to_millis(turn.get("duration_seconds")),
        to_millis(turn.get("first_delta_seconds")),
        turn.get("terminated_by"),
        turn.get("stop_reason"),
        len(tool_calls) if isinstance(tool_calls, list) else None,
        answer if isinstance(answer, str) else None,
        turn.get("transcript_path"),
        record.get("note", "") or "",
        to_utc(turn.get("started_at")),
        to_utc(turn.get("ended_at")),
        to_utc(record.get("created_at")) or to_utc(now_iso()),
    )


RUN_SQL = """
INSERT INTO runs (benchmark_id, batch_id, position, case_name, run_no, prompt,
    session_key, run_id, verdict, requested_model, duration_ms, first_delta_ms,
    terminated_by, stop_reason, tool_call_count, answer_preview, transcript_path,
    note, started_at, ended_at, created_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    batch_id=VALUES(batch_id), verdict=VALUES(verdict),
    requested_model=VALUES(requested_model), duration_ms=VALUES(duration_ms),
    first_delta_ms=VALUES(first_delta_ms), terminated_by=VALUES(terminated_by),
    stop_reason=VALUES(stop_reason), tool_call_count=VALUES(tool_call_count),
    answer_preview=VALUES(answer_preview), transcript_path=VALUES(transcript_path),
    note=VALUES(note), started_at=VALUES(started_at), ended_at=VALUES(ended_at)
"""

CHECK_SQL = """
INSERT INTO checks (benchmark_id, layer, name, verdict, detail)
VALUES (%s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE verdict=VALUES(verdict), detail=VALUES(detail)
"""

USAGE_SQL = """
INSERT INTO usage_samples (benchmark_id, source, model, provider, input_tokens,
    output_tokens, total_tokens, cache_read_tokens, cache_write_tokens, cost_usd,
    api_calls, error_calls, matched_by, sampled_at, raw)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    model=VALUES(model), provider=VALUES(provider), input_tokens=VALUES(input_tokens),
    output_tokens=VALUES(output_tokens), total_tokens=VALUES(total_tokens),
    cache_read_tokens=VALUES(cache_read_tokens), cache_write_tokens=VALUES(cache_write_tokens),
    cost_usd=VALUES(cost_usd), api_calls=VALUES(api_calls), error_calls=VALUES(error_calls),
    matched_by=VALUES(matched_by), sampled_at=VALUES(sampled_at), raw=VALUES(raw)
"""


def ingest_file(
    jsonl_path: Path,
    *,
    suite_name: str = "",
    product: str = "",
    model_mode: str = "",
    agent_id: str = "main",
    app_version: str = "",
    connection: Any = None,
) -> IngestResult:
    """把一个批次的 JSONL 灌进库。同一个 BenchmarkId 重放会覆盖，幂等。

    product / model_mode 优先用显式传入的，否则从记录里推——
    JSONL 是事实来源，能自描述就别靠命令行参数记忆。
    """

    try:
        records = list(iter_jsonl(jsonl_path))
    except ReportError as exc:
        raise IngestError(str(exc)) from exc
    if not records:
        raise IngestError(f"{jsonl_path} 里没有任何记录")

    batch_id = str(records[0].get("batch_id") or "")
    if not batch_id:
        raise IngestError(f"{jsonl_path} 缺少 batch_id")

    inferred_mode, model_ref = _infer_model_mode(records)
    meta = BatchMeta(
        batch_id=batch_id,
        product=product or str(records[0].get("product") or DEFAULT_PRODUCT),
        model_mode=model_mode or inferred_mode,
        model_ref=model_ref,
        agent_id=agent_id,
        app_version=app_version,
    )

    started_at, ended_at = _batch_window(records)

    if connection is not None:
        return _write(connection, meta, suite_name, records, started_at, ended_at)
    with connect() as fresh:
        return _write(fresh, meta, suite_name, records, started_at, ended_at)


def _write(
    connection: Any,
    meta: BatchMeta,
    suite_name: str,
    records: list[JsonObject],
    started_at: Any,
    ended_at: Any,
) -> IngestResult:
    suite_id = meta.suite_id
    with connection.cursor() as cursor:
        if suite_name and not suite_id:
            suite_id = suite_id_for(suite_name)
            cursor.execute(
                "INSERT INTO suite_runs (suite_id, name, created_at) VALUES (%s, %s, %s)"
                " ON DUPLICATE KEY UPDATE name=VALUES(name)",
                (suite_id, suite_name, to_utc(now_iso())),
            )

        cursor.execute(
            "INSERT INTO batches (batch_id, suite_id, product, model_mode, model_ref,"
            " agent_id, app_version, started_at, ended_at, run_count)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE suite_id=VALUES(suite_id), product=VALUES(product),"
            " model_mode=VALUES(model_mode), model_ref=VALUES(model_ref),"
            " agent_id=VALUES(agent_id), app_version=VALUES(app_version),"
            " started_at=VALUES(started_at), ended_at=VALUES(ended_at),"
            " run_count=VALUES(run_count)",
            (
                meta.batch_id,
                suite_id,
                meta.product,
                meta.model_mode,
                meta.model_ref,
                meta.agent_id,
                meta.app_version,
                started_at,
                ended_at,
                len(records),
            ),
        )

        run_rows = [_run_row(record, meta.batch_id) for record in records]
        cursor.executemany(RUN_SQL, run_rows)

        check_rows: list[tuple[Any, ...]] = []
        usage_rows: list[tuple[Any, ...]] = []
        for record in records:
            for check in record.get("checks") or []:
                if isinstance(check, dict):
                    check_rows.append(
                        (
                            record.get("benchmark_id"),
                            check.get("layer"),
                            check.get("name"),
                            check.get("verdict"),
                            check.get("detail", ""),
                        )
                    )
            usage_rows.extend(_usage_rows(record))

        if check_rows:
            cursor.executemany(CHECK_SQL, check_rows)
        if usage_rows:
            cursor.executemany(USAGE_SQL, usage_rows)

    return IngestResult(
        batch_id=meta.batch_id,
        runs=len(run_rows),
        checks=len(check_rows),
        usage_samples=len(usage_rows),
    )


def find_result_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(root.glob("*/results.jsonl"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m runner.ingest",
        description="把 results/*/results.jsonl 灌进 MySQL。JSONL 是事实来源，库随时可重建。",
    )
    parser.add_argument(
        "--results", type=Path, default=Path("results"), help="结果目录或单个 jsonl"
    )
    parser.add_argument("--suite", default="", help="归到哪个对比实验（按名字建/复用）")
    parser.add_argument("--product", default="", help="不给就用记录里的 product")
    parser.add_argument("--model-mode", default="", help="不给就从 requested_model 推")
    parser.add_argument("--agent", default="main")
    parser.add_argument("--app-version", default="")
    parser.add_argument(
        "--rebuild", action="store_true", help="先清空再重放（只动这几张表）"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    files = find_result_files(args.results)
    if not files:
        print(f"{args.results} 下没有找到 results.jsonl")
        return 64

    try:
        with connect() as connection:
            if args.rebuild:
                with connection.cursor() as cursor:
                    # runs/checks/usage_samples 靠外键级联清掉。
                    cursor.execute("DELETE FROM batches")
                    cursor.execute("DELETE FROM suite_runs")
                print("已清空旧数据，开始重放")

            total = 0
            for path in files:
                result = ingest_file(
                    path,
                    suite_name=args.suite,
                    product=args.product,
                    model_mode=args.model_mode,
                    agent_id=args.agent,
                    app_version=args.app_version,
                    connection=connection,
                )
                total += result.runs
                print(
                    f"{path} → batch {result.batch_id}："
                    f"{result.runs} 轮 / {result.checks} 条断言 / {result.usage_samples} 条用量"
                )
    except (DatabaseError, IngestError) as exc:
        print(f"入库失败：{exc}")
        return 1

    print(f"共入库 {total} 轮")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
