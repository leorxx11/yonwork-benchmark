from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from .models import JsonObject


DEFAULT_TIMEOUT_SECONDS = 15.0


class TransportError(RuntimeError):
    """HTTP 层的失败。属于工具自己的问题，由上层归为 Error。"""

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def build_opener() -> urllib.request.OpenerDirector:
    """绕开 shell 里的 http_proxy。

    环境坑 1：`http_proxy=http://127.0.0.1:7897` 会吞掉本地请求并返回空，
    看起来像「服务没起来」，实际是假阴性。空 ProxyHandler 等价于 curl --noproxy '*'。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _headers(token: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        # 当前构建 AUTH_MODE=trusted 免鉴权，照样带上，将来切 token 模式脚本不用改。
        headers["Authorization"] = f"Bearer {token}"
    if extra:
        headers.update(extra)
    return headers


def open_request(
    url: str,
    *,
    method: str = "GET",
    payload: JsonObject | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    accept: str = "application/json",
) -> Any:
    """发一个请求并返回**未读取**的响应对象，供 SSE 流式消费。"""

    body = None
    extra = {"Accept": accept}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        extra["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url, data=body, method=method, headers=_headers(token, extra)
    )
    try:
        return build_opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise TransportError(
            f"{method} {url} 返回 HTTP {exc.code}", status=exc.code, body=detail
        ) from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise TransportError(f"{method} {url} 请求失败：{exc}") from exc


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: JsonObject | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    response = open_request(
        url, method=method, payload=payload, token=token, timeout=timeout
    )
    with response:
        raw = response.read().decode("utf-8", "replace")
    if not raw.strip():
        raise TransportError(f"{method} {url} 返回空响应（检查是否被代理劫持）")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TransportError(f"{method} {url} 返回的不是 JSON：{raw[:200]}") from exc
