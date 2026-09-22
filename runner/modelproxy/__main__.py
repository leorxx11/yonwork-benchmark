"""采集代理的命令行入口。

    .venv/bin/python -m runner.modelproxy selfcheck   # 离线自检 + 额外开销（默认）
    .venv/bin/python -m runner.modelproxy serve       # 按 .env 起一个长驻入口
    .venv/bin/python -m runner.modelproxy health      # 查入口是否可达（compose 健康检查用）

`selfcheck` 不需要 YonWork、WorkBuddy、NewAPI 或任何模型——上游是本文件里的桩。
所以它得到的是**代理自身在回环上的下限开销**，`网关 + 模型`那部分一概不含。
真实跑批的开销只会更大，把这个数当成「跑批多花了多少」是错的。

按 CLAUDE.md 七-2.6 的规矩报**中位数 + 最小/最大 + 样本数**，不报均值；
跨度超过关心的效应量就该标样本不足，这里一并算出来。
"""
from __future__ import annotations

import argparse
import json
import signal
import statistics
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..transport import build_opener
from .config import CollectorConfig, CollectorConfigError
from .ledger import ATTRIBUTED, OBSERVED, LedgerWriter, load_ledger, summarize
from .proxy import CollectorProxy


_CHUNKS = (
    {"model": "selfcheck", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
    {"model": "selfcheck", "choices": [{"index": 0, "delta": {"content": "ok"}}]},
    {"model": "selfcheck", "choices": [],
     "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13}},
)


class _Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-oneapi-request-id", "selfcheck-" + str(time.time_ns()))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        for chunk in _CHUNKS:
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _once(url: str, token: str, extra: dict[str, str]) -> float:
    request = urllib.request.Request(
        url,
        data=json.dumps({"model": "selfcheck", "stream": True,
                         "messages": [{"role": "user", "content": "selfcheck"}]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}", **extra},
        method="POST",
    )
    started = time.monotonic()
    with build_opener().open(request, timeout=20) as response:
        response.read()
    return (time.monotonic() - started) * 1000


def _spread(values: list[float]) -> dict[str, float | int]:
    return {"n": len(values), "median_ms": round(statistics.median(values), 2),
            "min_ms": round(min(values), 2), "max_ms": round(max(values), 2)}


def _settle(proxy: CollectorProxy, expected: int) -> list:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        done = [item for item in proxy.records() if item.record_status == "closed"]
        if len(done) >= expected:
            return done
        time.sleep(0.02)
    raise RuntimeError("等不到全部请求结束")


def selfcheck(rounds: int) -> int:
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"

    with tempfile.TemporaryDirectory(prefix="modelproxy-selfcheck-") as temporary:
        ledger_path = Path(temporary) / "model-requests.jsonl"
        proxy = CollectorProxy(upstream_url=upstream_url, upstream_key="selfcheck",
                               ledger=LedgerWriter(ledger_path), proxy_id="selfcheck")
        with proxy:
            direct = [_once(upstream_url + "/v1/chat/completions", "selfcheck", {})
                      for _ in range(rounds)]
            through = []
            for number in range(rounds):
                run_id = f"selfcheck-{number:03d}"
                proxy.register(run_id=run_id, product="selfcheck")
                through.append(_once(proxy.base_url + "/chat/completions",
                                     proxy.client_token, {"x-yonwork-run-id": run_id}))
                proxy.close_run(run_id)
            records = _settle(proxy, rounds)
        upstream.shutdown()
        upstream.server_close()

        replayed = load_ledger(ledger_path)
        summary = summarize(replayed)

    report = {
        "direct_to_stub": _spread(direct),
        "through_collector": _spread(through),
        "added_median_ms": round(statistics.median(through) - statistics.median(direct), 2),
        "first_output_ms": _spread([r.first_output_seconds * 1000 for r in records
                                    if r.first_output_seconds is not None]),
        "ledger": summary,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    measured = report["through_collector"]
    if measured["max_ms"] - measured["min_ms"] > measured["median_ms"]:
        # 七-2.6：跨度超过中位数就别拿这个数说事。
        print("样本不足：转发耗时跨度超过中位数，这批数不能用来声称开销水平")

    ok = (len(replayed) == rounds
          and all(record.attribution == ATTRIBUTED for record in replayed)
          and all(record.usage_status == OBSERVED for record in replayed)
          and all(record.upstream_request_id for record in replayed)
          and summary["unattributed"] == 0 and summary["incomplete"] == 0)
    print("自检通过" if ok else "自检未通过")
    return 0 if ok else 1


def serve(batch_id: str) -> int:
    """按 `.env` 起一个长驻入口，直到收到 Ctrl-C / SIGTERM。

    **跑批还没接进来**，所以这里不会有任何轮次被注册，经过的请求一律记
    `unattributed`。它现在的用途是：把产品的 baseUrl 指过来，手动发一条消息，
    验证「产品确实能连上这个入口」，不必先改主链路。
    """
    config = CollectorConfig.load()
    if not config.enabled:
        print("BENCH_COLLECTOR_ENABLED 不是 1，未启动（这是默认值）")
        return 1
    ledger_path = config.ledger_path(batch_id)
    proxy = CollectorProxy.from_config(config, ledger=LedgerWriter(ledger_path))
    proxy.start()
    print(f"采集入口：{config.entry_url}")
    print(f"健康检查：{config.health_url}")
    print(f"账本：{ledger_path}")
    print("⚠️ 跑批尚未接入，经过的请求都会记成 unattributed")
    stop = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_args: stop.set())
    try:
        stop.wait()
    finally:
        proxy.stop()
        print(f"\n已停止，共记录 {len(proxy.records())} 条请求")
    return 0


def health() -> int:
    """compose 健康检查用。**未启用时返回 0**——关着是默认状态，不是故障。"""
    try:
        config = CollectorConfig.load()
    except CollectorConfigError as exc:
        print(f"配置有问题：{exc}")
        return 1
    if not config.enabled:
        print("采集代理未启用（BENCH_COLLECTOR_ENABLED=0）")
        return 0
    try:
        with build_opener().open(config.health_url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"入口不可达：{config.health_url}（{type(exc).__name__}）")
        return 1
    print(json.dumps({"entry": config.entry_url, **payload}, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m runner.modelproxy", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    check = sub.add_parser("selfcheck", help="离线自检 + 额外开销测量（不需要任何产品）")
    check.add_argument("--rounds", type=int, default=15)
    listen = sub.add_parser("serve", help="按 .env 起一个长驻采集入口")
    listen.add_argument("--batch-id", default="collector-manual")
    sub.add_parser("health", help="查入口是否可达")
    args = parser.parse_args(argv)

    if args.command == "serve":
        return serve(args.batch_id)
    if args.command == "health":
        return health()
    return selfcheck(getattr(args, "rounds", 15))


if __name__ == "__main__":
    raise SystemExit(main())
