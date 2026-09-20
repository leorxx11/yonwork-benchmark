from __future__ import annotations

import json
import socket
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from .catalog import ModelChoice
from .discovery import HostEndpoint
from .models import ChatTurn, JsonObject, now_iso
from .transport import TransportError, open_request


CHAT_SEND_PATH = "/api/chat/send"
DEFAULT_TURN_TIMEOUT_SECONDS = 600.0

# 终止判定（gateway-ne0Qzful.js 的 xc()）。判错会把轮次提前截断，
# 是跑批里最容易制造假数据的一处，改动前先回看 CLAUDE.md 三-2。
TERMINAL_PHASES = frozenset({"completed", "finalized", "failed"})
TERMINAL_STATES = frozenset({"completed", "finalized", "final", "error"})
NON_TERMINAL_STOP_REASONS = frozenset({"tooluse"})
NON_TERMINAL_STREAMS = frozenset({"compaction"})


class ChatError(RuntimeError):
    """驱动层能观测到的失败。**不做分类判定**，由 assertions 决定算 Error 还是 Fail。"""

    def __init__(self, message: str, *, partial: ChatTurn | None = None) -> None:
        super().__init__(message)
        self.partial = partial


class ChatTimeout(ChatError):
    """客户端超时。PAD 时代第 3 条问题（无显式超时退化成 Error）就死在这。"""


class SessionKeyReuse(RuntimeError):
    """复用 sessionKey 会让本轮看见上一轮上下文，数据静默作废且不报错。"""


@dataclass(frozen=True, slots=True)
class SseEvent:
    name: str
    data: str

    def json(self) -> JsonObject | None:
        if not self.data.strip():
            return None
        try:
            value = json.loads(self.data)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None


def iter_sse_events(lines: Iterable[bytes]) -> Iterator[SseEvent]:
    """按 SSE 规范切事件：`:` 开头是注释，空行派发，data 可多行。"""

    name = ""
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if name or data:
                yield SseEvent(name=name or "message", data="\n".join(data))
            name, data = "", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)
    if name or data:
        yield SseEvent(name=name or "message", data="\n".join(data))


def is_terminal_message(payload: JsonObject) -> bool:
    """chat.message 是否为终止帧。

    `stream=="compaction"` 和 `stopReason=="tooluse"` **不是**终止。
    """
    if payload.get("stream") in NON_TERMINAL_STREAMS:
        return False
    if payload.get("phase") in TERMINAL_PHASES:
        return True
    if payload.get("state") in TERMINAL_STATES:
        return True
    stop_reason = payload.get("stopReason")
    if isinstance(stop_reason, str) and stop_reason not in NON_TERMINAL_STOP_REASONS:
        return True
    return False


def _message_body(payload: JsonObject) -> JsonObject:
    """chat.message 的 data 外面还包了一层 {"message": {...}}。"""
    inner = payload.get("message")
    return inner if isinstance(inner, dict) else payload


def extract_text(message: JsonObject) -> str:
    """取一条消息里的纯文本内容块。"""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(part for part in parts if isinstance(part, str))


def extract_tool_names(message: JsonObject) -> list[str]:
    """尽力而为地收集工具调用名。

    tool_use 块的确切结构没有逐字段验证过，所以这里宽松匹配，
    取不到名字时记 'unknown' 而不是丢掉这次调用。
    """
    content = message.get("content")
    if not isinstance(content, list):
        return []
    names: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if isinstance(block_type, str) and "tool" in block_type:
            name = block.get("name") or block.get("toolName")
            names.append(name if isinstance(name, str) and name else "unknown")
    return names


def session_key_for(benchmark_id: str, agent_id: str = "main") -> str:
    """每轮一个全新 sessionKey，等价于 PAD 里每轮点「新建任务」。"""
    return f"agent:{agent_id}:{benchmark_id}"


class ChatClient:
    """`POST /api/chat/send` 的 SSE 客户端。只收原材料，不下判断。"""

    def __init__(
        self,
        endpoint: HostEndpoint,
        *,
        agent_id: str = "main",
        timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        transcript_dir: Path | None = None,
        model_choice: ModelChoice | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.agent_id = agent_id
        self.timeout_seconds = timeout_seconds
        self.transcript_dir = transcript_dir
        self.model_choice = model_choice
        self._used_session_keys: set[str] = set()

    def _claim_session_key(self, session_key: str) -> None:
        if session_key in self._used_session_keys:
            raise SessionKeyReuse(
                f"sessionKey 已被用过：{session_key}；复用会让本轮看见上一轮上下文"
            )
        self._used_session_keys.add(session_key)

    def _payload(self, benchmark_id: str, prompt: str, session_key: str) -> JsonObject:
        payload: JsonObject = {
            "sessionKey": session_key,
            "message": prompt,
            "deliver": False,  # 不外发到任何渠道
            # idempotencyKey 事实必填（不传直接 500），且服务端的 runId 直接取它的值。
            "idempotencyKey": benchmark_id,
            "clientMessageId": f"cm-{benchmark_id}",
        }
        if self.model_choice is not None:
            payload["modelSelection"] = self.model_choice.selection
        return payload

    def send(self, *, benchmark_id: str, prompt: str, session_key: str | None = None) -> ChatTurn:
        key = session_key or session_key_for(benchmark_id, self.agent_id)
        self._claim_session_key(key)

        started_at = now_iso()
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        transcript = self._open_transcript(benchmark_id)

        state = _TurnState(benchmark_id=benchmark_id, session_key=key, prompt=prompt)
        state.started_at = started_at
        state.requested_model = self.model_choice.model_id if self.model_choice else None
        state.requested_model_label = (
            (self.model_choice.display_name or self.model_choice.model_id)
            if self.model_choice
            else None
        )

        try:
            response = open_request(
                self.endpoint.url(CHAT_SEND_PATH),
                method="POST",
                payload=self._payload(benchmark_id, prompt, key),
                token=self.endpoint.token,
                timeout=self.timeout_seconds,
                accept="text/event-stream",
            )
        except TransportError as exc:
            state.http_status = exc.status
            raise ChatError(
                f"{benchmark_id}: chat/send 请求失败：{exc}",
                partial=state.build(started, transcript),
            ) from exc

        try:
            with response:
                state.http_status = getattr(response, "status", None)
                for event in iter_sse_events(_timed_lines(response, deadline, benchmark_id)):
                    if transcript is not None:
                        transcript.write(
                            json.dumps(
                                {"event": event.name, "data": event.data},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    if self._consume(state, event, started):
                        break
        except ChatTimeout as exc:
            exc.partial = state.build(started, transcript)
            self._close_transcript(transcript)
            raise
        except (socket.timeout, TimeoutError) as exc:
            self._close_transcript(transcript)
            raise ChatTimeout(
                f"{benchmark_id}: 读取 SSE 超时（{self.timeout_seconds:g}s）",
                partial=state.build(started, transcript),
            ) from exc
        except OSError as exc:
            self._close_transcript(transcript)
            raise ChatError(
                f"{benchmark_id}: SSE 流中断：{exc}",
                partial=state.build(started, transcript),
            ) from exc

        turn = state.build(started, transcript)
        self._close_transcript(transcript)
        return turn

    def _consume(self, state: "_TurnState", event: SseEvent, started: float) -> bool:
        """返回 True 表示本轮已终止。"""
        state.counts[event.name] += 1
        payload = event.json()
        if payload is None:
            return False

        if event.name == "chat.run-id":
            run_id = payload.get("runId")
            if isinstance(run_id, str):
                state.run_id = run_id
            return False

        if event.name == "chat.error":
            state.stream_error = payload
            return True

        if event.name == "chat.complete":
            run_id = payload.get("runId")
            if isinstance(run_id, str):
                state.run_id = run_id
            # 记最先到的那个终止信号：final 帧先到时，本轮其实在那里就结束了。
            state.terminated_by = state.terminated_by or "chat.complete"
            return True

        if event.name != "chat.message":
            return False

        body = _message_body(payload)
        if state.first_delta_seconds is None and body.get("deltaText"):
            state.first_delta_seconds = round(time.monotonic() - started, 3)

        inner = body.get("message")
        if isinstance(inner, dict):
            state.tool_calls.extend(extract_tool_names(inner))

        if not is_terminal_message(body):
            return False

        state.final_state = body.get("state") if isinstance(body.get("state"), str) else None
        stop_reason = body.get("stopReason")
        state.stop_reason = stop_reason if isinstance(stop_reason, str) else None
        if isinstance(inner, dict):
            text = extract_text(inner)
            if text:
                state.answer = text
        state.terminated_by = state.terminated_by or "chat.message:final"
        # 终止帧之后通常还会来 chat.complete；继续读，让服务端正常关流。
        return False

    def _open_transcript(self, benchmark_id: str) -> Any:
        if self.transcript_dir is None:
            return None
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        path = self.transcript_dir / f"{benchmark_id}.sse.jsonl"
        return path.open("w", encoding="utf-8")

    @staticmethod
    def _close_transcript(handle: Any) -> None:
        if handle is not None and not handle.closed:
            handle.close()


def _timed_lines(response: Any, deadline: float, benchmark_id: str) -> Iterator[bytes]:
    """按行读流，并在每行之间检查整轮截止时间。

    urlopen 的 timeout 只管单次 read，长任务可以靠不断吐 delta 一直不超时，
    所以整轮还要自己卡一个 deadline，否则会退化成「永远不返回」。
    """
    for line in response:
        yield line
        if time.monotonic() > deadline:
            raise ChatTimeout(f"{benchmark_id}: 本轮超过整体超时阈值")


@dataclass
class _TurnState:
    benchmark_id: str
    session_key: str
    prompt: str
    started_at: str = ""
    run_id: str | None = None
    answer: str | None = None
    first_delta_seconds: float | None = None
    terminated_by: str | None = None
    stop_reason: str | None = None
    final_state: str | None = None
    stream_error: JsonObject | None = None
    http_status: int | None = None
    requested_model: str | None = None
    requested_model_label: str | None = None
    counts: Counter[str] = field(default_factory=Counter)
    tool_calls: list[str] = field(default_factory=list)

    def build(self, started: float, transcript: Any) -> ChatTurn:
        path = getattr(transcript, "name", None) if transcript is not None else None
        return ChatTurn(
            benchmark_id=self.benchmark_id,
            session_key=self.session_key,
            prompt=self.prompt,
            started_at=self.started_at,
            ended_at=now_iso(),
            duration_seconds=round(time.monotonic() - started, 3),
            run_id=self.run_id,
            answer=self.answer,
            first_delta_seconds=self.first_delta_seconds,
            terminated_by=self.terminated_by,
            stop_reason=self.stop_reason,
            final_state=self.final_state,
            event_counts=dict(self.counts),
            tool_calls=tuple(self.tool_calls),
            requested_model=self.requested_model,
            requested_model_label=self.requested_model_label,
            stream_error=self.stream_error,
            http_status=self.http_status,
            transcript_path=str(path) if path else None,
        )
