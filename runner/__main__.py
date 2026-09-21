from __future__ import annotations

import argparse
import sys
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
from .catalog import CatalogError, list_model_choices, resolve_model_choice
from .client import ChatClient, DEFAULT_TURN_TIMEOUT_SECONDS
from .discovery import DiscoveryError, discover, has_session, health_check, session_status
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
        "--product", default="yonwork", help="产品维度：yonwork / workbuddy"
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
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 轮，0 表示不限")
    parser.add_argument("--no-usage", action="store_true", help="不采集端上 token 用量")
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
        endpoint = discover()
        health = health_check(endpoint)
    except (DiscoveryError, TransportError) as exc:
        _log(f"前置检查失败：{exc}")
        _log("检查顺序：应用在跑吗 → 脚本绕开 http_proxy 了吗 → 运行时文件在哪")
        return EXIT_USAGE
    _log(f"Host API：{endpoint.base_url}（来源 {endpoint.source}，健康 {health}）")

    try:
        if not has_session(session_status(endpoint)):
            _log("尚未登录（hasSession=false），先在应用里登录再跑，否则整批都是 Fail")
            return EXIT_USAGE
    except TransportError as exc:
        _log(f"登录态检查失败：{exc}")
        return EXIT_USAGE

    model_choice = None
    if args.model:
        try:
            model_choice = resolve_model_choice(list_model_choices(endpoint), args.model)
        except (CatalogError, TransportError) as exc:
            _log(f"模型解析失败：{exc}")
            return EXIT_USAGE
        _log(f"指定模型：{model_choice.label}")
    else:
        _log("未指定 --model，用智能体当前的默认模型")

    if args.dry_run:
        for item in items:
            _log(f"  {item.position + 1:>3}. {item.case_name}#{item.run_no} {item.prompt[:40]}")
        return 0

    client = ChatClient(
        endpoint,
        agent_id=args.agent,
        timeout_seconds=args.timeout,
        transcript_dir=None if args.no_transcript else out_dir / "transcripts",
        model_choice=model_choice,
    )
    options = BatchOptions(
        batch_id=batch_id,
        results_path=results_path,
        id_prefix=args.id_prefix,
        product=args.product,
        collect_usage=not args.no_usage,
    )

    try:
        records = run_batch(
            items, client=client, endpoint=endpoint, options=options, report=_log
        )
    except KeyboardInterrupt:
        _log("已中断；已完成的轮次都在 " + str(results_path))
        records = []

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
