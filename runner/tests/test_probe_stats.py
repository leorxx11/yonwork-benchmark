from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runner.client_probe.scenarios import _spread_is_too_wide, _stat, summarize


def row(scenario: str, *, run_no: int, long_task: int, lag: float = 500.0) -> dict:
    return {
        "scenario": scenario,
        "run_no": run_no,
        "benchmark_id": f"{scenario}-r{run_no}",
        "ui_lag_first_ms": lag,
        "ui_lag_complete_ms": 100.0,
        "answer_chars": 1000,
        "text_updates": 1,
        "long_task_max_ms": long_task,
        "long_task_count": 1 if long_task else 0,
        "tool_calls": 0,
        "history_turns": 2,
    }


def write(rows: list[dict]) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for item in rows:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    handle.close()
    return Path(handle.name)


class SampleSufficiencyTests(unittest.TestCase):
    """样本够不够直接决定结论成不成立，所以这条规则必须被测到。"""

    def test_zero_spread_is_the_most_stable_case_not_the_least(self) -> None:
        """`0/0/0` = 三轮都没观测到主线程阻塞，是最稳的结果。

        早先写成 `middle <= 0 or spread > middle`，把它判成了样本不足——
        判太严会把干净结论也一起废掉，和判太松一样不可信。
        """
        self.assertFalse(_spread_is_too_wide([0, 0, 0]))
        self.assertFalse(_spread_is_too_wide([52, 52, 66]))

    def test_spread_wider_than_median_is_thin(self) -> None:
        # S4 实测：182 / 88 / 79，中位 88、跨度 103。
        self.assertTrue(_spread_is_too_wide([182, 88, 79]))

    def test_zero_median_with_real_spread_is_thin(self) -> None:
        """中位 0 但出现过一次 182，说明还没稳定，不能当「没卡顿」。"""
        self.assertTrue(_spread_is_too_wide([0, 0, 182]))

    def test_single_sample_is_never_enough(self) -> None:
        self.assertTrue(_spread_is_too_wide([100]))

    def test_stat_carries_a_per_metric_thin_flag(self) -> None:
        """`thin` 要逐指标算——整场景一个标志会漏掉绝对量那一侧。"""
        self.assertFalse(_stat([0, 0, 0])["thin"])
        self.assertTrue(_stat([182, 88, 79])["thin"])


class VerdictGatingTests(unittest.TestCase):
    def test_p2_is_inconclusive_when_the_driving_scenario_is_thin(self) -> None:
        """样本不足时**不能判通过**。

        拿一个还没稳定的数去下「renderer 不是瓶颈」这种结论，
        比没有结论更糟——它会被当成已经验证过的事实往下传。
        """
        path = write([
            row("S1", run_no=1, long_task=0),
            row("S1", run_no=2, long_task=0),
            row("S1", run_no=3, long_task=0),
            row("S4", run_no=1, long_task=182),
            row("S4", run_no=2, long_task=88),
            row("S4", run_no=3, long_task=79),
        ])
        self.addCleanup(path.unlink)
        verdict = summarize(path)["verdict"]["P2_long_task"]
        # 最大值确实没过 200ms 的线，但支撑它的那个场景样本不足。
        self.assertEqual(182, verdict["worst_ms"])
        self.assertLess(verdict["worst_ms"], verdict["threshold_ms"])
        self.assertEqual(["S4"], verdict["thin_scenarios"])
        self.assertTrue(verdict["inconclusive"])
        self.assertFalse(verdict["pass"])

    def test_p2_passes_only_when_every_contributing_scenario_is_stable(self) -> None:
        path = write([
            row("S1", run_no=1, long_task=0),
            row("S1", run_no=2, long_task=0),
            row("S1", run_no=3, long_task=0),
            row("S2", run_no=1, long_task=52),
            row("S2", run_no=2, long_task=52),
            row("S2", run_no=3, long_task=66),
        ])
        self.addCleanup(path.unlink)
        verdict = summarize(path)["verdict"]["P2_long_task"]
        self.assertEqual([], verdict["thin_scenarios"])
        self.assertFalse(verdict["inconclusive"])
        self.assertTrue(verdict["pass"])


if __name__ == "__main__":
    unittest.main()
