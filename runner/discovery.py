from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import JsonObject
from .transport import TransportError, request_json


RUNTIME_FILENAME = "host-api-runtime.json"

# 主进程写 %APPDATA%\yonwork\（注意不是 yonclaw\，那是 yonworkctl 读的路径，见缺陷 2）。
APPDATA_SUBDIR = "AppData/Roaming/yonwork"
WINDOWS_USERS_ROOT = Path("/mnt/c/Users")

ENV_RUNTIME_FILE = "YONWORK_RUNTIME_FILE"
ENV_BASE_URL = "YONCLAW_HOST_API_URL"
ENV_TOKEN = "YONCLAW_HOST_API_TOKEN"


class DiscoveryError(RuntimeError):
    """找不到或读不懂运行时文件。属于工具自己的问题。"""


@dataclass(frozen=True, slots=True)
class HostEndpoint:
    base_url: str
    token: str = field(default="", repr=False)  # 绝对不要进日志和 JSONL
    pid: int | None = None
    version: str | None = None
    source: str = ""

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


def candidate_runtime_files() -> list[Path]:
    override = os.environ.get(ENV_RUNTIME_FILE)
    if override:
        return [Path(override).expanduser()]
    if not WINDOWS_USERS_ROOT.is_dir():
        return []
    return sorted(
        path
        for path in WINDOWS_USERS_ROOT.glob(f"*/{APPDATA_SUBDIR}/{RUNTIME_FILENAME}")
        if path.is_file()
    )


def _require(value: Any, predicate: bool, message: str, path: Path) -> None:
    if not predicate:
        raise DiscoveryError(f"{path}：{message}（实际 {value!r}）")


def load_runtime_file(path: Path) -> JsonObject:
    if not path.is_file():
        raise DiscoveryError(f"找不到运行时文件：{path}（YonWork 没在跑？）")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiscoveryError(f"运行时文件无法解析：{path}") from exc

    _require(raw, isinstance(raw, dict), "内容不是 JSON 对象", path)
    port = raw.get("port")
    _require(
        port,
        isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535,
        "port 缺失或非法",
        path,
    )
    token = raw.get("token")
    _require(token, isinstance(token, str) and bool(token), "token 缺失", path)
    return raw


def discover(runtime_file: Path | None = None) -> HostEndpoint:
    """从 host-api-runtime.json 读 port/token。端口**绝不硬编码**（3211 被占用会回落随机端口）。"""

    base_url = os.environ.get(ENV_BASE_URL)
    if base_url:
        return HostEndpoint(
            base_url=base_url.rstrip("/"),
            token=os.environ.get(ENV_TOKEN, ""),
            source=f"env:{ENV_BASE_URL}",
        )

    candidates = [runtime_file] if runtime_file else candidate_runtime_files()
    if not candidates:
        raise DiscoveryError(
            f"没找到任何 {RUNTIME_FILENAME}；用 {ENV_RUNTIME_FILE} 指定路径，"
            f"或用 {ENV_BASE_URL} 直接给 URL。"
        )
    if len(candidates) > 1:
        listed = "、".join(str(item) for item in candidates)
        raise DiscoveryError(
            f"找到多个运行时文件，无法判断用哪个：{listed}；请用 {ENV_RUNTIME_FILE} 指定。"
        )

    path = candidates[0]
    raw = load_runtime_file(path)
    return HostEndpoint(
        base_url=f"http://127.0.0.1:{raw['port']}",  # mirrored 模式下 WSL 直连有效
        token=raw["token"],
        pid=raw.get("pid") if isinstance(raw.get("pid"), int) else None,
        version=raw.get("version") if isinstance(raw.get("version"), str) else None,
        source=str(path),
    )


def health_check(endpoint: HostEndpoint, timeout: float = 5.0) -> JsonObject:
    """确认 Host API 真的在跑（失败多半是应用退了，其次才是代理劫持）。"""

    try:
        payload = request_json(endpoint.url("/healthz"), token=endpoint.token, timeout=timeout)
    except TransportError as exc:
        raise DiscoveryError(
            f"Host API 健康检查失败（{endpoint.base_url}，来源 {endpoint.source}）：{exc}"
        ) from exc
    return payload if isinstance(payload, dict) else {"raw": payload}


def session_status(endpoint: HostEndpoint, timeout: float = 5.0) -> JsonObject:
    """登录态。没登录时 chat/send 会失败，属于前置条件而不是产品缺陷。"""

    payload = request_json(
        endpoint.url("/api/auth-runtime/session/status"),
        token=endpoint.token,
        timeout=timeout,
    )
    return payload if isinstance(payload, dict) else {"raw": payload}


def has_session(status: JsonObject) -> bool:
    value = status.get("hasSession")
    if isinstance(value, bool):
        return value
    data = status.get("data")
    if isinstance(data, dict) and isinstance(data.get("hasSession"), bool):
        return data["hasSession"]
    return False
