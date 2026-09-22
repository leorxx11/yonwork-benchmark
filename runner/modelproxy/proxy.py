from __future__ import annotations

import json
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ..models import now_iso
from ..transport import build_opener
from .config import CollectorConfig
from .ledger import (
    ATTRIBUTED,
    CLIENT_DISCONNECTED,
    COMPLETED,
    LATE,
    MISSING,
    OBSERVED,
    REFUSED,
    REJECTED,
    STREAM_TRUNCATED,
    TIMEOUT,
    TRANSPORT_ERROR,
    UNATTRIBUTED,
    UPSTREAM_ERROR,
    LedgerWriter,
    ModelRequestRecord,
)


# 产品**原生**的轮次标识请求头，2026-09-22 实测严格等于 BenchmarkId。
# 顺序即优先级；加产品时在这里加一行，不要在别处另写一套匹配。
# ⚠️ 只认**全等**，不认包含：包含匹配在探针阶段是探索手段，
# 拿来做归属会把带前缀的别的 ID 误判成本轮。
CORRELATION_HEADERS: tuple[str, ...] = (
    "x-yonwork-run-id",      # YonWork 1.0.8
    "x-yonclaw-run-id",      # 同一个值的内部代号版本
    "x-conversation-id",     # WorkBuddy 5.5.6
    "x-benchmark-run-id",    # 我们自己注入的，仅在产品没有原生头时兜底
)

# 透传给网关，后台日志才对得上我们这一层的归属。鉴权头不在此列，由代理重签。
FORWARDED_HEADERS: tuple[str, ...] = (*CORRELATION_HEADERS, "traceparent")

_UPSTREAM_ID_HEADERS = ("x-oneapi-request-id", "x-newapi-request-id", "x-request-id")

# 非流式响应缓冲上限。只为取 usage / model，正文读完即弃，不进账本。
_MAX_BUFFERED_BODY = 1 << 20


class ProxyError(RuntimeError):
    pass


@dataclass(slots=True)
class _Registration:
    run_id: str
    product: str
    closed: bool = False
    requests: list[str] = field(default_factory=list)


class CollectorProxy:
    """`产品 → 采集代理 → NewAPI → 上游模型` 里的中间那一跳。

    **只观察，不干预**：不重试、不改请求体、不改产品的请求次数。
    代理自己失败时如实记 `transport-error`，不替产品重发——重发会让
    「产品发了几次」这个被测量变成我们自己造的数。

    归属策略（和 `scripts/probe_model_entry.py` 那个隔离探针**刻意不同**）：

    | 情况 | 探针 | 这里 | 为什么 |
    |---|---|---|---|
    | 凭据不对 | 401 | 401，记 `rejected` | 不是被测产品发的，挡掉才算隔离 |
    | 路径不认识 | 404 | 404，记 `rejected` | 同上 |
    | 有凭据、没有关联头 | 404 | **照常转发**，记 `unattributed` | 拒掉会改变产品行为 |
    | 关联头命中已收尾的轮次 | 410 | **照常转发**，记 `late`，仍归旧轮 | 同上 |

    探针要的是「证明能隔离」，可以拒；正式采集要的是「不影响被测对象」。
    子代理或辅助模型如果不带关联头，拒掉就等于我们把产品打断了，
    然后还会把这次打断算成产品的失败。
    """

    def __init__(
        self,
        *,
        upstream_url: str,
        upstream_key: str,
        ledger: LedgerWriter,
        bind: str = "127.0.0.1",
        port: int = 0,
        proxy_id: str | None = None,
        client_token: str | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        upstream = urlsplit(upstream_url)
        if not upstream.scheme or not upstream.netloc:
            raise ProxyError(f"上游地址不完整：{upstream_url}")
        self._upstream_base = f"{upstream.scheme}://{upstream.netloc}{upstream.path.rstrip('/')}"
        self._upstream_key = upstream_key
        self._ledger = ledger
        self._timeout_seconds = timeout_seconds
        self._proxy_id = proxy_id or (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
        # 产品侧配置成 apiKey 的那个值。有了它，同端口上的外部流量才挡得住。
        self._client_token = client_token or secrets.token_hex(24)
        self._lock = threading.Lock()
        self._sequence = 0
        self._runs: dict[str, _Registration] = {}
        self._records: dict[str, ModelRequestRecord] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bind = bind
        self._port = port

    # ---- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        if self._server is not None:
            raise ProxyError("采集代理已经在运行")
        try:
            server = _Server((self._bind, self._port), _Handler)
        except OSError as exc:
            # 裸 OSError 只说 "Address already in use"，看不出是谁占着。
            # 端口是固定的，所以占用者基本只有两种：没退干净的上一个 Worker，
            # 或者还开着的 `python -m runner.modelproxy serve`。
            raise ProxyError(
                f"采集入口 {self._bind}:{self._port} 起不来（{exc.strerror or exc}）："
                "端口可能被上一个 Worker 或 `runner.modelproxy serve` 占着；"
                "关掉它，或改 BENCH_COLLECTOR_PORT"
            ) from exc
        server.daemon_threads = True
        server.proxy = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def __enter__(self) -> "CollectorProxy":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        if self._server is None:
            raise ProxyError("采集代理还没启动")
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        """配进产品的 baseUrl。产品自己会在后面接 `/chat/completions`。"""
        return f"http://{self._bind}:{self.port}/v1"

    @property
    def client_token(self) -> str:
        return self._client_token

    @property
    def proxy_id(self) -> str:
        return self._proxy_id

    @property
    def ledger_path(self) -> Path:
        return self._ledger.path

    # ---- 轮次注册 -------------------------------------------------------

    def register(self, *, run_id: str, product: str) -> None:
        """预注册一轮。**先注册再发起**，否则本轮请求会记成未归属。"""
        if not run_id:
            raise ProxyError("run_id 不能为空")
        with self._lock:
            if run_id in self._runs:
                raise ProxyError(f"轮次已注册：{run_id}")
            self._runs[run_id] = _Registration(run_id=run_id, product=product)

    def close_run(self, run_id: str) -> None:
        """产品已终止本轮。之后再来的同标识请求记 `late`，**仍归这一轮**。

        收尾只改归属标记，不代表「不会再有请求」——安静一段时间只是等待策略，
        单独证明不了任务已经没有后台调用。
        """
        with self._lock:
            registration = self._runs.get(run_id)
            if registration is not None:
                registration.closed = True

    def records_for(self, run_id: str) -> tuple[ModelRequestRecord, ...]:
        with self._lock:
            registration = self._runs.get(run_id)
            ids = tuple(registration.requests) if registration else ()
            return tuple(self._records[key] for key in ids if key in self._records)

    def records(self) -> tuple[ModelRequestRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    @property
    def open_run_count(self) -> int:
        with self._lock:
            return sum(1 for item in self._runs.values() if not item.closed)

    # ---- 记账 -----------------------------------------------------------

    def upstream_for(self, path: str) -> str:
        """保留客户端路径里 `/v1` 之后的部分，别的 OpenAI 路由也能透明转发。"""
        marker = path.find("/v1/")
        suffix = path[marker:] if marker >= 0 else "/v1/chat/completions"
        return self._upstream_base + suffix

    def begin(self, *, path: str, headers, body: bytes) -> ModelRequestRecord:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        record = ModelRequestRecord(
            request_id=f"{self._proxy_id}-{sequence:05d}",
            proxy_id=self._proxy_id,
            sequence=sequence,
            received_at=now_iso(),
            path=path,
            header_names=tuple(sorted({name.lower() for name in headers})),
        )
        try:
            payload = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
            record.error_kind = "request-not-json"
        if isinstance(payload, dict):
            record.requested_model = payload.get("model")
            record.stream = payload.get("stream")
            messages = payload.get("messages")
            record.message_count = len(messages) if isinstance(messages, list) else None

        run_id, source = self._correlate(headers)
        with self._lock:
            registration = self._runs.get(run_id or "")
            if registration is not None:
                record.run_id = registration.run_id
                record.product = registration.product
                record.attribution = LATE if registration.closed else ATTRIBUTED
                record.attribution_source = source
                registration.requests.append(record.request_id)
            self._records[record.request_id] = record

        if not self.authorized(headers.get("Authorization", "")):
            record.attribution = REJECTED
            record.termination = REFUSED
            record.http_status = 401
            record.error_detail = "凭据不匹配"
        elif "/v1/" not in path:
            record.attribution = REJECTED
            record.termination = REFUSED
            record.http_status = 404
            record.error_detail = "未知路径"
        self._ledger.write(record)
        return record

    def finish(self, record: ModelRequestRecord, started: float | None = None) -> None:
        if started is not None:
            record.duration_seconds = round(time.monotonic() - started, 6)
        record.ended_at = now_iso()
        record.usage_status = OBSERVED if record.usage else MISSING
        record.record_status = "closed"
        self._ledger.write(record)

    def authorized(self, header_value: str) -> bool:
        return secrets.compare_digest(header_value, f"Bearer {self._client_token}")

    def _correlate(self, headers) -> tuple[str | None, str]:
        """只认全等。命中不了就返回空，**不按时间窗猜**。"""
        with self._lock:
            known = set(self._runs)
        for name in CORRELATION_HEADERS:
            value = (headers.get(name) or "").strip()
            if value and value in known:
                return value, name
        return None, ""

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def upstream_key(self) -> str:
        return self._upstream_key

    @classmethod
    def from_config(
        cls, config: "CollectorConfig", *, ledger: LedgerWriter, proxy_id: str | None = None
    ) -> "CollectorProxy":
        """按 `.env` / 环境变量构造。端口固定，凭据来自配置而**不是随机生成**——
        产品配置里存着这个令牌，每次启动换一个的话产品就再也连不上了。
        """
        config.require_credentials()
        return cls(
            upstream_url=config.upstream_url,
            upstream_key=config.upstream_key,
            ledger=ledger,
            bind=config.bind,
            port=config.port,
            proxy_id=proxy_id,
            client_token=config.client_token,
            timeout_seconds=config.timeout_seconds,
        )


def _upstream_request_id(headers) -> str | None:
    for name in _UPSTREAM_ID_HEADERS:
        value = headers.get(name)
        if value:
            return value
    return None


def _has_output(payload: object) -> bool:
    """这一片算不算「首个有效输出」。

    role-only 和空 delta 不算——它们先到，算进去首字就测得偏早。
    工具参数分片算，否则工具调用那一路会被当成「一直没输出」。
    """
    if not isinstance(payload, dict):
        return False
    for choice in payload.get("choices") or ():
        if not isinstance(choice, dict):
            continue
        part = choice.get("delta") or choice.get("message") or {}
        if not isinstance(part, dict):
            continue
        if part.get("content") or part.get("reasoning_content") or part.get("tool_calls"):
            return True
    return False


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """客户端半路断开是**正常现象**，不该打一整屏 traceback。

        实测 WorkBuddy 重试时会重置 keep-alive 连接，默认行为会把它打成看起来
        像代理崩了的堆栈，把跑批日志里真正的错误淹掉。断开本身已经记在账本的
        `client-disconnected` 里，这里只放过连接类异常，别的照常抛。
        """
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BenchCollector/1"

    def log_message(self, *_args: object) -> None:  # 访问日志会带路径，交给账本
        pass

    @property
    def _proxy(self) -> CollectorProxy:
        return self.server.proxy  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        """模型列表等非模型调用**不记账本**：记了会把「这一轮发了几次模型请求」冲淡。"""
        proxy = self._proxy
        if self.path.rstrip("/") == "/healthz":
            # 免鉴权，但只回状态不回任何凭据或轮次内容。绑的是 loopback。
            self._json(200, {"ok": True, "proxy_id": proxy.proxy_id,
                             "open_runs": proxy.open_run_count})
            return
        if "/v1/" not in self.path:
            self._json(404, {"error": "unknown collector route"})
            return
        if not proxy.authorized(self.headers.get("Authorization", "")):
            # 凭据也要在这里查。产品的「测试连接」打的就是模型列表：
            # 放过去的话，API Key 填错时测试仍然显示成功，
            # 真发消息才每轮 401——又是一个「看起来正常」型故障。
            self._json(401, {"error": "incorrect collector credential"})
            return
        request = urllib.request.Request(
            proxy.upstream_for(self.path),
            headers={"Accept": "application/json", "Authorization": f"Bearer {proxy.upstream_key}"},
        )
        try:
            with build_opener().open(request, timeout=30) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        except Exception:  # noqa: BLE001 —— 探模型列表失败不该拖垮跑批
            self._json(502, {"error": "collector upstream failure"})
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        proxy = self._proxy
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
        length = int(self.headers.get("Content-Length") or 0)
        body = b"" if chunked else (self.rfile.read(length) if length > 0 else b"")
        record = proxy.begin(path=self.path, headers=self.headers, body=body)
        if chunked:
            # 实测两款产品都带 Content-Length，所以这里不实现 chunked 解码。
            # 但**必须当场报错**：静默转发一个空 body 会让产品收到莫名其妙的回答，
            # 而账本看起来一切正常——正是这个项目反复栽进去的那种假数据。
            record.attribution = REJECTED
            record.termination = REFUSED
            record.http_status = 501
            record.error_kind = "chunked-request-body"
            record.error_detail = "请求体是 chunked 编码，采集代理未实现解码"
        if record.termination == REFUSED:
            self._json(record.http_status or 400, {"error": record.error_detail})
            proxy.finish(record)
            return
        self._forward(record, body)

    def _forward(self, record: ModelRequestRecord, body: bytes) -> None:
        proxy = self._proxy
        headers = {
            "Content-Type": "application/json",
            "Accept": self.headers.get("Accept", "text/event-stream"),
            "Authorization": f"Bearer {proxy.upstream_key}",
        }
        for name in FORWARDED_HEADERS:
            value = self.headers.get(name)
            if value:
                headers[name] = value
        request = urllib.request.Request(
            proxy.upstream_for(self.path), data=body, headers=headers, method="POST"
        )
        started = time.monotonic()
        try:
            response = build_opener().open(request, timeout=proxy.timeout_seconds)
        except urllib.error.HTTPError as exc:
            response = exc
            record.termination = UPSTREAM_ERROR
        except Exception as exc:  # noqa: BLE001 —— 连不上网关是我们这层的问题
            reason = getattr(exc, "reason", None)
            timed_out = isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(
                reason, (TimeoutError, socket.timeout)
            )
            record.termination = TIMEOUT if timed_out else TRANSPORT_ERROR
            record.error_kind = type(exc).__name__
            try:
                self._json(502, {"error": "collector upstream failure"})
            except OSError:
                record.termination = CLIENT_DISCONNECTED
            proxy.finish(record, started)
            return

        with response:
            record.http_status = response.status
            record.upstream_request_id = _upstream_request_id(response.headers)
            content_type = response.headers.get("Content-Type", "application/json")
            streaming = "event-stream" in content_type.lower()
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            content_length = response.headers.get("Content-Length")
            if content_length and not streaming:
                self.send_header("Content-Length", content_length)
            else:
                # 边收边转发就不能预先知道长度，只能靠关闭连接界定响应结束。
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            pending = b""
            buffered = bytearray()
            while True:
                try:
                    chunk = response.read1(65536)
                except (TimeoutError, socket.timeout) as exc:
                    record.termination = TIMEOUT
                    record.error_kind = type(exc).__name__
                    break
                except Exception as exc:  # noqa: BLE001 —— 上游断流
                    record.termination = STREAM_TRUNCATED
                    record.error_kind = type(exc).__name__
                    break
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError as exc:
                    # 产品自己走了。请求**仍然留在账本里**，不当作没发生。
                    record.termination = CLIENT_DISCONNECTED
                    record.error_kind = type(exc).__name__
                    break
                if streaming:
                    pending = self._scan(record, pending + chunk, started)
                elif len(buffered) < _MAX_BUFFERED_BODY:
                    buffered += chunk
            if not streaming and buffered:
                self._absorb(record, bytes(buffered), started)
            if record.termination is None:
                if streaming and not record.sse_done:
                    record.termination = STREAM_TRUNCATED
                elif (record.http_status or 0) >= 400:
                    record.termination = UPSTREAM_ERROR
                else:
                    record.termination = COMPLETED
        proxy.finish(record, started)

    def _scan(self, record: ModelRequestRecord, pending: bytes, started: float) -> bytes:
        """逐行解析 SSE，**只取元数据**。正文读过就扔，不进账本。"""
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            if not line.startswith(b"data:"):
                continue
            value = line[5:].strip()
            if value == b"[DONE]":
                record.sse_done = True
                continue
            try:
                payload = json.loads(value)
            except (json.JSONDecodeError, UnicodeDecodeError):
                record.error_kind = record.error_kind or "sse-parse"
                continue
            self._absorb_payload(record, payload, started)
        return pending

    def _absorb(self, record: ModelRequestRecord, raw: bytes, started: float) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            record.error_kind = record.error_kind or "response-not-json"
            return
        self._absorb_payload(record, payload, started)

    def _absorb_payload(self, record: ModelRequestRecord, payload: object, started: float) -> None:
        if not isinstance(payload, dict):
            return
        if isinstance(payload.get("usage"), dict):
            record.usage = payload["usage"]
        if payload.get("model"):
            record.response_model = payload["model"]
        if _has_output(payload):
            record.output_events += 1
            if record.first_output_seconds is None:
                record.first_output_seconds = round(time.monotonic() - started, 6)
