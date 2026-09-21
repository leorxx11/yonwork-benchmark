from __future__ import annotations

import inspect
import unittest

from runner.models import USAGE_SOURCES
from web import queries


class UsageSourceCoverageTests(unittest.TestCase):
    """报告查询必须认得每一个用量来源。

    2026-09-21 实测撞到过：`workbuddy-cli` 早就进了 `USAGE_SOURCES`，
    却没进 `mode_summary` / `matrix` 的 COALESCE，结果 WorkBuddy 那一行的
    token **整列是空的**——跟端上端点漏记（CLAUDE.md 六-3）长得一模一样，
    看不出是查询漏了还是产品漏记了。

    这条是结构性的：不跑真库，只盯「来源表和 SQL 是否同步」。
    接新产品时忘了改查询，这里会当场红，而不是等到跑完对比才发现空列。
    """

    def _sql(self) -> str:
        return inspect.getsource(queries)

    def test_every_usage_source_appears_in_the_report_queries(self) -> None:
        sql = self._sql()
        for source in USAGE_SOURCES:
            self.assertIn(f"'{source}'", sql, f"{source} 没有出现在报告查询里")

    def test_token_fallback_covers_every_source_in_mode_summary(self) -> None:
        sql = inspect.getsource(queries.mode_summary)
        for source in USAGE_SOURCES:
            self.assertIn(f"'{source}'", sql, f"mode_summary 漏了 {source}")

    def test_token_fallback_covers_every_source_in_matrix(self) -> None:
        sql = inspect.getsource(queries.matrix)
        for source in USAGE_SOURCES:
            self.assertIn(f"'{source}'", sql, f"matrix 漏了 {source}")


class DurationSplitTests(unittest.TestCase):
    def test_mode_summary_reports_engine_time_separately(self) -> None:
        """跨产品比耗时前必须能拆出冷启动，否则比的是起进程不是模型。"""
        sql = inspect.getsource(queries.mode_summary)
        self.assertIn("engine_ms", sql)
        self.assertIn("avg_startup_ms", sql)
        # engine_rows 用来区分「没有冷启动这回事」和「冷启动为 0」
        self.assertIn("engine_rows", sql)


if __name__ == "__main__":
    unittest.main()
