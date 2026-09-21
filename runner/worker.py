from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .batch import BatchOptions, run_batch
from .case_catalog import CaseCatalogError, load_case_set
from .catalog import CatalogError, list_model_choices, resolve_model_choice
from .client import ChatClient
from .db import DatabaseError
from .discovery import DiscoveryError, discover, has_session, health_check, session_status
from .ingest import IngestError, ingest_file, suite_id_for
from .job_store import (
    CANCELLED,
    COMPLETED,
    FAILED,
    WorkerAlreadyRunning,
    append_event,
    claim_next_job,
    ensure_schema,
    exclusive_worker_lock,
    fail_running_jobs,
    finish_job,
    is_cancel_requested,
    set_completed_runs,
    set_total_runs,
)
from .models import expand_cases
from .newapi import NewApiError
from .reconcile import collect_session_usage, reconcile_suite
from .report import build_database, export_xlsx, summarize
from .sessionlog import SessionLogError
from .transport import TransportError


DEFAULT_POLL_SECONDS = 2.0


def _worker_id() -> str:
    return os.environ.get("BENCH_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


def _log(job_id: str, message: str, level: str = "info") -> None:
    print(f"[{job_id[:8]}] {message}", flush=True)
    append_event(job_id, message, level)


def _terminal_log(job_id: str, message: str, level: str = "info") -> None:
    """终态已落库后，事件日志失败不能再把成功任务反写成 Failed。"""

    print(f"[{job_id[:8]}] {message}", flush=True)
    try:
        append_event(job_id, message, level)
    except DatabaseError as exc:
        print(f"[{job_id[:8]}] 终态事件写入失败：{exc}", file=sys.stderr, flush=True)


def execute_job(
    job: dict[str, Any],
    results_root: Path = Path("results"),
    shutdown_requested: Callable[[], bool] = lambda: False,
) -> None:
    job_id = str(job["job_id"])
    batch_id = str(job["batch_id"])
    out_dir = results_root / batch_id
    results_path = out_dir / "results.jsonl"
    suite_id: str | None = None

    try:
        case_set = load_case_set(
            Path(str(job["case_catalog_path"])), str(job["case_set_id"])
        )
        items = expand_cases(list(case_set.cases))
        limit = int(job.get("limit_runs") or 0)
        if limit > 0:
            items = items[:limit]
        if not items:
            raise CaseCatalogError("选中的 Case Set 没有可执行轮次")
        set_total_runs(job_id, len(items))
        _log(job_id, f"已加载 {case_set.name}：{len(items)} 轮")

        endpoint = discover()
        health_check(endpoint)
        if not has_session(session_status(endpoint)):
            raise DiscoveryError("YonWork 尚未登录（hasSession=false）")
        _log(job_id, f"YonWork 前置检查通过：{endpoint.base_url}")

        model_choice = None
        model_query = str(job.get("model_query") or "").strip()
        if model_query:
            model_choice = resolve_model_choice(list_model_choices(endpoint), model_query)
            _log(job_id, f"指定模型：{model_choice.label}")
        else:
            _log(job_id, "使用 YonWork 智能体当前默认模型")

        client = ChatClient(
            endpoint,
            agent_id=str(job.get("agent_id") or "main"),
            timeout_seconds=float(job.get("timeout_seconds") or 600),
            transcript_dir=out_dir / "transcripts",
            model_choice=model_choice,
        )
        options = BatchOptions(
            batch_id=batch_id,
            results_path=results_path,
            id_prefix=f"web-{job_id[:8]}",
            product=str(job.get("product") or "yonwork"),
            collect_usage=bool(job.get("collect_usage", True)),
        )
        completed = 0

        def on_record(_record: Any) -> None:
            nonlocal completed
            completed += 1
            set_completed_runs(job_id, completed)

        records = run_batch(
            items,
            client=client,
            endpoint=endpoint,
            options=options,
            report=lambda message: _log(
                job_id, message, "warning" if message.lstrip().startswith("!") else "info"
            ),
            on_record=on_record,
            should_stop=lambda: shutdown_requested() or is_cancel_requested(job_id),
        )

        if records:
            database_path = out_dir / "results.db"
            build_database(results_path, database_path)
            if bool(job.get("export_xlsx", True)):
                export_xlsx(database_path, out_dir / "results.xlsx")

            ingest_file(
                results_path,
                suite_name=str(job["experiment_name"]),
                product=str(job.get("product") or "yonwork"),
                agent_id=str(job.get("agent_id") or "main"),
            )
            suite_id = suite_id_for(str(job["experiment_name"]))
            _log(job_id, f"结果已入库：{summarize(results_path).as_text()}")

            try:
                session_result = collect_session_usage(
                    suite_id, agent_id=str(job.get("agent_id") or "main")
                )
                _log(
                    job_id,
                    f"会话用量补采：{session_result['matched']}/{session_result['runs']} 轮",
                )
            except (DatabaseError, SessionLogError) as exc:
                _log(job_id, f"会话用量补采跳过：{exc}", "warning")

            try:
                backend_result = reconcile_suite(suite_id)
                _log(
                    job_id,
                    f"NewAPI 对账：{backend_result['matched']}/{backend_result['runs']} 轮",
                )
            except (DatabaseError, NewApiError, TransportError) as exc:
                _log(job_id, f"NewAPI 对账跳过：{exc}", "warning")

        cancelled = is_cancel_requested(job_id)
        interrupted = shutdown_requested() and len(records) < len(items)
        if cancelled:
            status = CANCELLED
            terminal_message = "任务已停止"
            terminal_error = ""
        elif interrupted:
            status = FAILED
            terminal_message = "Worker 收到停止信号；已保存完成轮次，任务未全部执行"
            terminal_error = terminal_message
        else:
            status = COMPLETED
            terminal_message = "任务执行完成"
            terminal_error = ""
        finish_job(
            job_id,
            status=status,
            suite_id=suite_id,
            results_path=str(results_path) if results_path.is_file() else None,
            error=terminal_error,
        )
        _terminal_log(job_id, terminal_message, "warning" if interrupted else "info")
    except (
        CaseCatalogError,
        CatalogError,
        DatabaseError,
        DiscoveryError,
        IngestError,
        TransportError,
    ) as exc:
        finish_job(
            job_id,
            status=FAILED,
            suite_id=suite_id,
            results_path=str(results_path) if results_path.is_file() else None,
            error=str(exc),
        )
        _terminal_log(job_id, f"任务失败：{exc}", "error")
    except Exception as exc:  # noqa: BLE001 - Worker 必须把未知异常留在任务现场
        finish_job(
            job_id,
            status=FAILED,
            suite_id=suite_id,
            results_path=str(results_path) if results_path.is_file() else None,
            error=f"{type(exc).__name__}: {exc}",
        )
        _terminal_log(
            job_id, f"Worker 未预期异常：{type(exc).__name__}: {exc}", "error"
        )


def run_worker(*, once: bool = False, poll_seconds: float = DEFAULT_POLL_SECONDS) -> int:
    ensure_schema()
    worker_id = _worker_id()

    # 先拿独占锁再收拢遗留任务：没有锁的话，这一步会把另一个 Worker
    # 正在跑的任务误判成「上次异常退出」。
    with exclusive_worker_lock():
        recovered = fail_running_jobs("Worker 重启，上一任务的执行状态无法确认")
        if recovered:
            print(f"已将 {recovered} 个遗留 Running 任务标记为 Failed", flush=True)

        stopping = False

        def stop(_signum: int, _frame: Any) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print(f"Benchmark Worker 已启动：{worker_id}", flush=True)

        while not stopping:
            job = claim_next_job(worker_id)
            if job is not None:
                execute_job(job, shutdown_requested=lambda: stopping)
                if once:
                    return 0
                continue
            if once:
                return 0
            time.sleep(poll_seconds)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行 Web 创建的基准测试任务")
    parser.add_argument("--once", action="store_true", help="最多执行一条任务后退出")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_worker(once=args.once, poll_seconds=max(0.2, args.poll_seconds))
    except WorkerAlreadyRunning as exc:
        print(f"Worker 未启动：{exc}", file=sys.stderr)
        return 2
    except DatabaseError as exc:
        print(f"Worker 启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
