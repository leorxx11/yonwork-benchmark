from __future__ import annotations

import json
import unittest

from runner.client import (
    ChatClient,
    SessionKeyReuse,
    extract_text,
    extract_tool_names,
    is_terminal_message,
    iter_sse_events,
    session_key_for,
)
from runner.discovery import HostEndpoint


def _lines(text: str) -> list[bytes]:
    return [f"{line}\n".encode("utf-8") for line in text.split("\n")]


class SseParsingTests(unittest.TestCase):
    def test_parses_named_events_and_skips_comments(self) -> None:
        raw = (
            ": connected\n"
            ": yonclaw-sandbox-chat-stream-marker=x\n"
            "\n"
            'event: chat.run-id\n'
            'data: {"runId":"bench-1"}\n'
            "\n"
            "event: chat.complete\n"
            'data: {"runId":"bench-1"}\n'
            "\n"
        )
        events = list(iter_sse_events(_lines(raw)))
        self.assertEqual(["chat.run-id", "chat.complete"], [item.name for item in events])
        self.assertEqual("bench-1", events[0].json()["runId"])

    def test_joins_multi_line_data(self) -> None:
        raw = 'event: chat.message\ndata: {"a":\ndata: 1}\n\n'
        event = next(iter_sse_events(_lines(raw)))
        self.assertEqual({"a": 1}, event.json())

    def test_tolerates_unparsable_data(self) -> None:
        event = next(iter_sse_events(_lines("event: chat.message\ndata: not json\n\n")))
        self.assertIsNone(event.json())


class TerminationTests(unittest.TestCase):
    def test_final_state_is_terminal(self) -> None:
        self.assertTrue(is_terminal_message({"state": "final", "stopReason": "stop"}))

    def test_delta_is_not_terminal(self) -> None:
        self.assertFalse(is_terminal_message({"state": "delta", "deltaText": "P"}))

    def test_compaction_stream_is_not_terminal(self) -> None:
        """跑批时最容易误判的坑之一。"""
        self.assertFalse(
            is_terminal_message({"stream": "compaction", "state": "final", "stopReason": "stop"})
        )

    def test_tooluse_stop_reason_is_not_terminal(self) -> None:
        """另一个坑：工具调用中途的 stopReason 不代表这一轮结束。"""
        self.assertFalse(is_terminal_message({"state": "delta", "stopReason": "tooluse"}))

    def test_failed_phase_is_terminal(self) -> None:
        self.assertTrue(is_terminal_message({"phase": "failed"}))


class ExtractionTests(unittest.TestCase):
    def test_extracts_text_blocks_only(self) -> None:
        message = {
            "content": [
                {"type": "text", "text": "PO"},
                {"type": "tool_use", "name": "read_file"},
                {"type": "text", "text": "NG"},
            ]
        }
        self.assertEqual("PONG", extract_text(message))
        self.assertEqual(["read_file"], extract_tool_names(message))

    def test_unnamed_tool_call_is_kept(self) -> None:
        message = {"content": [{"type": "tool_result"}]}
        self.assertEqual(["unknown"], extract_tool_names(message))


class SessionKeyTests(unittest.TestCase):
    def test_session_key_shape(self) -> None:
        self.assertEqual("agent:main:bench-1", session_key_for("bench-1"))

    def test_session_key_is_lowercased(self) -> None:
        """带大写的 sessionKey 会让 YonWork 把一轮拆成两条会话：

        有标题的那条没有 sessionId 和对话内容，有内容的那条没有标题。
        界面上看就是「用户消息和 Agent 回答不在一个界面」。实测过。
        """
        self.assertEqual(
            "agent:main:bench-case01-r1", session_key_for("bench-Case01-r1")
        )

    def test_client_refuses_to_reuse_a_session_key(self) -> None:
        """复用 sessionKey 会让本轮看见上一轮上下文，必须硬拦。"""
        client = ChatClient(HostEndpoint(base_url="http://127.0.0.1:1"))
        client._claim_session_key("agent:main:bench-1")
        with self.assertRaises(SessionKeyReuse):
            client._claim_session_key("agent:main:bench-1")

    def test_reuse_check_ignores_case(self) -> None:
        """服务端把只差大小写的两个 key 当成同一会话，这里也必须当成同一个。"""
        client = ChatClient(HostEndpoint(base_url="http://127.0.0.1:1"))
        client._claim_session_key("agent:main:bench-Case01")
        with self.assertRaises(SessionKeyReuse):
            client._claim_session_key("agent:main:bench-case01")


class PayloadTests(unittest.TestCase):
    def test_idempotency_key_is_the_benchmark_id(self) -> None:
        client = ChatClient(HostEndpoint(base_url="http://127.0.0.1:1"))
        payload = client._payload("bench-Case01-r1-abc", "hi", "agent:main:bench-Case01-r1-abc")
        self.assertEqual("bench-Case01-r1-abc", payload["idempotencyKey"])
        self.assertFalse(payload["deliver"])
        # 不传 idempotencyKey 服务端直接 500，序列化后必须确实带着。
        self.assertIn("idempotencyKey", json.loads(json.dumps(payload)))


class _FakeResponse:
    """够用的假 SSE 响应：可迭代、可当上下文管理器、带 status。"""

    def __init__(self, raw: str, status: int = 200) -> None:
        self._lines = _lines(raw)
        self.status = status

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class SendTurnTests(unittest.TestCase):
    def _send(self, raw: str) -> object:
        import runner.client as client_module

        original = client_module.open_request
        client_module.open_request = lambda *args, **kwargs: _FakeResponse(raw)
        try:
            client = ChatClient(HostEndpoint(base_url="http://127.0.0.1:1"))
            return client.send(benchmark_id="bench-1", prompt="Reply with exactly: PONG")
        finally:
            client_module.open_request = original

    def test_collects_answer_and_run_id(self) -> None:
        raw = (
            ": connected\n\n"
            "event: chat.run-id\n"
            'data: {"runId":"bench-1","result":{"status":"started"}}\n\n'
            "event: chat.message\n"
            'data: {"message":{"seq":2,"state":"delta","deltaText":"PO"}}\n\n'
            "event: chat.message\n"
            'data: {"message":{"seq":6,"state":"final","stopReason":"stop",'
            '"message":{"role":"assistant","content":[{"type":"text","text":"PONG"}]}}}\n\n'
            "event: chat.complete\n"
            'data: {"runId":"bench-1"}\n\n'
        )
        turn = self._send(raw)
        self.assertEqual("bench-1", turn.run_id)
        self.assertEqual("PONG", turn.answer)
        self.assertEqual("chat.message:final", turn.terminated_by)
        self.assertEqual("stop", turn.stop_reason)
        self.assertIsNotNone(turn.first_delta_seconds)
        self.assertEqual(2, turn.event_counts["chat.message"])

    def test_tool_use_round_does_not_truncate_the_turn(self) -> None:
        """中途的 tooluse 不是终止，答案必须取最后那条 final。"""
        raw = (
            "event: chat.run-id\n"
            'data: {"runId":"bench-1"}\n\n'
            "event: chat.message\n"
            'data: {"message":{"state":"delta","stopReason":"tooluse",'
            '"message":{"content":[{"type":"tool_use","name":"read_file"}]}}}\n\n'
            "event: chat.message\n"
            'data: {"message":{"stream":"compaction","state":"final"}}\n\n'
            "event: chat.message\n"
            'data: {"message":{"state":"final","stopReason":"stop",'
            '"message":{"content":[{"type":"text","text":"最终答案"}]}}}\n\n'
            "event: chat.complete\n"
            'data: {"runId":"bench-1"}\n\n'
        )
        turn = self._send(raw)
        self.assertEqual("最终答案", turn.answer)
        self.assertEqual(("read_file",), turn.tool_calls)

    def test_chat_error_is_captured_not_raised(self) -> None:
        """产品报错是原材料，不是驱动层异常；分类交给断言层。"""
        raw = "event: chat.error\ndata: {\"message\":\"boom\"}\n\n"
        turn = self._send(raw)
        self.assertEqual({"message": "boom"}, turn.stream_error)
        self.assertIsNone(turn.terminated_by)


if __name__ == "__main__":
    unittest.main()
