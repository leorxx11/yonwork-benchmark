from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from runner.job_store import (
    CANCELLED,
    COMPLETED,
    FAILED,
    WorkerAlreadyRunning,
    exclusive_worker_lock,
)
from runner.worker import execute_job


class _FakeDriver:
    """只提供 Driver 协议要的那几项，不碰任何产品。"""

    product = "yonwork"

    def preflight(self):
        return ["前置检查通过（测试桩）"]

    def session_key(self, benchmark_id: str) -> str:
        return f"agent:main:{benchmark_id}".lower()

    def run_turn(self, *, benchmark_id: str, prompt: str):
        raise AssertionError("run_batch 被打了桩，不该真的跑一轮")

    def collect_usage(self, turn):
        raise AssertionError("run_batch 被打了桩，不该采用量")

    def close(self) -> None:
        return None


class WorkerTests(unittest.TestCase):
    @staticmethod
    def _job() -> dict[str, object]:
        return {
            "job_id": "a" * 32,
            "batch_id": "web-test",
            "experiment_name": "回归测试",
            "case_catalog_path": "cases/catalog.yaml",
            "case_set_id": "smoke",
            "product": "yonwork",
            "model_query": "",
            "agent_id": "main",
            "timeout_seconds": 60,
            "collect_usage": True,
            "export_xlsx": False,
            "limit_runs": 0,
        }

    def _execute(
        self, *, returned_records: int, shutdown: bool, cancelled: bool = False
    ) -> MagicMock:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        finish = MagicMock()

        def fake_run_batch(items, **kwargs):
            # 真的去问一次 should_stop，这样「点了停止就不再开下一轮」
            # 这个协作式停止的约定是被测到的，不是靠约定。
            records: list[object] = []
            for _ in range(returned_records):
                if kwargs["should_stop"]():
                    break
                record = object()
                records.append(record)
                kwargs["on_record"](record)
            return records

        summary = MagicMock()
        summary.as_text.return_value = "2 轮"
        patches = (
            patch(
                "runner.worker.load_case_set",
                return_value=SimpleNamespace(name="smoke", cases=(object(), object())),
            ),
            patch("runner.worker.expand_cases", return_value=[object(), object()]),
            patch("runner.worker.set_total_runs"),
            patch("runner.worker.set_completed_runs"),
            # Worker 现在只认 Driver 接口，前置检查、模型解析、发请求
            # 全在驱动里；这里换掉整个驱动就够，不用再逐个补 YonWork 的桩。
            patch("runner.worker.build_driver", return_value=_FakeDriver()),
            patch("runner.worker.run_batch", side_effect=fake_run_batch),
            patch("runner.worker.build_database"),
            patch("runner.worker.ingest_file"),
            patch("runner.worker.suite_id_for", return_value="suite-1"),
            patch("runner.worker.summarize", return_value=summary),
            patch(
                "runner.worker.collect_session_usage",
                return_value={"matched": returned_records, "runs": returned_records},
            ),
            patch(
                "runner.worker.reconcile_suite",
                return_value={"matched": returned_records, "runs": returned_records},
            ),
            patch("runner.worker.is_cancel_requested", return_value=cancelled),
            patch("runner.worker.finish_job", finish),
            patch("runner.worker.append_event"),
        )
        for context in patches:
            context.start()
            self.addCleanup(context.stop)

        execute_job(
            self._job(),
            results_root=Path(temporary.name),
            shutdown_requested=lambda: shutdown,
        )
        return finish

    def test_completed_job_is_marked_completed(self) -> None:
        finish = self._execute(returned_records=2, shutdown=False)
        self.assertEqual(COMPLETED, finish.call_args.kwargs["status"])
        self.assertEqual("", finish.call_args.kwargs["error"])

    def test_shutdown_after_current_round_marks_partial_job_failed(self) -> None:
        finish = self._execute(returned_records=1, shutdown=True)
        self.assertEqual(FAILED, finish.call_args.kwargs["status"])
        self.assertIn("停止信号", finish.call_args.kwargs["error"])

    def test_cancel_button_stops_before_next_round_and_marks_cancelled(self) -> None:
        # 页面上点「停止任务」：一轮都不该再开，终态是 Cancelled 而不是 Failed
        # ——用户主动停的不是产品的问题，混进 Failed 会把统计弄脏。
        finish = self._execute(returned_records=2, shutdown=False, cancelled=True)
        self.assertEqual(CANCELLED, finish.call_args.kwargs["status"])
        self.assertEqual("", finish.call_args.kwargs["error"])
        self.assertIsNone(finish.call_args.kwargs["suite_id"])


class WorkerExclusivityTests(unittest.TestCase):
    """第二个 Worker 必须被拦住。

    并发跑批会让 NewAPI 后台用量按时间窗张冠李戴（CLAUDE.md 七-5），
    而且 fail_running_jobs() 会把别人正在跑的任务收拢成 Failed。
    """

    def test_second_worker_refuses_to_start(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"acquired": 0}  # GET_LOCK 没拿到

        with patch("runner.job_store.open_connection", return_value=connection):
            with self.assertRaises(WorkerAlreadyRunning):
                with exclusive_worker_lock():
                    self.fail("拿不到锁却进了临界区")
        connection.close.assert_called_once()

    def test_lock_is_released_even_when_body_raises(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"acquired": 1}

        with patch("runner.job_store.open_connection", return_value=connection):
            with self.assertRaises(ZeroDivisionError):
                with exclusive_worker_lock():
                    raise ZeroDivisionError
        released = [
            call for call in cursor.execute.call_args_list if "RELEASE_LOCK" in call.args[0]
        ]
        self.assertEqual(1, len(released))
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
