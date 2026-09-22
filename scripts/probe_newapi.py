#!/usr/bin/env python3
"""用真实的小额模型请求探活；输出 JSONL，不输出凭据或响应正文。"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner.modelproxy.config import CollectorConfig  # noqa: E402


def probe(route: str, url: str, key: str, model: str, stream: bool, timeout: float) -> dict:
    body = json.dumps({
        "model": model, "messages": [{"role": "user", "content": "Reply OK."}],
        "max_tokens": 8, "stream": stream,
    }).encode()
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
    })
    row = {"at": datetime.now(timezone.utc).isoformat(), "route": route,
           "stream": stream, "ok": False}
    started = time.monotonic()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            row["status"] = response.status
            row["headers_s"] = round(time.monotonic() - started, 3)
            row["request_id"] = response.headers.get("x-oneapi-request-id")
            if stream:
                done, output, errors = False, False, False
                for line in response:
                    if time.monotonic() - started > timeout:
                        raise TimeoutError()
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        done = True
                        break
                    if not data:
                        continue
                    payload = json.loads(data)
                    errors |= bool(payload.get("error"))
                    for choice in payload.get("choices") or []:
                        delta = choice.get("delta") or {}
                        output |= bool(delta.get("content") or delta.get("reasoning_content"))
                row.update(done=done, output=output)
                row["ok"] = response.status == 200 and done and output and not errors
            else:
                payload = json.load(response)
                row["ok"] = response.status == 200 and bool(payload.get("choices")) and not payload.get("error")
    except urllib.error.HTTPError as exc:
        row.update(status=exc.code, error="HTTPError")
        exc.close()
    except Exception as exc:
        # 异常文本可能带 URL/响应内容，只保留类型。
        row["error"] = type(exc).__name__
    row["elapsed_s"] = round(time.monotonic() - started, 3)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", choices=("newapi", "collector", "both"), default="both")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=0)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    if args.rounds < 1 or args.idle_seconds < 0 or args.timeout <= 0:
        parser.error("rounds >= 1, idle-seconds >= 0, timeout > 0")
    config = CollectorConfig.load()
    routes = {
        "newapi": (config.upstream_url.rstrip("/").removesuffix("/v1") + "/v1/chat/completions", config.upstream_key),
        "collector": (config.entry_url.rstrip("/") + "/chat/completions", config.client_token),
    }
    selected = routes if args.route == "both" else {args.route: routes[args.route]}
    if any(not key for _, key in selected.values()):
        parser.error("请先配置 .env 中对应的 BENCH_COLLECTOR_UPSTREAM_KEY / BENCH_COLLECTOR_CLIENT_TOKEN")
    failed = False
    for index in range(args.rounds):
        if index:
            time.sleep(args.idle_seconds)
        for route, (url, key) in selected.items():
            row = probe(route, url, key, args.model, args.stream, args.timeout)
            row["round"] = index + 1
            print(json.dumps(row), flush=True)
            failed |= not row["ok"]
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
