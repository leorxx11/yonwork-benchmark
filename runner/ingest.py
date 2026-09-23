from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .db import DatabaseError, connect, to_millis, to_utc
from .modelproxy import LedgerError, load_ledger
from .models import JsonObject, now_iso, observed_tool_count
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
    model_requests: int = 0


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
    # 计数属于各自来源，不能把 NewAPI 的 ErrorCalls 复制到端上/会话用量行。
    source = usage.get("source") or DEVICE_SOURCE
    legacy_stats = stats if isinstance(stats, dict) and stats.get("source") in (
        source, "recent-token-history" if source == DEVICE_SOURCE else source,
    ) else {}
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
        usage.get("api_calls", legacy_stats.get("api_calls")),
        usage.get("error_calls", legacy_stats.get("error_calls")),
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
        to_millis(turn.get("engine_seconds")),
        (record.get("model_calls") or {}).get("status") or "disabled",
        turn.get("terminated_by"),
        turn.get("stop_reason"),
        observed_tool_count(turn),
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
    engine_ms, model_calls_status, terminated_by, stop_reason, tool_call_count,
    answer_preview, transcript_path, note, started_at, ended_at, created_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    batch_id=VALUES(batch_id), verdict=VALUES(verdict),
    requested_model=VALUES(requested_model), duration_ms=VALUES(duration_ms),
    first_delta_ms=VALUES(first_delta_ms), engine_ms=VALUES(engine_ms),
    model_calls_status=VALUES(model_calls_status),
    terminated_by=VALUES(terminated_by),
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


MODEL_REQUEST_SQL = """
INSERT INTO model_requests (request_id, batch_id, benchmark_id, proxy_id, sequence,
    attribution, attribution_source, product, protocol, requested_model, response_model,
    is_stream, http_status, termination, first_output_ms, duration_ms, input_tokens,
    output_tokens, total_tokens, cache_read_tokens, usage_status, output_events,
    traceparent, upstream_request_id, upstream_attempts, error_kind, received_at, ended_at, raw)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    batch_id=VALUES(batch_id),
    benchmark_id=VALUES(benchmark_id), attribution=VALUES(attribution),
    attribution_source=VALUES(attribution_source), response_model=VALUES(response_model),
    http_status=VALUES(http_status), termination=VALUES(termination),
    first_output_ms=VALUES(first_output_ms), duration_ms=VALUES(duration_ms),
    input_tokens=VALUES(input_tokens), output_tokens=VALUES(output_tokens),
    total_tokens=VALUES(total_tokens), cache_read_tokens=VALUES(cache_read_tokens),
    usage_status=VALUES(usage_status), output_events=VALUES(output_events),
    traceparent=VALUES(traceparent),
    upstream_request_id=VALUES(upstream_request_id), error_kind=VALUES(error_kind),
    ended_at=VALUES(ended_at), raw=VALUES(raw)
"""


def _model_request_row(
    request: JsonObject, batch_id: str, benchmark_id: str | None
) -> tuple[Any, ...]:
    usage = request.get("usage") if isinstance(request.get("usage"), dict) else {}
    return (
        request.get("request_id"),
        batch_id,
        benchmark_id,
        request.get("proxy_id") or "",
        request.get("sequence") or 0,
        request.get("attribution") or "unattributed",
        request.get("attribution_source") or "",
        request.get("product"),
        request.get("protocol") or "",
        request.get("requested_model"),
        request.get("response_model"),
        request.get("stream"),
        request.get("http_status"),
        request.get("termination"),
        to_millis(request.get("first_output_seconds")),
        to_millis(request.get("duration_seconds")),
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("total_tokens"),
        usage.get("prompt_cache_hit_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        request.get("usage_status") or "missing",
        request.get("output_events"),
        request.get("traceparent"),
        request.get("upstream_request_id"),
        # 恒为 None：一次客户端请求不等于一次上游尝试（`ModelRequestRecord` 的规矩）。
        request.get("upstream_attempts"),
        request.get("error_kind") or "",
        to_utc(request.get("received_at")),
        to_utc(request.get("ended_at")),
        json.dumps(request, ensure_ascii=False),
    )


def _model_request_rows(records: list[JsonObject], batch_id: str) -> list[tuple[Any, ...]]:
    """逐请求账本入库。

    两个来源合并，按 `request_id` 去重：

    1. 每轮 `RunRecord.model_calls.requests`——归属到某一轮的那些。
    2. 批次的 `model-requests.jsonl`——**补上未归属和被拒的请求**。
       它们属于这个批次但不属于任何一轮，`benchmark_id` 留 NULL。
       只靠来源 1 的话，未归属请求永远进不了报告，而「没有未归属请求」和
       「有但我们没记」看起来一模一样。

    账本读得到时**以账本为准**：hook 绑定可能晚于这一轮的快照才到
    （`CollectorProxy.bind_trace` 会追加一条新的 closed 行），快照里还是未归属，
    账本里已经补上了。归属到别的批次的记录不入本批——跨批次的迟到请求
    会同时写进两批的账本，由它所属那一批入库（重复入库时 `batch_id` 跟着改过去）。

    账本读不到就只入来源 1（换台机器重放时会这样），**不伪造**缺失的那部分。
    """
    rows: dict[str, tuple[Any, ...]] = {}
    ledgers: set[str] = set()
    batch_runs = {str(record.get("benchmark_id")) for record in records if record.get("benchmark_id")}
    for record in records:
        calls = record.get("model_calls")
        if not isinstance(calls, dict):
            continue
        if calls.get("ledger_path"):
            ledgers.add(str(calls["ledger_path"]))
        for request in calls.get("requests") or []:
            if isinstance(request, dict) and request.get("request_id"):
                rows[str(request["request_id"])] = _model_request_row(
                    request, batch_id, record.get("benchmark_id")
                )
    for path in sorted(ledgers):
        try:
            # 用 load_ledger 而不是自己读行：它会合并 open/closed 两行，
            # 并把只有 open 的记录标成 incomplete。自己再实现一遍迟早会漏掉那一步，
            # 于是采集器中断看起来就跟正常请求一样了。
            entries = load_ledger(Path(path))
        except LedgerError:
            continue
        for entry in entries:
            if entry.run_id and entry.run_id not in batch_runs:
                # 归属到别的批次（跨批次的迟到请求 / 迟到绑定）：由那一批入库，
                # 这里记成「本批未归属」就是污染。
                rows.pop(entry.request_id, None)
                continue
            rows[entry.request_id] = _model_request_row(entry.to_json(), batch_id, entry.run_id)
    return list(rows.values())


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
        _widen_usage_source(connection)
        _ensure_engine_column(connection)
        _ensure_model_calls(connection)
        return _write(connection, meta, suite_name, records, started_at, ended_at)
    with connect() as fresh:
        _widen_usage_source(fresh)
        _ensure_engine_column(fresh)
        _ensure_model_calls(fresh)
        return _write(fresh, meta, suite_name, records, started_at, ended_at)


_SOURCE_WIDENED = False


def _widen_usage_source(connection: Any) -> None:
    """把 `usage_samples.source` 从 ENUM 放宽成 VARCHAR。

    `infra/schema.sql` 只在数据目录为空时执行一次，已有的库永远跑不到那份新定义。
    不迁的话，`workbuddy-cli` 这一行会在插入时被 MySQL 拒掉，
    而且症状是「用量列空着」——看起来跟端上漏记（产品缺陷 #3）一模一样，很难查。

    只在确实还是 ENUM 时才 ALTER，所以重复调用是廉价的。
    """
    global _SOURCE_WIDENED
    if _SOURCE_WIDENED:
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DATA_TYPE FROM information_schema.COLUMNS"
                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'usage_samples'"
                " AND COLUMN_NAME = 'source'"
            )
            row = cursor.fetchone()
            if row and str(row.get("DATA_TYPE", "")).lower() == "enum":
                cursor.execute(
                    "ALTER TABLE usage_samples MODIFY COLUMN source VARCHAR(32) NOT NULL"
                )
    except Exception as exc:  # noqa: BLE001 —— 迁移失败不该拦住入库
        print(f"提示：usage_samples.source 放宽失败，新来源可能存不进去：{exc}")
    _SOURCE_WIDENED = True


_MODEL_CALLS_READY = False

# ⚠️ 与 `infra/schema.sql` 的 model_requests 是有意重复的两份：那份只在数据目录
# 为空时由 MySQL 镜像执行一次，已有的库永远跑不到。加列时两处都要改。
_MODEL_REQUESTS_DDL = """
CREATE TABLE IF NOT EXISTS model_requests (
    request_id         VARCHAR(160) NOT NULL PRIMARY KEY,
    batch_id           VARCHAR(64)  NOT NULL,
    benchmark_id       VARCHAR(128) NULL,
    proxy_id           VARCHAR(128) NOT NULL DEFAULT '',
    sequence           INT NOT NULL DEFAULT 0,
    attribution        VARCHAR(16)  NOT NULL DEFAULT 'unattributed',
    attribution_source VARCHAR(64)  NOT NULL DEFAULT '',
    product            VARCHAR(32)  NULL,
    protocol           VARCHAR(32)  NOT NULL DEFAULT '',
    requested_model    VARCHAR(128) NULL,
    response_model     VARCHAR(128) NULL,
    is_stream          TINYINT(1)   NULL,
    http_status        INT NULL,
    termination        VARCHAR(32)  NULL,
    first_output_ms    INT NULL,
    duration_ms        INT NULL,
    input_tokens       INT NULL,
    output_tokens      INT NULL,
    total_tokens       INT NULL,
    cache_read_tokens  INT NULL,
    usage_status       VARCHAR(16)  NOT NULL DEFAULT 'missing',
    output_events      INT NULL,
    traceparent        VARCHAR(128) NULL,
    upstream_request_id VARCHAR(128) NULL,
    upstream_attempts  INT NULL,
    error_kind         VARCHAR(64)  NOT NULL DEFAULT '',
    received_at        DATETIME(3)  NULL,
    ended_at           DATETIME(3)  NULL,
    raw                JSON NULL,
    KEY idx_mreq_run (benchmark_id, sequence),
    KEY idx_mreq_batch (batch_id, sequence),
    KEY idx_mreq_attribution (attribution),
    KEY idx_mreq_upstream (upstream_request_id),
    KEY idx_mreq_trace (traceparent),
    CONSTRAINT fk_mreq_batch FOREIGN KEY (batch_id)
        REFERENCES batches (batch_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _ensure_model_calls(connection: Any) -> None:
    """给已有的库补 `runs.model_calls_status` 和 `model_requests` 表。

    **和 `_ensure_engine_column` 一样不吞异常**：`_run_row` 已经无条件多传了一个值，
    列不存在的话每条 INSERT 都会失败，整批入不了库。与其让人对着
    "Unknown column 'model_calls_status'" 猜，不如在这里就炸。
    """
    global _MODEL_CALLS_READY
    if _MODEL_CALLS_READY:
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'runs'"
            " AND COLUMN_NAME = 'model_calls_status'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "ALTER TABLE runs ADD COLUMN model_calls_status VARCHAR(16)"
                " NOT NULL DEFAULT 'disabled' AFTER engine_ms"
            )
        cursor.execute(_MODEL_REQUESTS_DDL)
        # 已有 model_requests 表的库要单独补这一列。
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'model_requests'"
            " AND COLUMN_NAME = 'traceparent'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "ALTER TABLE model_requests ADD COLUMN traceparent VARCHAR(128) NULL"
                " AFTER output_events"
            )
    _MODEL_CALLS_READY = True


_ENGINE_COLUMN_READY = False


def _ensure_engine_column(connection: Any) -> None:
    """给已有的库补 `runs.engine_ms`。

    和上面一样的理由：`infra/schema.sql` 只在数据目录为空时跑一次。
    **这次不能吞异常**——`_run_row` 已经无条件多传了一个值，列不存在的话
    每一条 INSERT 都会失败，整批入不了库。与其让人对着
    "Unknown column 'engine_ms'" 猜，不如在这里就炸。
    （`_widen_usage_source` 吞异常是对的：那条不迁也只是少一个来源。）
    """
    global _ENGINE_COLUMN_READY
    if _ENGINE_COLUMN_READY:
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'runs'"
            " AND COLUMN_NAME = 'engine_ms'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "ALTER TABLE runs ADD COLUMN engine_ms INT NULL AFTER first_delta_ms"
            )
    _ENGINE_COLUMN_READY = True


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

        request_rows = _model_request_rows(records, meta.batch_id)

        if check_rows:
            cursor.executemany(CHECK_SQL, check_rows)
        if usage_rows:
            cursor.executemany(USAGE_SQL, usage_rows)
        if request_rows:
            cursor.executemany(MODEL_REQUEST_SQL, request_rows)

    return IngestResult(
        batch_id=meta.batch_id,
        runs=len(run_rows),
        checks=len(check_rows),
        usage_samples=len(usage_rows),
        model_requests=len(request_rows),
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
                    f" / {result.model_requests} 条模型请求"
                )
    except (DatabaseError, IngestError) as exc:
        print(f"入库失败：{exc}")
        return 1

    print(f"共入库 {total} 轮")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
