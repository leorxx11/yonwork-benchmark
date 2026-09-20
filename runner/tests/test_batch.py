from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runner.batch import BatchOptions, exit_code_for, run_batch
from runner.client import ChatTimeout
from runner.discovery import HostEndpoint
from runner.models import ChatTurn, Expectations, TaskItem, Verdict, now_iso


class _FakeClient:
    """按脚本逐轮返回结果或抛异常。"""

    def __init__(self, script: list[object]) -> None:
        self.agent_id = "main"
        self.script = list(script)
        self.session_keys: list[str] = []

    def send(self, *, benchmark_id: str, prompt: str, session_key: str) -> ChatTurn:
        self.session_keys.append(session_key)
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return ChatTurn(
            benchmark_id=benchmark_id,
            session_key=session_key,
            prompt=prompt,
            started_at=now_iso(),
            ended_at=now_iso(),
            duration_seconds=1.0,
            run_id=benchmark_id,
            answer=str(outcome),
            terminated_by="chat.complete",
            stop_reason="stop",
            http_status=200,
        )


def items(count: int) -> list[TaskItem]:
    return [
        TaskItem(position=index, case_name=f"Case0{index + 1}", run_no=1, prompt="你好",
                 expectations=Expectations())
        for index in range(count)
    ]


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.results = Path(temporary.name) / "results.jsonl"
        self.options = BatchOptions(
            batch_id="test-batch", results_path=self.results, collect_usage=False
        )

    def _run(self, script: list[object]):
        client = _FakeClient(script)
        records = run_batch(
            items(len(script)),
            client=client,  # type: ignore[arg-type]
            endpoint=HostEndpoint(base_url="http://127.0.0.1:1"),
            options=self.options,
        )
        return client, records

    def test_one_bad_round_does_not_abort_the_batch(self) -> None:
        """PAD 时代第 1 条问题：一次超时整批中止。"""
        _, records = self._run(["A", ChatTimeout("超时"), "C"])
        self.assertEqual(3, len(records))
        self.assertEqual(
            [Verdict.PASS, Verdict.TIMEOUT, Verdict.PASS],
            [record.verdict for record in records],
        )

    def test_unexpected_exception_is_classified_as_error(self) -> None:
        _, records = self._run([RuntimeError("驱动自己炸了")])
        self.assertEqual(Verdict.ERROR, records[0].verdict)

    def test_every_round_gets_a_fresh_session_key(self) -> None:
        """复用 sessionKey 会让第 N 轮看见第 N-1 轮的上下文。"""
        client, records = self._run(["A", "B", "C"])
        self.assertEqual(3, len(set(client.session_keys)))
        for record in records:
            self.assertTrue(record.session_key.endswith(record.benchmark_id))

    def test_benchmark_id_is_the_idempotency_key_and_run_id(self) -> None:
        _, records = self._run(["A"])
        self.assertEqual(records[0].benchmark_id, records[0].turn.run_id)

    def test_results_land_on_disk_round_by_round(self) -> None:
        """整批结束才写 = 跑到一半崩了什么都不剩。"""
        self._run(["A", ChatTimeout("超时")])
        lines = self.results.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(2, len(lines))
        self.assertEqual("Timeout", json.loads(lines[1])["verdict"])

    def test_exit_code_reflects_the_worst_verdict(self) -> None:
        _, records = self._run(["A", ChatTimeout("超时")])
        self.assertEqual(2, exit_code_for(records))
        self.assertEqual(4, exit_code_for([]))


if __name__ == "__main__":
    unittest.main()
