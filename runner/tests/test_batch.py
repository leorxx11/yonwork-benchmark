from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runner.batch import BatchOptions, exit_code_for, run_batch
from runner.client import ChatTimeout, session_key_for
from runner.drivers import UsageCollection
from runner.models import ChatTurn, Expectations, TaskItem, Verdict, now_iso


class _FakeDriver:
    """按脚本逐轮返回结果或抛异常。

    这是个 Driver，不是 ChatClient——`run_batch` 现在对被测产品一无所知。
    """

    product = "yonwork"

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.session_keys: list[str] = []

    def preflight(self):
        return ()

    def session_key(self, benchmark_id: str) -> str:
        return session_key_for(benchmark_id)

    def run_turn(self, *, benchmark_id: str, prompt: str) -> ChatTurn:
        session_key = self.session_key(benchmark_id)
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

    def collect_usage(self, turn: ChatTurn) -> UsageCollection:
        return UsageCollection()

    def close(self) -> None:
        return None


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
        driver = _FakeDriver(script)
        records = run_batch(
            items(len(script)), driver=driver, options=self.options
        )
        return driver, records

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
        driver, records = self._run(["A", "B", "C"])
        self.assertEqual(3, len(set(driver.session_keys)))
        for record in records:
            # sessionKey 是小写的（见 session_key_for），BenchmarkId 保留原样大小写，
            # 所以这里按小写比。两者必须仍然一一对应。
            self.assertTrue(record.session_key.endswith(record.benchmark_id.lower()))

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

    def test_stop_check_prevents_starting_the_next_round(self) -> None:
        driver = _FakeDriver(["A", "B", "C"])
        completed = []
        records = run_batch(
            items(3),
            driver=driver,
            options=self.options,
            on_record=completed.append,
            should_stop=lambda: len(completed) >= 1,
        )
        self.assertEqual(1, len(records))
        self.assertEqual(1, len(driver.session_keys))

    def test_product_comes_from_the_driver(self) -> None:
        """产品维度只有驱动说了算，BatchOptions 不再重复声明一份。"""
        driver, records = self._run(["A"])
        self.assertEqual(driver.product, records[0].product)


if __name__ == "__main__":
    unittest.main()
