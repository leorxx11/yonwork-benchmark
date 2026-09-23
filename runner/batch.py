from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .assertions import evaluate
from .drivers import Driver
from .modelproxy import ATTRIBUTED, CollectorProxy, LATE
from .models import (
    ChatTurn,
    UsageSample,
    EXIT_CODES,
    MODEL_CALLS_DISABLED,
    MODEL_CALLS_OBSERVED,
    MODEL_CALLS_UNAVAILABLE,
    ModelCallCollection,
    RunRecord,
    TaskItem,
    USAGE_SOURCES,
    Verdict,
    benchmark_id_for,
    now_iso,
    worst,
)
from .report import append_jsonl
from .usage import log_stats_from_usage


Reporter = Callable[[str], None]
RecordReporter = Callable[[RunRecord], None]
StopCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class BatchOptions:
    batch_id: str
    results_path: Path
    id_prefix: str = "bench"
    collect_usage: bool = True


def run_batch(
    items: list[TaskItem],
    *,
    driver: Driver,
    options: BatchOptions,
    report: Reporter = lambda _message: None,
    on_record: RecordReporter = lambda _record: None,
    should_stop: StopCheck = lambda: False,
    collector: CollectorProxy | None = None,
) -> list[RunRecord]:
    """跑一批，每轮之间完全隔离。

    一轮出事只影响这一轮：异常被捕获、归类、照样落盘，然后继续下一轮。
    PAD 时代第 1 条问题（一次超时整批中止）就是在这里解决的。

    这里对被测产品**一无所知**——YonWork 的 SSE 和 WorkBuddy 的一次性进程
    都只是 `driver` 后面的实现细节。隔离、落盘、判定、计数这四件事是共用的。
    """
    records: list[RunRecord] = []
    for item in items:
        if should_stop():
            report("收到停止请求，不再开始下一轮")
            break
        record = run_one(
            item, driver=driver, options=options, report=report, collector=collector
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
    driver: Driver,
    options: BatchOptions,
    report: Reporter = lambda _message: None,
    collector: CollectorProxy | None = None,
) -> RunRecord:
    stamp = f"{time.time_ns() // 1_000_000:x}"
    benchmark_id = benchmark_id_for(options.id_prefix, item.case_name, item.run_no, stamp)
    # 每轮全新的隔离标识：复用会让本轮看见上一轮上下文，数据静默作废。
    # 具体是 sessionKey 还是 --session-id 由驱动决定，这里不关心。
    session_key = driver.session_key(benchmark_id)

    turn: ChatTurn | None = None
    failure: BaseException | None = None
    # 必须在发请求**之前**注册：晚一步的话本轮最早那几个请求会记成未归属，
    # 而归属只认原生头全等，事后没法补回来。
    if collector is not None:
        collector.register(run_id=benchmark_id, product=driver.product)
    try:
        turn = driver.run_turn(benchmark_id=benchmark_id, prompt=item.prompt)
    except BaseException as exc:  # noqa: BLE001 —— 本轮兜底，绝不让一轮拖垮整批
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        failure = exc
        turn = getattr(exc, "partial", None)
    finally:
        # 收尾只改归属标记，**不表示不会再有请求**：之后来的同标识请求记 late，
        # 仍归这一轮，不会偷偷并进下一轮。
        if collector is not None:
            collector.close_run(benchmark_id)

    samples: tuple[UsageSample, ...] = ()
    notes: tuple[str, ...] = ()
    if options.collect_usage and turn is not None:
        collected = driver.collect_usage(turn)
        samples, notes = collected.samples, collected.notes
        for note in notes:
            report(f"  ! {note}")

    if turn is not None:
        # 放在采用量**之后**：补采读的是应用写出来的会话 JSONL，
        # 采用量那一步已经等过落盘，这里就不用再单独等一次。
        try:
            turn = driver.enrich(turn)
        except AttributeError:
            # 驱动少实现了协议方法是**编程错误**，不是数据问题。
            # 跟下面一起吞掉的话，新驱动忘了写 enrich 就会静默少一份原材料，
            # 表现成「这个产品从来不调工具」——正是要防的那种假数据。
            raise
        except Exception as exc:  # noqa: BLE001 —— 工具补采失败交给断言层分类
            turn = replace(turn, tool_calls_status="error", tool_calls_source="driver-enrich",
                           tool_calls_detail=f"原材料补采失败：{type(exc).__name__}")
        if turn.tool_calls_status != "observed":
            detail = turn.tool_calls_detail or "工具调用未完整采集"
            notes = (*notes, detail)
            report(f"  ! {detail}")

    usage = None
    for source in USAGE_SOURCES:
        usage = next((sample for sample in samples if sample.source == source), None)
        if usage is not None:
            break

    # token 的来源优先级不等于调用计数来源优先级：端上 token 存在时，
    # 也必须让后台错误计数参与本轮判定。
    log_sample = next((sample for sample in samples if sample.source == "newapi"), usage)
    log_stats = log_stats_from_usage(log_sample, turn)
    model_calls = _collect_model_calls(collector, benchmark_id, turn)
    if model_calls.status == MODEL_CALLS_UNAVAILABLE:
        notes = (*notes, model_calls.detail)
        report(f"  ! {model_calls.detail}")
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
        product=driver.product,
        checks=evaluation.checks,
        turn=turn,
        usage_samples=samples,
        log_stats=log_stats,
        model_calls=model_calls,
        note="；".join(part for part in (*notes, evaluation.summary) if part),
        created_at=now_iso(),
    )


def _collect_model_calls(
    collector: CollectorProxy | None, benchmark_id: str, turn: ChatTurn | None
) -> ModelCallCollection:
    """把这一轮经过采集入口的请求收进记录。**只搬运不判定**（二-3）。

    ⚠️ 关键在于把「没开采集」和「0 次调用」分开。采集开着却一个请求都没经过，
    几乎一定是产品的 baseUrl 没指向我们，而不是产品真的没调模型——
    这时候记 0 就成了产品缺陷 #3 那种静默漏记：数字看着正常，全是假的。
    所以这种情况标 `unavailable` 并说清楚该去查什么，由断言层决定怎么归类。
    """
    if collector is None:
        return ModelCallCollection(status=MODEL_CALLS_DISABLED, detail="逐请求采集未启用")
    records = collector.records_for(benchmark_id)
    payload = tuple(record.to_json() for record in records)
    ledger_path = str(collector.ledger_path)
    if not any(record.attribution in (ATTRIBUTED, LATE) for record in records):
        # ⚠️ **两种原因都要说**，别只报一种。
        # 原来这里断言「baseUrl 没指过来」，但 YonWork 1.0.10 换用 OpenAI SDK 之后
        # 请求照样到入口、只是不再带 `x-yonwork-run-id`——那种情况下 baseUrl 是对的，
        # 照着这句话去查会一路查错方向（2026-09-22 实际发生过）。
        return ModelCallCollection(
            status=MODEL_CALLS_UNAVAILABLE,
            detail=(
                "逐请求采集已启用，但本轮一个请求都没归属上。三种可能："
                "①这个模式本来就不经过采集入口（如产品官方默认通路）——账本里应当 0 条请求；"
                "②产品的 baseUrl 没指向 BENCH_COLLECTOR_PORT——同样 0 条；"
                "③产品这个版本不再发送可关联的轮次标识"
                "（YonWork 1.0.10 起就不发 x-yonwork-run-id）——账本里有 unattributed 请求。"
                "看批次账本里有没有请求即可区分 ①② 和 ③"
            ),
            requests=payload,
            ledger_path=ledger_path,
        )
    return ModelCallCollection(
        status=MODEL_CALLS_OBSERVED, requests=payload, ledger_path=ledger_path
    )


def exit_code_for(records: list[RunRecord]) -> int:
    """自定义退出码（PAD 时代 EXIT Code 恒为 0，CI 判不了）。"""
    if not records:
        return EXIT_CODES[Verdict.INVALID]  # 一轮都没跑成，数据无意义
    return EXIT_CODES[worst([record.verdict for record in records])]
