from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import runner.modelproxy.__main__ as cli
import runner.modelproxy.config as config_module
from runner.batch import BatchOptions, run_batch
from runner.drivers import UsageCollection
from runner.models import ChatTurn, TaskItem, Verdict, now_iso
from runner.modelproxy import (
    ATTRIBUTED,
    CollectorConfig,
    CollectorConfigError,
    CollectorProxy,
    INCOMPLETE,
    LATE,
    LedgerWriter,
    ModelRequestRecord,
    REJECTED,
    UNATTRIBUTED,
    load_ledger,
    summarize,
)
from runner.modelproxy.ledger import (
    COMPLETED,
    MISSING,
    OBSERVED,
    STREAM_TRUNCATED,
    TIMEOUT,
    UPSTREAM_ERROR,
)


PROMPT = "这句提示词绝对不该出现在账本里"
UPSTREAM_ID = "20260922-stub-request-id"


class _StubUpstream(BaseHTTPRequestHandler):
    """假网关。按请求里的 model 决定回什么，覆盖各条异常路径。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        self._send(200, json.dumps({"object": "list", "data": []}).encode())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw)
        self.server.seen.append({"path": self.path, "body": payload,  # type: ignore[attr-defined]
                                 "headers": dict(self.headers)})
        model = payload.get("model")
        if model == "boom":
            self._send(500, json.dumps({"error": "upstream exploded"}).encode())
        elif model == "slow":
            time.sleep(1.5)
            self._send(200, json.dumps({"model": model}).encode())
        elif model == "plain":
            body = json.dumps({
                "model": "resolved-plain",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
            }).encode()
            self._send(200, body)
        else:
            self._stream(truncated=model == "cut")

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-oneapi-request-id", UPSTREAM_ID)
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, *, truncated: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-oneapi-request-id", UPSTREAM_ID)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        # 第一片只有 role：先到但没有内容，**不能算首字**。
        chunks: list[dict] = [
            {"model": "resolved-stream", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"model": "resolved-stream", "choices": [{"index": 0, "delta": {"content": "他"}}]},
            {"model": "resolved-stream", "choices": [{"index": 0, "delta": {"content": "好"}}]},
        ]
        for chunk in chunks:
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
        if truncated:
            return  # 没有 usage、没有 [DONE]：断流
        tail = {"model": "resolved-stream", "choices": [],
                "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49}}
        self.wfile.write(b"data: " + json.dumps(tail).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _post(url: str, payload: dict, *, token: str, extra: dict[str, str] | None = None):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}", **(extra or {})},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()


class CollectorProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
        self.upstream.seen = []  # type: ignore[attr-defined]
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.temporary = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.temporary.name) / "model-requests.jsonl"
        self.proxy = CollectorProxy(
            upstream_url=f"http://127.0.0.1:{self.upstream.server_address[1]}",
            upstream_key="upstream-secret",
            ledger=LedgerWriter(self.ledger_path),
            proxy_id="test-proxy",
            timeout_seconds=0.6,
        )
        self.proxy.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.addCleanup(self.proxy.stop)

    # ---- 工具 -----------------------------------------------------------

    def send(self, *, model: str = "stream", run_id: str | None = None,
             header: str = "x-yonwork-run-id", token: str | None = None,
             path: str = "/chat/completions", stream: bool = True):
        extra = {header: run_id} if run_id else {}
        url = self.proxy.base_url + path
        return _post(url, {"model": model, "stream": stream,
                           "messages": [{"role": "user", "content": PROMPT}]},
                     token=token or self.proxy.client_token, extra=extra)

    def settled(self, expected: int) -> list[ModelRequestRecord]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            records = [item for item in self.proxy.records() if item.record_status == "closed"]
            if len(records) >= expected:
                return sorted(records, key=lambda item: item.sequence)
            time.sleep(0.02)
        self.fail(f"等不到 {expected} 条结束的记录")

    # ---- 归属 -----------------------------------------------------------

    def test_attributed_stream_records_metadata(self) -> None:
        self.proxy.register(run_id="bench-1", product="yonwork")
        status, _ = self.send(run_id="bench-1")
        self.assertEqual(status, 200)
        record, = self.settled(1)
        self.assertEqual(record.attribution, ATTRIBUTED)
        self.assertEqual(record.run_id, "bench-1")
        self.assertEqual(record.product, "yonwork")
        self.assertEqual(record.attribution_source, "x-yonwork-run-id")
        self.assertEqual(record.termination, COMPLETED)
        self.assertEqual(record.requested_model, "stream")
        self.assertEqual(record.response_model, "resolved-stream")
        self.assertEqual(record.upstream_request_id, UPSTREAM_ID)
        self.assertIsNone(record.upstream_attempts)
        self.assertEqual(record.usage_status, OBSERVED)
        self.assertEqual(record.usage["prompt_tokens"], 42)
        self.assertTrue(record.sse_done)
        # role-only 那一片不算：三片里只有两片有内容。
        self.assertEqual(record.output_events, 2)
        self.assertIsNotNone(record.first_output_seconds)
        self.assertLessEqual(record.first_output_seconds, record.duration_seconds)
        self.assertEqual(self.proxy.records_for("bench-1"), (record,))

    def test_workbuddy_header_also_correlates(self) -> None:
        self.proxy.register(run_id="bench-wb", product="workbuddy")
        self.send(run_id="bench-wb", header="X-Conversation-ID")
        record, = self.settled(1)
        self.assertEqual(record.attribution, ATTRIBUTED)
        self.assertEqual(record.attribution_source, "x-conversation-id")

    def test_unattributed_request_is_forwarded_not_refused(self) -> None:
        """没有关联头照样转发。拒掉等于我们把产品打断，然后算成产品的失败。"""
        self.proxy.register(run_id="bench-1", product="yonwork")
        status, _ = self.send()
        self.assertEqual(status, 200)
        record, = self.settled(1)
        self.assertEqual(record.attribution, UNATTRIBUTED)
        self.assertIsNone(record.run_id)
        self.assertEqual(record.termination, COMPLETED)
        self.assertEqual(len(self.upstream.seen), 1)

    def test_partial_header_value_does_not_correlate(self) -> None:
        """只认全等：带前缀的别的 ID 不能算本轮。"""
        self.proxy.register(run_id="bench-1", product="yonwork")
        self.send(run_id="bench-1-subagent")
        record, = self.settled(1)
        self.assertEqual(record.attribution, UNATTRIBUTED)

    def test_late_request_stays_with_the_closed_run(self) -> None:
        self.proxy.register(run_id="bench-1", product="yonwork")
        self.proxy.register(run_id="bench-2", product="yonwork")
        self.send(run_id="bench-1")
        self.proxy.close_run("bench-1")
        self.send(run_id="bench-1")
        first, late = self.settled(2)
        self.assertEqual(first.attribution, ATTRIBUTED)
        self.assertEqual(late.attribution, LATE)
        self.assertEqual(late.run_id, "bench-1")
        self.assertEqual(self.proxy.records_for("bench-2"), ())

    # ---- 拒绝 -----------------------------------------------------------

    def test_wrong_credential_is_refused_and_not_forwarded(self) -> None:
        self.proxy.register(run_id="bench-1", product="yonwork")
        status, _ = self.send(run_id="bench-1", token="not-the-token")
        self.assertEqual(status, 401)
        record, = self.settled(1)
        self.assertEqual(record.attribution, REJECTED)
        self.assertEqual(record.http_status, 401)
        self.assertEqual(self.upstream.seen, [])

    def test_model_list_requires_the_credential(self) -> None:
        """产品的「测试连接」打的就是模型列表。放过去的话 API Key 填错也显示成功。"""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = self.proxy.base_url + "/models"
        request = urllib.request.Request(url, headers={"Authorization": "Bearer wrong"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(request, timeout=5)
        with caught.exception:
            self.assertEqual(caught.exception.code, 401)
        right = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.proxy.client_token}"}
        )
        with opener.open(right, timeout=5) as response:
            self.assertEqual(response.status, 200)
        # 模型列表不是模型调用，不该进账本。
        self.assertEqual(self.proxy.records(), ())

    def test_unknown_path_is_refused(self) -> None:
        proxy_url = self.proxy.base_url.removesuffix("/v1")
        status, _ = _post(proxy_url + "/nope", {"model": "stream"},
                          token=self.proxy.client_token)
        self.assertEqual(status, 404)
        record, = self.settled(1)
        self.assertEqual(record.attribution, REJECTED)
        self.assertEqual(self.upstream.seen, [])

    def test_chunked_request_body_fails_loudly(self) -> None:
        """不实现 chunked 解码可以，静默转发空 body 不行。"""
        self.proxy.register(run_id="bench-1", product="yonwork")
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        connection.putrequest("POST", "/v1/chat/completions")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Authorization", f"Bearer {self.proxy.client_token}")
        connection.putheader("x-yonwork-run-id", "bench-1")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        payload = json.dumps({"model": "stream", "messages": []}).encode()
        connection.send(b"%x\r\n%s\r\n0\r\n\r\n" % (len(payload), payload))
        response = connection.getresponse()
        self.assertEqual(response.status, 501)
        response.read()
        connection.close()
        record, = self.settled(1)
        self.assertEqual(record.attribution, REJECTED)
        self.assertEqual(record.error_kind, "chunked-request-body")
        self.assertEqual(self.upstream.seen, [])

    # ---- 异常路径 -------------------------------------------------------

    def test_truncated_stream_keeps_missing_usage(self) -> None:
        self.proxy.register(run_id="bench-1", product="yonwork")
        self.send(model="cut", run_id="bench-1")
        record, = self.settled(1)
        self.assertEqual(record.termination, STREAM_TRUNCATED)
        self.assertEqual(record.usage_status, MISSING)
        self.assertIsNone(record.usage)   # 缺就是缺，不补零
        self.assertEqual(record.output_events, 2)

    def test_upstream_error_status_is_recorded(self) -> None:
        self.proxy.register(run_id="bench-1", product="yonwork")
        status, _ = self.send(model="boom", run_id="bench-1")
        self.assertEqual(status, 500)
        record, = self.settled(1)
        self.assertEqual(record.http_status, 500)
        self.assertEqual(record.termination, UPSTREAM_ERROR)
        self.assertEqual(record.usage_status, MISSING)

    def test_timeout_is_recorded_without_retrying(self) -> None:
        self.proxy.register(run_id="bench-1", product="yonwork")
        self.send(model="slow", run_id="bench-1")
        record, = self.settled(1)
        self.assertEqual(record.termination, TIMEOUT)
        # 代理不重试：产品发了一次，上游就只该看到一次。
        self.assertEqual(len(self.upstream.seen), 1)

    def test_non_stream_response_yields_usage(self) -> None:
        self.proxy.register(run_id="bench-1", product="yonwork")
        status, _ = self.send(model="plain", run_id="bench-1", stream=False)
        self.assertEqual(status, 200)
        record, = self.settled(1)
        self.assertEqual(record.termination, COMPLETED)
        self.assertEqual(record.usage_status, OBSERVED)
        self.assertEqual(record.usage["prompt_tokens"], 11)
        self.assertEqual(record.response_model, "resolved-plain")

    # ---- 账本 -----------------------------------------------------------

    def test_ledger_holds_no_prompt_or_credential(self) -> None:
        self.proxy.register(run_id="bench-1", product="yonwork")
        self.send(run_id="bench-1")
        self.settled(1)
        raw = self.ledger_path.read_text(encoding="utf-8")
        self.assertNotIn(PROMPT, raw)
        self.assertNotIn(self.proxy.client_token, raw)
        self.assertNotIn("upstream-secret", raw)
        self.assertNotIn("Authorization", raw)
        self.assertIn("authorization", raw)   # 只留请求头的名字

    def test_request_is_written_before_it_finishes(self) -> None:
        self.proxy.register(run_id="bench-1", product="yonwork")
        self.send(run_id="bench-1")
        self.settled(1)
        phases = [json.loads(line)["record_status"]
                  for line in self.ledger_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(phases, ["open", "closed"])


    def test_healthz_answers_without_credential(self) -> None:
        self.proxy.register(run_id="bench-1", product="yonwork")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = self.proxy.base_url.removesuffix("/v1")
        with opener.open(base + "/healthz", timeout=5) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["open_runs"], 1)
        self.proxy.close_run("bench-1")
        with opener.open(base + "/healthz", timeout=5) as response:
            self.assertEqual(json.loads(response.read())["open_runs"], 0)
        # 健康检查不该进账本：记了会把「这一轮发了几次模型请求」冲淡。
        self.assertEqual(self.proxy.records(), ())


class CollectorConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        # 不读仓库里真实的 .env，否则测试行为会随本机配置变化。
        patcher = unittest.mock.patch.object(config_module, "load_env_file", return_value={})
        self.env_file = patcher.start()
        self.addCleanup(patcher.stop)
        environment = unittest.mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        stale = [name for name in os.environ if name.startswith("BENCH_COLLECTOR_")]
        for name in (*stale, "NEWAPI_BASE_URL"):
            os.environ.pop(name, None)

    def test_disabled_by_default(self) -> None:
        loaded = CollectorConfig.load()
        self.assertFalse(loaded.enabled)
        self.assertEqual(loaded.port, 3312)
        self.assertEqual(loaded.entry_url, "http://127.0.0.1:3312/v1")
        self.assertEqual(loaded.upstream_url, "http://127.0.0.1:3000")

    def test_enabled_without_credentials_fails_loudly(self) -> None:
        os.environ["BENCH_COLLECTOR_ENABLED"] = "1"
        with self.assertRaises(CollectorConfigError) as caught:
            CollectorConfig.load()
        self.assertIn("BENCH_COLLECTOR_UPSTREAM_KEY", str(caught.exception))
        self.assertIn("BENCH_COLLECTOR_CLIENT_TOKEN", str(caught.exception))

    def test_dot_env_is_read_when_environment_is_empty(self) -> None:
        """这条守的是 2026-09-21 踩过的坑：只读 os.environ 会静默忽略 `.env`。"""
        self.env_file.return_value = {"BENCH_COLLECTOR_PORT": "3399"}
        self.assertEqual(CollectorConfig.load().port, 3399)

    def test_environment_wins_over_dot_env(self) -> None:
        self.env_file.return_value = {"BENCH_COLLECTOR_PORT": "3399"}
        os.environ["BENCH_COLLECTOR_PORT"] = "3400"
        self.assertEqual(CollectorConfig.load().port, 3400)

    def test_upstream_falls_back_to_newapi_base_url(self) -> None:
        os.environ["NEWAPI_BASE_URL"] = "http://127.0.0.1:3005/"
        self.assertEqual(CollectorConfig.load().upstream_url, "http://127.0.0.1:3005")

    def test_reserved_port_is_rejected(self) -> None:
        os.environ["BENCH_COLLECTOR_PORT"] = "3211"   # YonWork Host API
        with self.assertRaises(CollectorConfigError):
            CollectorConfig.load()

    def test_unparsable_flag_is_rejected(self) -> None:
        os.environ["BENCH_COLLECTOR_ENABLED"] = "maybe"
        with self.assertRaises(CollectorConfigError):
            CollectorConfig.load()

    def test_ledger_path_is_per_batch(self) -> None:
        path = CollectorConfig.load().ledger_path("web-20260922-x")
        self.assertEqual(path.parent.name, "web-20260922-x")
        self.assertEqual(path.name, "model-requests.jsonl")

    def test_health_command_is_green_while_disabled(self) -> None:
        """关着是默认状态，不是故障——健康检查不能因此把 Worker 标成 unhealthy。"""
        self.assertEqual(cli.health(), 0)

    def test_health_command_reports_unreachable_entry(self) -> None:
        os.environ.update({"BENCH_COLLECTOR_ENABLED": "1",
                           "BENCH_COLLECTOR_UPSTREAM_KEY": "k",
                           "BENCH_COLLECTOR_CLIENT_TOKEN": "t",
                           "BENCH_COLLECTOR_PORT": "3398"})
        self.assertEqual(cli.health(), 1)


class _ProxyDriver:
    """假驱动：`run_turn` 时**真的**往采集入口发一个请求，模拟被测产品的调用。

    `routed=False` 模拟「产品的 baseUrl 没指向我们」——这是接进跑批之后最容易
    出现、也最容易被记成假数据的那种配置错误。
    """

    product = "yonwork"

    def __init__(self, proxy: CollectorProxy, *, routed: bool = True, calls: int = 1) -> None:
        self.proxy = proxy
        self.routed = routed
        self.calls = calls

    def preflight(self):
        return ()

    def session_key(self, benchmark_id: str) -> str:
        return f"agent:main:{benchmark_id}".lower()

    def run_turn(self, *, benchmark_id: str, prompt: str):
        if self.routed:
            for _ in range(self.calls):
                _post(self.proxy.base_url + "/chat/completions",
                      {"model": "stream", "stream": True, "messages": []},
                      token=self.proxy.client_token,
                      extra={"x-yonwork-run-id": benchmark_id})
        return ChatTurn(
            benchmark_id=benchmark_id, session_key=self.session_key(benchmark_id),
            prompt=prompt, started_at=now_iso(), ended_at=now_iso(), duration_seconds=1.0,
            run_id=benchmark_id, answer="ok", terminated_by="chat.complete",
            stop_reason="stop", http_status=200,
        )

    def collect_usage(self, turn):
        return UsageCollection()

    def enrich(self, turn):
        return turn

    def close(self) -> None:
        return None


class BatchWiringTests(unittest.TestCase):
    """`batch.run_batch` 接上采集代理之后的行为。"""

    def setUp(self) -> None:
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
        self.upstream.seen = []  # type: ignore[attr-defined]
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.proxy = CollectorProxy(
            upstream_url=f"http://127.0.0.1:{self.upstream.server_address[1]}",
            upstream_key="upstream-secret",
            ledger=LedgerWriter(self.root / "model-requests.jsonl"),
            proxy_id="batch-test",
        )
        self.proxy.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.addCleanup(self.proxy.stop)

    def _run(self, driver, *, count: int = 1, collector: CollectorProxy | None = None):
        items = [TaskItem(position=index, case_name="smoke", run_no=index + 1, prompt="hi")
                 for index in range(count)]
        options = BatchOptions(batch_id="batch-test", results_path=self.root / "results.jsonl")
        return run_batch(items, driver=driver, options=options, collector=collector)

    def test_disabled_collector_is_not_reported_as_zero(self) -> None:
        """没开采集就得说「没开」。报 0 次调用和六-3 那个端上漏记一模一样。"""
        record, = self._run(_ProxyDriver(self.proxy, routed=False))
        self.assertEqual(record.model_calls.status, "disabled")
        self.assertEqual(record.model_calls.requests, ())
        self.assertEqual(record.verdict, Verdict.PASS)

    def test_routed_turn_records_its_requests(self) -> None:
        record, = self._run(_ProxyDriver(self.proxy, calls=2), collector=self.proxy)
        self.assertEqual(record.model_calls.status, "observed")
        self.assertEqual(len(record.model_calls.requests), 2)
        self.assertTrue(all(item["run_id"] == record.benchmark_id
                            for item in record.model_calls.requests))
        self.assertTrue(all(item["attribution"] == ATTRIBUTED
                            for item in record.model_calls.requests))
        self.assertIn("model-requests.jsonl", record.model_calls.ledger_path)

    def test_enabled_but_unrouted_turn_is_flagged_not_zeroed(self) -> None:
        """采集开着却一个请求都没经过 = baseUrl 没指过来，不是产品没调模型。"""
        record, = self._run(_ProxyDriver(self.proxy, routed=False), collector=self.proxy)
        self.assertEqual(record.model_calls.status, "unavailable")
        self.assertIn("baseUrl", record.model_calls.detail)
        self.assertIn("没有任何请求经过采集入口", record.note)

    def test_each_turn_gets_its_own_requests(self) -> None:
        records = self._run(_ProxyDriver(self.proxy), count=3, collector=self.proxy)
        self.assertEqual([len(item.model_calls.requests) for item in records], [1, 1, 1])
        owners = {item.model_calls.requests[0]["run_id"] for item in records}
        self.assertEqual(owners, {item.benchmark_id for item in records})

    def test_records_survive_a_failing_turn(self) -> None:
        """轮次抛异常也要收尾，否则下一轮的请求会被算进这一轮。"""
        driver = _ProxyDriver(self.proxy)
        original = driver.run_turn

        def explode(*, benchmark_id: str, prompt: str):
            original(benchmark_id=benchmark_id, prompt=prompt)
            raise RuntimeError("产品挂了")

        driver.run_turn = explode
        first, second = self._run(driver, count=2, collector=self.proxy)
        self.assertEqual(first.verdict, Verdict.ERROR)
        self.assertEqual(len(first.model_calls.requests), 1)
        self.assertEqual(len(second.model_calls.requests), 1)
        self.assertNotEqual(first.model_calls.requests[0]["run_id"],
                            second.model_calls.requests[0]["run_id"])

    def test_record_json_round_trips(self) -> None:
        record, = self._run(_ProxyDriver(self.proxy), collector=self.proxy)
        payload = json.loads(json.dumps(record.to_json(), ensure_ascii=False))
        self.assertEqual(payload["model_calls"]["status"], "observed")
        self.assertEqual(len(payload["model_calls"]["requests"]), 1)


class LedgerReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "model-requests.jsonl"

    def _record(self, sequence: int, **overrides) -> ModelRequestRecord:
        fields = {"request_id": f"p-{sequence:05d}", "proxy_id": "p", "sequence": sequence,
                  "received_at": "2026-09-22T10:00:00+08:00", "path": "/v1/chat/completions"}
        return ModelRequestRecord(**{**fields, **overrides})

    def test_replay_merges_phases_and_is_idempotent(self) -> None:
        writer = LedgerWriter(self.path)
        record = self._record(1, run_id="bench-1", attribution=ATTRIBUTED)
        writer.write(record)
        record.record_status = "closed"
        record.termination = COMPLETED
        record.usage = {"prompt_tokens": 5}
        record.usage_status = OBSERVED
        writer.write(record)
        once = load_ledger(self.path)
        self.assertEqual(len(once), 1)
        self.assertEqual(once[0].termination, COMPLETED)
        # 同一份文件再追加一遍（重放/重复导入）不该变成两条。
        self.path.write_text(self.path.read_text() * 2, encoding="utf-8")
        self.assertEqual(len(load_ledger(self.path)), 1)

    def test_open_only_record_is_marked_incomplete(self) -> None:
        writer = LedgerWriter(self.path)
        writer.write(self._record(1, run_id="bench-1", attribution=ATTRIBUTED))
        record, = load_ledger(self.path)
        self.assertEqual(record.termination, INCOMPLETE)
        self.assertEqual(record.record_status, "incomplete")

    def test_summary_separates_coverage_from_totals(self) -> None:
        closed = self._record(1, run_id="bench-1", attribution=ATTRIBUTED,
                              record_status="closed", termination=COMPLETED,
                              usage={"prompt_tokens": 10, "completion_tokens": 3},
                              usage_status=OBSERVED)
        no_usage = self._record(2, run_id="bench-1", attribution=ATTRIBUTED,
                                record_status="closed", termination=STREAM_TRUNCATED)
        outsider = self._record(3, attribution=UNATTRIBUTED, record_status="closed",
                                termination=COMPLETED)
        summary = summarize([closed, no_usage, outsider])
        self.assertEqual(summary["attributed"], 2)
        self.assertEqual(summary["unattributed"], 1)
        self.assertEqual(summary["usage_observed"], 1)
        self.assertEqual(summary["usage_missing"], 1)
        self.assertEqual(summary["input_tokens_observed"], 10)
        self.assertIsNone(summary["upstream_attempts"])

    def test_summary_reports_none_not_zero_when_nothing_observed(self) -> None:
        record = self._record(1, run_id="bench-1", attribution=ATTRIBUTED,
                              record_status="closed", termination=STREAM_TRUNCATED)
        summary = summarize([record])
        self.assertIsNone(summary["input_tokens_observed"])
        self.assertEqual(summary["usage_missing"], 1)


if __name__ == "__main__":
    unittest.main()
