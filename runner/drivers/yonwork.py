from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..catalog import (
    CatalogError,
    default_choice,
    list_model_choices,
    resolve_model_choice,
)
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

        # ⚠️ **不指定模型时也必须显式发 modelSelection**，不能靠省略。
        # YonWork 1.0.10 实测：空 selection 会让模型应用函数返回
        # `runtimeApplied: false`，请求里也不带解析结果，于是引擎**沿用它上一次的配置**。
        # 表现就是「默认模型」那一列实际跑的是上一次用过的 provider——
        # 2026-09-22 整整一列 12 轮白跑，而且判定全 Pass，从结果上完全看不出来。
        # 所以这里把「默认」解析成官方标记的那个 choice，显式发出去。
        try:
            choices = list_model_choices(endpoint)
            if self.model_query:
                model_choice = resolve_model_choice(choices, self.model_query)
                yield f"指定模型：{model_choice.label}"
            else:
                model_choice = default_choice(choices)
                if model_choice is None:
                    raise DriverError(
                        "没有任何模型被标记为默认，无法确定「默认模型」是哪一个；"
                        "用 --model 显式指定"
                    )
                yield f"默认模型（显式发送，不靠省略）：{model_choice.label}"
        except (CatalogError, TransportError) as exc:
            raise DriverError(f"模型解析失败：{exc}") from exc

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

    def enrich(self, turn: ChatTurn) -> ChatTurn:
        """从会话 JSONL 补上 SSE 看不到的工具调用。

        **2026-09-21 实测**：同一轮，`/api/chat/send` 的 SSE 里 content 块
        只有 `text`（22 条 chat.message 全是文本增量），而会话 JSONL 里
        明明白白有 `{"type":"toolCall"}` + `toolResult`。
        所以 `tool_calls` 对 YonWork **恒为 0**，不是产品没调工具，
        是我们这条通路看不见——拿它去判「工具用例」等于把观测盲区算成产品失败。

        SSE 的正向调用证据保留，但不能据此认定完整计数。
        补采缺失和确认零次必须分开，最终由断言层决定如何判定。
        """
        if turn.tool_calls_status == "observed":
            return turn
        try:
            found = collect_one(
                turn.benchmark_id,
                agent_id=self.agent_id,
                started_at=_parse_iso(turn.started_at),
            )
        except (SessionLogError, OSError, ValueError, TypeError) as exc:
            return replace(turn, tool_calls_status="error", tool_calls_source="session-jsonl",
                           tool_calls_detail=f"会话日志补采失败：{type(exc).__name__}")
        if found is None:
            return replace(turn, tool_calls_status="unavailable", tool_calls_source="session-jsonl",
                           tool_calls_detail="没有找到本轮会话日志，不能确认工具调用次数")
        calls = found.tool_calls if len(found.tool_calls) >= len(turn.tool_calls) else turn.tool_calls
        if found.tool_calls_error or not found.tool_calls_complete:
            return replace(turn, tool_calls=calls, tool_calls_source="session-jsonl",
                           tool_calls_status="error" if found.tool_calls_error else "unavailable",
                           tool_calls_detail="会话日志损坏" if found.tool_calls_error else "会话日志尚未包含终止消息")
        # 不把两个来源相加，以免重复计数；不同来源冲突时保留观测异常。
        if len(found.tool_calls) < len(turn.tool_calls):
            return replace(turn, tool_calls_status="unavailable", tool_calls_source="session-jsonl+sse",
                           tool_calls_detail="会话日志调用数少于 SSE，采集完整性未确认")
        return replace(turn, tool_calls=found.tool_calls, tool_calls_status="observed",
                       tool_calls_source="session-jsonl", tool_calls_detail="")

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
