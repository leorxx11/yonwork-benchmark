from __future__ import annotations

import argparse
import json
from typing import Any, Sequence

from .db import DatabaseError, connect, to_utc
from .newapi import NewApiConfig, NewApiError, fetch_logs, match_logs
from .sessionlog import SessionLogError, collect as collect_sessions, to_json as session_json
from .transport import TransportError


UPSERT_SQL = """
INSERT INTO usage_samples (benchmark_id, source, model, provider, input_tokens,
    output_tokens, total_tokens, cache_read_tokens, cache_write_tokens,
    api_calls, error_calls, matched_by, sampled_at, raw)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    model=VALUES(model), provider=VALUES(provider), input_tokens=VALUES(input_tokens),
    output_tokens=VALUES(output_tokens), total_tokens=VALUES(total_tokens),
    cache_read_tokens=VALUES(cache_read_tokens), cache_write_tokens=VALUES(cache_write_tokens),
    api_calls=VALUES(api_calls), error_calls=VALUES(error_calls),
    matched_by=VALUES(matched_by), sampled_at=VALUES(sampled_at), raw=VALUES(raw)
"""


def _runs_for(connection: Any, suite_id: str) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT r.benchmark_id, r.started_at, r.ended_at, b.model_mode
            FROM runs r JOIN batches b ON b.batch_id = r.batch_id
            WHERE b.suite_id = %s AND r.started_at IS NOT NULL
            ORDER BY r.started_at
            """,
            (suite_id,),
        )
        return list(cursor.fetchall())


def reconcile_suite(suite_id: str, *, token_name: str = "") -> dict[str, Any]:
    """拉 NewAPI 后台日志，按时间窗落到每一轮上，写进 usage_samples。

    对不上的日志不会被丢掉，会在返回里报出来——跑批期间有人手动点对话、
    或者别的进程在用同一个 token，都会在这里露出来。
    """
    config = NewApiConfig.load()
    with connect() as connection:
        runs = _runs_for(connection, suite_id)
        if not runs:
            return {"runs": 0, "matched": 0, "unmatched_logs": 0, "missing": []}

        window_start = min(run["started_at"] for run in runs)
        window_end = max(run["ended_at"] or run["started_at"] for run in runs)
        logs = fetch_logs(config, window_start, window_end, token_name=token_name)
        samples, unmatched = match_logs(logs, runs)

        with connection.cursor() as cursor:
            for sample in samples:
                cursor.execute(
                    UPSERT_SQL,
                    (
                        sample.benchmark_id,
                        "newapi",
                        sample.model,
                        None,
                        sample.input_tokens,
                        sample.output_tokens,
                        sample.total_tokens,
                        None,
                        None,
                        sample.api_calls,
                        sample.error_calls,
                        sample.matched_by,
                        sample.sampled_at,
                        json.dumps(sample.raw, ensure_ascii=False),
                    ),
                )

    matched_ids = {sample.benchmark_id for sample in samples}
    return {
        "runs": len(runs),
        "matched": len(matched_ids),
        "unmatched_logs": len(unmatched),
        # 走了 newapi 模式却没有后台记录的轮次 —— 值得单独看一眼
        "missing": [
            run["benchmark_id"]
            for run in runs
            if run["benchmark_id"] not in matched_ids and run["model_mode"] != "default"
        ],
    }


def collect_session_usage(suite_id: str, *, agent_id: str = "main") -> dict[str, Any]:
    """从本地会话 JSONL 补 token 用量。

    这一路按 idempotencyKey 精确匹配，且**覆盖所有模式**——
    默认模型那些轮不经过 NewAPI，端上 HTTP 端点又在漏记，只有这里记着。
    """
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.benchmark_id FROM runs r
                JOIN batches b ON b.batch_id = r.batch_id
                WHERE b.suite_id = %s
                """,
                (suite_id,),
            )
            wanted = {row["benchmark_id"] for row in cursor.fetchall()}
        if not wanted:
            return {"runs": 0, "matched": 0, "missing": []}

        found = collect_sessions(wanted, agent_id=agent_id)
        with connection.cursor() as cursor:
            for benchmark_id, usage in found.items():
                cursor.execute(
                    UPSERT_SQL,
                    (
                        benchmark_id,
                        "session-jsonl",
                        usage.model,
                        usage.provider,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.total_tokens,
                        usage.cache_read_tokens,
                        usage.cache_write_tokens,
                        # 助手轮次数就是这一轮实际发生的模型调用次数
                        usage.assistant_turns,
                        None,
                        "idempotency-key",
                        to_utc(usage.timestamp),
                        json.dumps(session_json(usage), ensure_ascii=False),
                    ),
                )

    return {
        "runs": len(wanted),
        "matched": len(found),
        "missing": sorted(wanted - set(found)),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m runner.reconcile",
        description="拉 NewAPI 后台用量，和端上数据放进同一张表做对账。",
    )
    parser.add_argument("--suite", required=True, help="suite_id")
    parser.add_argument(
        "--token-name", default="", help="只看某个 NewAPI 令牌名下的日志（默认全部）"
    )
    parser.add_argument("--agent", default="main", help="会话日志属于哪个 agent")
    parser.add_argument(
        "--skip-newapi", action="store_true", help="只补会话 JSONL，不碰 NewAPI"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        session = collect_session_usage(args.suite, agent_id=args.agent)
    except (DatabaseError, SessionLogError) as exc:
        print(f"会话日志采集失败：{exc}")
        return 1
    print(f"会话 JSONL：{session['runs']} 轮中 {session['matched']} 轮有记录")
    for benchmark_id in session["missing"]:
        print(f"  会话日志里没有：{benchmark_id}")

    if args.skip_newapi:
        return 0

    try:
        result = reconcile_suite(args.suite, token_name=args.token_name)
    except (DatabaseError, NewApiError, TransportError) as exc:
        print(f"NewAPI 对账失败：{exc}")
        return 1

    print(
        f"NewAPI 后台：{result['runs']} 轮中 {result['matched']} 轮对上；"
        f"{result['unmatched_logs']} 条后台日志没有对应轮次"
    )
    if result["missing"]:
        print("以下轮次走了非默认模型却没有后台记录：")
        for benchmark_id in result["missing"]:
            print(f"  {benchmark_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
