from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .batch import BatchOptions, exit_code_for, run_batch
from .case_catalog import (
    DEFAULT_CASE_SET,
    DEFAULT_CATALOG_PATH,
    CaseCatalogError,
    load_case_set,
)
from .cases import CasesError, load_cases
from .catalog import CatalogError, list_model_choices
from .client import DEFAULT_TURN_TIMEOUT_SECONDS
from .discovery import DiscoveryError, discover
from .drivers import DRIVERS, DriverError, DriverSpec, build_driver
from .db import DatabaseError
from .job_store import WorkerAlreadyRunning, exclusive_worker_lock
from .modelproxy import CollectorConfig, CollectorConfigError, CollectorProxy, LedgerWriter
from .models import EXIT_CODES, Verdict, expand_cases
from .report import build_database, export_xlsx, summarize
from .transport import TransportError


EXIT_USAGE = 64  # 参数/前置条件问题，和跑批结论区分开


def _log(message: str) -> None:
    print(message, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m runner",
        description="YonWork Host API 基准测试：批量跑 prompt，落 JSONL，再汇总。",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CATALOG_PATH,
        help="YAML Case Catalog（默认 cases/catalog.yaml）",
    )
    parser.add_argument(
        "--case-set", default=DEFAULT_CASE_SET, help="Catalog 中的 Case Set id"
    )
    parser.add_argument(
        "--workbook", type=Path, help="兼容旧流程：改从 Excel 工作簿读取"
    )
    parser.add_argument("--sheet", default="Cases", help="旧 Excel 工作表名")
    parser.add_argument("--out-dir", type=Path, default=Path("results"), help="产物目录")
    parser.add_argument("--batch-id", default="", help="默认用当前时间戳")
    parser.add_argument("--id-prefix", default="bench", help="BenchmarkId 前缀")
    parser.add_argument("--agent", default="main", help="agent id")
    parser.add_argument(
        "--product",
        default="yonwork",
        choices=sorted(DRIVERS),
        help="被测产品，决定用哪个驱动",
    )
    parser.add_argument(
        "--model",
        default="",
        help="指定模型（显示名 / modelId / choiceId），不给就用智能体默认",
    )
    parser.add_argument(
        "--list-models", action="store_true", help="列出可用模型后退出"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TURN_TIMEOUT_SECONDS,
        help="单轮超时（秒）",
    )
    parser.add_argument(
        "--allow-tools",
        action="store_true",
        help="允许被测产品调用工具（只对 WorkBuddy 有效；YonWork 的工具由智能体配置决定）",
    )
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 轮，0 表示不限")
    parser.add_argument("--no-usage", action="store_true", help="不采集用量和后台调用统计")
    parser.add_argument("--no-xlsx", action="store_true", help="只出 JSONL + SQLite")
    parser.add_argument(
        "--no-transcript", action="store_true", help="不保存每轮的 SSE 原始流"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只做前置检查和用例展开，不发请求"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    batch_id = args.batch_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir: Path = args.out_dir / batch_id
    results_path = out_dir / "results.jsonl"

    if args.list_models:
        try:
            endpoint = discover()
            for choice in list_model_choices(endpoint):
                _log(f"  {choice.label}{'（默认）' if choice.is_default else ''}")
        except (DiscoveryError, CatalogError, TransportError) as exc:
            _log(f"读取模型列表失败：{exc}")
            return EXIT_USAGE
        return 0

    try:
        if args.workbook is not None:
            cases = load_cases(args.workbook, args.sheet)
            definitions = cases.cases
            enabled_count = cases.enabled_count
            warnings = cases.warnings
            source_label = f"Excel {args.workbook}:{args.sheet}"
        else:
            case_set = load_case_set(args.cases, args.case_set)
            definitions = list(case_set.cases)
            enabled_count = case_set.enabled_count
            warnings = [
                f"{item.case_name} 已禁用" for item in case_set.cases if not item.enabled
            ]
            source_label = f"Catalog {args.cases}:{case_set.case_set_id}"
    except (CasesError, CaseCatalogError) as exc:
        _log(f"用例读取失败：{exc}")
        return EXIT_USAGE
    for warning in warnings:
        _log(f"提示：{warning}")

    items = expand_cases(definitions)
    if args.limit > 0:
        items = items[: args.limit]
    _log(
        f"批次 {batch_id}：{enabled_count} 个 Case，共 {len(items)} 轮"
        f"（来源 {source_label}）"
    )

    try:
        # 配置有问题要在前置检查里暴露，而不是跑到一半才发现。
        collector_config = CollectorConfig.load()
        driver = build_driver(
            DriverSpec(
                product=args.product,
                agent_id=args.agent,
                model_query=args.model,
                timeout_seconds=args.timeout,
                transcript_dir=None if args.no_transcript else out_dir / "transcripts",
                allow_tools=args.allow_tools,
            )
        )
        for line in driver.preflight():
            _log(line)
        if collector_config.enabled:
            _log(f"逐请求采集已启用：{collector_config.entry_url}")
            _log("⚠️ 被测产品的 baseUrl 必须指向这个入口，否则每一轮都会标未采集")
        else:
            _log("逐请求采集未启用（BENCH_COLLECTOR_ENABLED=0）")
    except (CollectorConfigError, DriverError) as exc:
        _log(f"前置检查失败：{exc}")
        return EXIT_USAGE

    if args.dry_run:
        for item in items:
            _log(f"  {item.position + 1:>3}. {item.case_name}#{item.run_no} {item.prompt[:40]}")
        driver.close()
        return 0

    options = BatchOptions(
        batch_id=batch_id,
        results_path=results_path,
        id_prefix=args.id_prefix,
        collect_usage=not args.no_usage,
    )

    collector: CollectorProxy | None = None
    try:
        # 和 Worker 共用锁；CLI 也会污染时间窗，不能绕开串行约束。
        with exclusive_worker_lock():
            if collector_config.enabled:
                collector = CollectorProxy.from_config(
                    collector_config,
                    ledger=LedgerWriter(collector_config.ledger_path(batch_id)),
                    proxy_id=batch_id,
                )
                collector.start()
            records = run_batch(
                items, driver=driver, options=options, report=_log, collector=collector
            )
    except (WorkerAlreadyRunning, DatabaseError) as exc:
        _log(f"无法开始跑批：{exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        _log("已中断；已完成的轮次都在 " + str(results_path))
        records = []
    finally:
        if collector is not None:
            collector.stop()
        driver.close()

    if not results_path.is_file():
        _log("没有任何结果落盘")
        return EXIT_CODES[Verdict.INVALID]

    database_path = out_dir / "results.db"
    count = build_database(results_path, database_path)
    _log(f"已汇总 {count} 轮 → {database_path}")
    if not args.no_xlsx:
        _log(f"已导出 → {export_xlsx(database_path, out_dir / 'results.xlsx')}")

    summary = summarize(results_path)
    _log(summary.as_text())
    return exit_code_for(records) if records else EXIT_CODES[summary.worst_verdict]


if __name__ == "__main__":
    raise SystemExit(main())
