#!/usr/bin/env python3
"""隔离验证 YonWork Host API 标识是否传到模型入口；只保存元数据。

从仓库根运行 python -m scripts.probe_yonwork_correlation。
需先停止空闲 Worker；脚本持全局锁，创建临时模型账户，结束后删除。
使用本机假模型，不调用 NewAPI，不修改产品安装文件。
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from runner.catalog import list_model_choices
from runner.client import ChatClient
from runner.discovery import discover
from runner.job_store import exclusive_worker_lock
from runner.transport import build_opener, request_json


def main() -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    out = Path("results") / ("yonwork-correlation-" + stamp)
    out.mkdir(parents=True)
    evidence = {"probe_id": stamp, "requests": [], "turns": [], "cleanup": {}}
    token = secrets.token_hex(24)
    account_id = "correlation-" + secrets.token_hex(8)
    endpoint = discover()

    def save():
        (out / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.headers.get("Authorization") != "Bearer " + token:
                self.send_error(401)
                return
            # 请求头只保存明确允许的关联元数据；正文只取字段名。
            evidence["requests"].append({
                "received_at": time.time(), "path": self.path,
                "header_names": sorted(k.lower() for k in self.headers),
                "body_keys": sorted(body), "message_count": len(body.get("messages", [])),
                "model": body.get("model"), "stream": body.get("stream"),
                "correlation": {k: self.headers[k] for k in (
                    "traceparent", "x-yonwork-run-id", "x-yonclaw-run-id",
                    "x-benchmark-run-id", "x-yonwork-session-key", "x-yonclaw-session-key",
                ) if self.headers.get(k)},
            })
            payload = {
                "id": "probe-" + secrets.token_hex(8), "model": body.get("model"),
                "object": "chat.completion", "created": int(time.time()),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ENTRY_OK"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
            if body.get("stream"):
                payload["object"] = "chat.completion.chunk"
                payload["choices"][0]["delta"] = payload["choices"][0].pop("message")
                raw = ("data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n").encode()
                mime = "text/event-stream"
            else:
                raw, mime = json.dumps(payload).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence["listen_port"] = server.server_port
    evidence["temporary_account_id"] = account_id
    save()
    print("Evidence:", out, flush=True)
    try:
        with exclusive_worker_lock():
            baseline = request_json(endpoint.url("/api/provider-accounts"), token=endpoint.token)
            try:
                response = request_json(endpoint.url("/api/provider-accounts"), method="POST",
                    token=endpoint.token, timeout=30, payload={"account": {
                        "id": account_id, "vendorId": "custom", "label": "correlation-probe-" + stamp,
                        "authMode": "api_key", "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        "apiProtocol": "openai-completions", "model": "correlation-probe",
                        "fallbackModels": [], "enabled": True, "isDefault": False,
                    }, "apiKey": token})
                if not response.get("success"):
                    raise RuntimeError("temporary provider creation failed")
                account_id = response["account"]["id"]
                evidence["temporary_account_id"] = account_id
                save()
                choice = next(c for c in list_model_choices(endpoint) if c.provider_account_id == account_id)
                time.sleep(5)
                for number in (1, 2):
                    run = f"correlation-{stamp}-{number}"
                    injected = "00-" + secrets.token_hex(16) + "-" + secrets.token_hex(8) + "-01"
                    start_index = len(evidence["requests"])

                    def open_with_headers(url, *, payload, token, timeout, **_kwargs):
                        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
                            "Authorization": "Bearer " + token, "Content-Type": "application/json",
                            "Accept": "text/event-stream", "traceparent": injected,
                            "x-benchmark-run-id": run, "x-yonwork-run-id": run,
                        })
                        return build_opener().open(req, timeout=timeout)

                    row = {"run_id": run, "injected_traceparent": injected}
                    try:
                        with patch("runner.client.open_request", open_with_headers):
                            turn = ChatClient(endpoint, model_choice=choice, timeout_seconds=120).send(
                                benchmark_id=run, prompt="请只回复 ENTRY_OK，不调用工具。")
                        row.update(terminated_by=turn.terminated_by,
                                   answer_matches=(turn.answer or "").strip() == "ENTRY_OK",
                                   duration_seconds=turn.duration_seconds)
                    except Exception as exc:
                        row["error"] = type(exc).__name__
                    # 此分组仅用于隔离实验计数，不作为正式轮次关联依据。
                    row["observed_request_indices"] = list(range(start_index, len(evidence["requests"])))
                    captured = evidence["requests"][start_index:]
                    row["native_run_header_present"] = any(
                        any(key.endswith("run-id") for key in request["correlation"])
                        for request in captured)
                    row["injected_trace_propagated"] = any(
                        request["correlation"].get("traceparent", "").split("-")[1:2]
                        == injected.split("-")[1:2] for request in captured)
                    evidence["turns"].append(row)
                    save()
                    print("turn", number, {k: v for k, v in row.items() if k != "injected_traceparent"}, flush=True)
            finally:
                response = request_json(endpoint.url("/api/provider-accounts/" + account_id),
                                        method="DELETE", token=endpoint.token, timeout=30)
                evidence["cleanup"]["temporary_provider_deleted"] = bool(response.get("success"))
                after = request_json(endpoint.url("/api/provider-accounts"), token=endpoint.token)
                evidence["cleanup"]["provider_list_restored"] = baseline == after
                save()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        evidence["cleanup"]["listener_stopped"] = not thread.is_alive()
        save()
    print("cleanup", evidence["cleanup"], flush=True)
    return int(not (len(evidence["turns"]) == 2
                   and all(r.get("answer_matches") for r in evidence["turns"])
                   and all(evidence["cleanup"].values())))


if __name__ == "__main__":
    raise SystemExit(main())
