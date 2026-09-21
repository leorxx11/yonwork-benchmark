from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import json

from runner.models import ChatTurn
from runner.newapi import TYPE_CONSUME, TYPE_ERROR, NewApiConfig, NewApiError, collect_turn_logs, fetch_logs, match_logs


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


class TurnCollectionTests(unittest.TestCase):
    def setUp(self):
        self.turn = ChatTurn(
            benchmark_id="b1", session_key="b1", prompt="hi",
            started_at="2026-09-20T21:44:55.328+08:00",
            ended_at="2026-09-20T21:44:58.414+08:00", duration_seconds=3.086,
            requested_model="deepseek-flash",
        )
        self.success = {**log(ts(13, 44, 57)), "id": 1}
        self.error = {**log(ts(13, 44, 56), kind=TYPE_ERROR), "id": 2}

    def collect(self, responses):
        with patch("runner.newapi.fetch_logs", side_effect=responses) as fetch:
            sample = collect_turn_logs(NewApiConfig(), self.turn, settle_seconds=0)
        self.assertEqual(3, fetch.call_count)
        self.assertEqual(0, fetch.call_args.kwargs["slack_seconds"])
        return sample

    def test_late_error_is_collected_even_when_api_renumbers_ids(self):
        # 同一消费日志从 id=1 变为 id=2；id=1 现在指另一条错误日志。
        final = [{**self.error, "id": 1}, {**self.success, "id": 2}]
        sample = self.collect([[self.success], final, final])
        self.assertEqual((2, 1), (sample.api_calls, sample.error_calls))
        self.assertEqual(110, sample.total_tokens)
        self.assertEqual(2, len(sample.log_entries))

    def test_identical_calls_are_not_collapsed(self):
        rows = [self.success, {**self.success, "id": 2}]
        sample = self.collect([rows] * 3)
        self.assertEqual((2, 0), (sample.api_calls, sample.error_calls))

    def test_empty_logs_remain_unavailable(self):
        self.assertIsNone(self.collect([[], [], []]))

    def test_delayed_consumption_is_retried(self):
        sample = self.collect([[], [], [self.success]])
        self.assertEqual((1, 0), (sample.api_calls, sample.error_calls))

    def test_previous_round_and_other_model_logs_cannot_contaminate_this_round(self):
        rows = [
            {**self.error, "created_at": ts(13, 44, 54)},
            {**self.error, "id": 3, "model_name": "unrelated"},
            {**self.error, "id": 4, "created_at": ts(13, 44, 59)},
            self.success,
        ]
        sample = self.collect([rows] * 3)
        self.assertEqual((1, 0), (sample.api_calls, sample.error_calls))

    def test_only_errors_still_count_as_calls(self):
        sample = self.collect([[self.error]] * 3)
        self.assertEqual((1, 1), (sample.api_calls, sample.error_calls))
        self.assertEqual(0, sample.total_tokens)

    def test_invalid_window_is_not_silently_zero(self):
        with self.assertRaises(NewApiError):
            collect_turn_logs(NewApiConfig(), replace(self.turn, started_at="bad"))

    def test_token_filter_is_checked_locally_too(self):
        with patch("runner.newapi.fetch_logs", return_value=[
            {**self.error, "token_name": "another-app"},
            {**self.success, "token_name": "yonwork"},
        ]):
            sample = collect_turn_logs(NewApiConfig(), self.turn, token_name="yonwork", settle_seconds=0)
        self.assertEqual((1, 0), (sample.api_calls, sample.error_calls))


class FetchTests(unittest.TestCase):
    def test_pagination_and_timezone_conversion(self):
        opener = MagicMock()
        responses = []
        for i in (1, 2):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "success": True, "data": {"items": [{"id": i}], "total": 2},
            }).encode()
            responses.append(response)
        opener.open.side_effect = responses
        with patch("runner.newapi.build_opener", return_value=opener):
            rows = fetch_logs(NewApiConfig(),
                datetime.fromisoformat("2026-09-20T21:44:55+08:00"),
                datetime.fromisoformat("2026-09-20T21:44:58+08:00"), page_size=1, slack_seconds=0)
        self.assertEqual([{"id": 1}, {"id": 2}], rows)
        self.assertIn(f"start_timestamp={ts(13, 44, 55)}", opener.open.call_args.args[0].full_url)
        self.assertIn("p=2", opener.open.call_args.args[0].full_url)

    def test_malformed_response_is_not_an_empty_success(self):
        for data in ({}, {"items": [], "total": 1}, {"items": []}):
            with self.subTest(data=data), patch("runner.newapi.build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps({
                    "success": True, "data": data,
                }).encode()
                with self.assertRaises(NewApiError):
                    fetch_logs(NewApiConfig(), datetime(2026, 9, 20), datetime(2026, 9, 21))

    def test_changing_pagination_total_is_not_a_complete_snapshot(self):
        opener = MagicMock()
        responses = []
        for total in (2, 3):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "success": True, "data": {"items": [{"id": 1}], "total": total},
            }).encode()
            responses.append(response)
        opener.open.side_effect = responses
        with patch("runner.newapi.build_opener", return_value=opener), self.assertRaises(NewApiError):
            fetch_logs(NewApiConfig(), datetime(2026, 9, 20), datetime(2026, 9, 21), page_size=1)


if __name__ == "__main__":
    unittest.main()
