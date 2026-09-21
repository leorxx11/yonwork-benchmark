from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..catalog import CatalogError, list_model_choices, resolve_model_choice
from ..client import ChatClient, session_key_for
from ..discovery import (
    DiscoveryError,
    HostEndpoint,
    discover,
    has_session,
    health_check,
    session_status,
)
from ..models import ChatTurn, UsageSample
from ..newapi import NewApiConfig, NewApiError, collect_turn_logs
from ..sessionlog import SessionLogError, collect_one
from ..transport import TransportError
from ..usage import DEFAULT_SETTLE_SECONDS, fetch_recent_token_history, match_usage
from .base import DriverError, UsageCollection


class YonWorkDriver:
    """YonWork 桌面端：常驻 Host API + SSE。

    这是主链路，行为与迁移到 Driver 接口之前逐字一致——
    搬过来只是换了个位置，没有改判定口径。
    """

    product = "yonwork"

    def __init__(
        self,
        *,
        agent_id: str = "main",
        model_query: str = "",
        timeout_seconds: float = 600.0,
        transcript_dir: Path | None = None,
        usage_settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    ) -> None:
        self.agent_id = agent_id
        self.model_query = model_query.strip()
        self.timeout_seconds = timeout_seconds
        self.transcript_dir = transcript_dir
        self.usage_settle_seconds = usage_settle_seconds
        self.endpoint: HostEndpoint | None = None
        self._client: ChatClient | None = None
        self._uses_newapi = False

    def preflight(self) -> Iterator[str]:
        try:
            endpoint = discover()
            health = health_check(endpoint)
        except (DiscoveryError, TransportError) as exc:
            raise DriverError(
                f"YonWork 前置检查失败：{exc}\n"
                "检查顺序：应用在跑吗 → 脚本绕开 http_proxy 了吗 → 运行时文件在哪"
            ) from exc
        yield f"Host API：{endpoint.base_url}（来源 {endpoint.source}，健康 {health}）"

        try:
            logged_in = has_session(session_status(endpoint))
        except TransportError as exc:
            raise DriverError(f"登录态检查失败：{exc}") from exc
        if not logged_in:
            # 未登录时整批都会是 Fail，那批数据毫无意义，不如一开始就不跑。
            raise DriverError("YonWork 尚未登录（hasSession=false）")

        model_choice = None
        if self.model_query:
            try:
                model_choice = resolve_model_choice(
                    list_model_choices(endpoint), self.model_query
                )
            except (CatalogError, TransportError) as exc:
                raise DriverError(f"模型解析失败：{exc}") from exc
            yield f"指定模型：{model_choice.label}"
        else:
            yield "未指定模型，用智能体当前的默认模型"

        self.endpoint = endpoint
        # 本项目通过名为 newapi 的模型配置访问网关；指定其它模型不代表经过它。
        self._uses_newapi = bool(
            model_choice and model_choice.display_name.casefold() == "newapi"
        )
        self._client = ChatClient(
            endpoint,
            agent_id=self.agent_id,
            timeout_seconds=self.timeout_seconds,
            transcript_dir=self.transcript_dir,
            model_choice=model_choice,
        )

    def session_key(self, benchmark_id: str) -> str:
        return session_key_for(benchmark_id, self.agent_id)

    def run_turn(self, *, benchmark_id: str, prompt: str) -> ChatTurn:
        client = self._require_client()
        return client.send(
            benchmark_id=benchmark_id,
            prompt=prompt,
            session_key=self.session_key(benchmark_id),
        )

    def collect_usage(self, turn: ChatTurn) -> UsageCollection:
        """端上 HTTP、会话 JSONL、NewAPI 分别采，一路失败不影响另一路。

        端上端点实测在漏记（CLAUDE.md 六-3），默认模型那几轮全靠会话 JSONL
        才有数——所以这两路是互为兜底的两端，不是重复。
        """
        samples: list[UsageSample] = []
        notes: list[str] = []
        for label, collect in (
            ("端上用量", lambda: self._device_usage(turn)),
            ("会话日志", lambda: self._session_usage(turn)),
            ("NewAPI 日志", lambda: self._backend_usage(turn)),
        ):
            try:
                sample = collect()
            except (TransportError, SessionLogError, NewApiError, ValueError, TypeError) as exc:
                # 采不到不改变本轮的产品判定，只记一笔：
                # 「没采到」和「产品出错」是两回事，混了统计就脏了。
                # 后台错误正文可能含上游地址/凭据，不写进报告。
                detail = type(exc).__name__ if label == "NewAPI 日志" else str(exc)
                notes.append(f"{label}采集失败：{detail}")
                continue
            if sample is not None:
                samples.append(sample)
            elif label == "NewAPI 日志" and self._uses_newapi:
                notes.append("NewAPI 未匹配到本轮日志，调用统计仍为未采集")
        return UsageCollection(samples=tuple(samples), notes=tuple(notes))

    def _backend_usage(self, turn: ChatTurn) -> UsageSample | None:
        if not self._uses_newapi:
            return None
        config = NewApiConfig.load()
        return collect_turn_logs(config, turn, token_name=config.token_name)

    def close(self) -> None:
        self._client = None

    def _require_client(self) -> ChatClient:
        if self._client is None:
            raise DriverError("YonWorkDriver 还没做前置检查，先调用 preflight()")
        return self._client

    def _device_usage(self, turn: ChatTurn) -> UsageSample | None:
        if self.endpoint is None:
            return None
        if self.usage_settle_seconds > 0:
            time.sleep(self.usage_settle_seconds)
        return match_usage(fetch_recent_token_history(self.endpoint), turn)

    def _session_usage(self, turn: ChatTurn) -> UsageSample | None:
        """会话 JSONL：本地文件，按 idempotencyKey 精确匹配，不受时间窗影响。"""
        found = collect_one(
            turn.benchmark_id,
            agent_id=self.agent_id,
            started_at=_parse_iso(turn.started_at),
        )
        return found.as_sample() if found else None


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
