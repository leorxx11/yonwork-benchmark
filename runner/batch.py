from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .assertions import evaluate
from .client import ChatClient, session_key_for
from .discovery import HostEndpoint
from .models import (
    ChatTurn,
    UsageSample,
    EXIT_CODES,
    RunRecord,
    TaskItem,
    USAGE_SOURCES,
    Verdict,
    benchmark_id_for,
    now_iso,
    worst,
)
from .report import append_jsonl
from .sessionlog import SessionLogError, collect_one
from .transport import TransportError
from .usage import (
    DEFAULT_SETTLE_SECONDS,
    fetch_recent_token_history,
    log_stats_from_usage,
    match_usage,
)


Reporter = Callable[[str], None]
RecordReporter = Callable[[RunRecord], None]
StopCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class BatchOptions:
    batch_id: str
    results_path: Path
    id_prefix: str = "bench"
    product: str = "yonwork"
    collect_usage: bool = True
    usage_settle_seconds: float = DEFAULT_SETTLE_SECONDS


def run_batch(
    items: list[TaskItem],
    *,
    client: ChatClient,
    endpoint: HostEndpoint,
    options: BatchOptions,
    report: Reporter = lambda _message: None,
    on_record: RecordReporter = lambda _record: None,
    should_stop: StopCheck = lambda: False,
) -> list[RunRecord]:
    """跑一批，每轮之间完全隔离。

    一轮出事只影响这一轮：异常被捕获、归类、照样落盘，然后继续下一轮。
    PAD 时代第 1 条问题（一次超时整批中止）就是在这里解决的。
    """
    records: list[RunRecord] = []
    for item in items:
        if should_stop():
            report("收到停止请求，不再开始下一轮")
            break
        record = run_one(
            item, client=client, endpoint=endpoint, options=options, report=report
        )
        append_jsonl(options.results_path, record)
        records.append(record)
        on_record(record)
        report(
            f"[{item.position + 1}/{len(items)}] {item.case_name}#{item.run_no}"
            f" → {record.verdict.value}"
        )
    return records


def run_one(
    item: TaskItem,
    *,
    client: ChatClient,
    endpoint: HostEndpoint,
    options: BatchOptions,
    report: Reporter = lambda _message: None,
) -> RunRecord:
    stamp = f"{time.time_ns() // 1_000_000:x}"
    benchmark_id = benchmark_id_for(options.id_prefix, item.case_name, item.run_no, stamp)
    # 每轮全新 sessionKey：复用会让本轮看见上一轮上下文，数据静默作废。
    session_key = session_key_for(benchmark_id, client.agent_id)

    turn: ChatTurn | None = None
    failure: BaseException | None = None
    try:
        turn = client.send(
            benchmark_id=benchmark_id, prompt=item.prompt, session_key=session_key
        )
    except BaseException as exc:  # noqa: BLE001 —— 本轮兜底，绝不让一轮拖垮整批
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        failure = exc
        turn = getattr(exc, "partial", None)

    samples: list[UsageSample] = []
    notes: list[str] = []
    if options.collect_usage:
        for label, collect in (
            ("端上用量", lambda: _collect_device_usage(endpoint, turn, options.usage_settle_seconds)),
            ("会话日志", lambda: _collect_session_usage(turn, client.agent_id)),
        ):
            try:
                sample = collect()
            except (TransportError, SessionLogError) as exc:
                # 某一路采不到不改变本轮的产品判定，只记一笔——
                # 「没采到」和「产品出错」是两回事，混了统计就脏了。
                notes.append(f"{label}采集失败：{exc}")
                report(f"  ! {notes[-1]}")
                continue
            if sample is not None:
                samples.append(sample)

    usage = None
    for source in USAGE_SOURCES:
        usage = next((item for item in samples if item.source == source), None)
        if usage is not None:
            break

    log_stats = log_stats_from_usage(usage, turn)
    evaluation = evaluate(
        turn=turn,
        expectations=item.expectations,
        usage=usage,
        log_stats=log_stats,
        failure=failure,
    )

    return RunRecord(
        benchmark_id=benchmark_id,
        batch_id=options.batch_id,
        position=item.position,
        case_name=item.case_name,
        run_no=item.run_no,
        prompt=item.prompt,
        session_key=session_key,
        verdict=evaluation.verdict,
        product=options.product,
        checks=evaluation.checks,
        turn=turn,
        usage_samples=tuple(samples),
        log_stats=log_stats,
        note="；".join(part for part in (*notes, evaluation.summary) if part),
        created_at=now_iso(),
    )


def _collect_device_usage(
    endpoint: HostEndpoint, turn: ChatTurn | None, settle: float
) -> UsageSample | None:
    if turn is None:
        return None
    if settle > 0:
        time.sleep(settle)
    return match_usage(fetch_recent_token_history(endpoint), turn)


def _collect_session_usage(turn: ChatTurn | None, agent_id: str) -> UsageSample | None:
    """会话 JSONL：本地文件，按 idempotencyKey 精确匹配，不受时间窗影响。

    端上 HTTP 端点实测会漏记，默认模型那几轮全靠这一路才有数。
    """
    if turn is None:
        return None
    started = _parse_iso(turn.started_at)
    found = collect_one(turn.benchmark_id, agent_id=agent_id, started_at=started)
    return found.as_sample() if found else None


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def exit_code_for(records: list[RunRecord]) -> int:
    """自定义退出码（PAD 时代 EXIT Code 恒为 0，CI 判不了）。"""
    if not records:
        return EXIT_CODES[Verdict.INVALID]  # 一轮都没跑成，数据无意义
    return EXIT_CODES[worst([record.verdict for record in records])]
