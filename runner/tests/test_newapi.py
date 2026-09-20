from __future__ import annotations

import unittest
from datetime import datetime, timezone

from runner.newapi import TYPE_CONSUME, TYPE_ERROR, match_logs


def ts(hour: int, minute: int, second: int) -> int:
    return int(
        datetime(2026, 9, 20, hour, minute, second, tzinfo=timezone.utc).timestamp()
    )


def run(benchmark_id: str, start: tuple[int, int, int], end: tuple[int, int, int]) -> dict:
    return {
        "benchmark_id": benchmark_id,
        "started_at": datetime(2026, 9, 20, *start),
        "ended_at": datetime(2026, 9, 20, *end),
        "model_mode": "newapi",
    }


def log(created: int, *, kind: int = TYPE_CONSUME, prompt: int = 100, completion: int = 10) -> dict:
    return {
        "type": kind,
        "created_at": created,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "model_name": "deepseek-flash",
        "use_time": 2,
    }


class MatchTests(unittest.TestCase):
    def test_log_inside_the_window_belongs_to_that_run(self) -> None:
        samples, unmatched = match_logs(
            [log(ts(13, 44, 57))], [run("bench-1", (13, 44, 55), (13, 44, 58))]
        )
        self.assertEqual(1, len(samples))
        self.assertEqual("bench-1", samples[0].benchmark_id)
        self.assertEqual(110, samples[0].total_tokens)
        self.assertEqual([], unmatched)

    def test_unmatched_logs_are_returned_not_dropped(self) -> None:
        """对不上的日志可能是跑批期间有人手动点了对话，是污染证据，不能丢。"""
        samples, unmatched = match_logs(
            [log(ts(10, 0, 0))], [run("bench-1", (13, 44, 55), (13, 44, 58))]
        )
        self.assertEqual([], samples)
        self.assertEqual(1, len(unmatched))

    def test_overlapping_windows_go_to_the_nearest_end(self) -> None:
        runs = [
            run("bench-early", (13, 44, 50), (13, 44, 56)),
            run("bench-late", (13, 44, 55), (13, 45, 20)),
        ]
        samples, _ = match_logs([log(ts(13, 45, 18))], runs)
        self.assertEqual("bench-late", samples[0].benchmark_id)

    def test_error_logs_count_as_calls_and_errors(self) -> None:
        """口径和旧的 newapi_stats.ps1 一致：消费 + 错误都算调用。"""
        samples, _ = match_logs(
            [
                log(ts(13, 44, 56)),
                log(ts(13, 44, 57), kind=TYPE_ERROR, prompt=0, completion=0),
            ],
            [run("bench-1", (13, 44, 55), (13, 44, 58))],
        )
        self.assertEqual(2, samples[0].api_calls)
        self.assertEqual(1, samples[0].error_calls)
        # 错误日志不进 token 统计
        self.assertEqual(100, samples[0].input_tokens)

    def test_irrelevant_log_types_are_ignored(self) -> None:
        samples, unmatched = match_logs(
            [log(ts(13, 44, 56), kind=1)], [run("bench-1", (13, 44, 55), (13, 44, 58))]
        )
        self.assertEqual([], samples)
        self.assertEqual([], unmatched)

    def test_multiple_calls_in_one_turn_are_summed(self) -> None:
        samples, _ = match_logs(
            [log(ts(13, 44, 56)), log(ts(13, 44, 57), prompt=50, completion=5)],
            [run("bench-1", (13, 44, 55), (13, 44, 58))],
        )
        self.assertEqual(150, samples[0].input_tokens)
        self.assertEqual(165, samples[0].total_tokens)
        self.assertEqual(2, samples[0].api_calls)

    def test_runs_without_a_window_are_skipped(self) -> None:
        broken = {"benchmark_id": "bench-x", "started_at": None, "ended_at": None}
        samples, unmatched = match_logs([log(ts(13, 44, 56))], [broken])
        self.assertEqual([], samples)
        self.assertEqual(1, len(unmatched))


if __name__ == "__main__":
    unittest.main()
