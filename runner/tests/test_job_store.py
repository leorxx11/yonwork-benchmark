from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from runner import job_store
from runner.job_store import NewPlan, PlanMode, create_plan


class _FakeDatabase:
    """把 connect() 换掉，只记录执行过的 SQL。

    这一层不该需要真库：计划创建的全部风险都在「插了几条、字段怎么填、
    出错了回不回滚」，这三件事看语句就能验。
    """

    def __init__(self) -> None:
        self.cursor = MagicMock()
        self.cursor.fetchall.return_value = []
        self.cursor.fetchone.return_value = None
        self.connection = MagicMock()
        self.connection.cursor.return_value.__enter__.return_value = self.cursor
        # 一次 create_plan 会开两段事务：ensure_schema 的建表/迁移，
        # 然后才是插任务。用列表而不是布尔值，免得前一段的成功
        # 把后一段的回滚盖掉——那会让这个测试永远是绿的。
        self.outcomes: list[str] = []

    @contextmanager
    def connect(self, *_args, **_kwargs):
        try:
            yield self.connection
            self.outcomes.append("commit")
        except Exception:
            self.outcomes.append("rollback")
            raise

    def inserted_jobs(self) -> list[tuple]:
        return [
            call.args[1]
            for call in self.cursor.execute.call_args_list
            if "INSERT INTO benchmark_jobs" in call.args[0]
        ]


class CreatePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _FakeDatabase()
        patcher = patch("runner.job_store.connect", self.database.connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        # ensure_schema 有进程级缓存，逐个用例都要从头来一遍
        job_store._SCHEMA_READY = False
        self.addCleanup(setattr, job_store, "_SCHEMA_READY", False)
        rows = patch(
            "runner.job_store.plan_jobs",
            side_effect=lambda plan_id: [{"plan_id": plan_id}],
        )
        rows.start()
        self.addCleanup(rows.stop)

    @staticmethod
    def _plan(*modes: PlanMode) -> NewPlan:
        return NewPlan(
            experiment_name="跨模式对比",
            case_set_id="smoke",
            modes=modes,
        )

    def test_every_mode_becomes_one_job_sharing_plan_and_name(self) -> None:
        create_plan(
            self._plan(
                PlanMode(product="yonwork", model_query="newapi"),
                PlanMode(product="yonwork", model_query=""),
                PlanMode(product="workbuddy", model_query=""),
            )
        )
        rows = self.database.inserted_jobs()
        self.assertEqual(3, len(rows))
        plan_ids = {row[1] for row in rows}
        self.assertEqual(1, len(plan_ids))
        self.assertNotIn(None, plan_ids)
        # 位置决定执行顺序，必须是 0/1/2 且与提交顺序一致
        self.assertEqual([0, 1, 2], [row[2] for row in rows])
        self.assertEqual(
            ["yonwork", "yonwork", "workbuddy"], [row[8] for row in rows]
        )
        # 同一个实验名 = 同一个 suite_id = 一份合并报告，这是整个特性的支点
        self.assertEqual({"跨模式对比"}, {row[5] for row in rows})
        # batch_id 仍要互不相同，否则 uk_job_batch 会把整组挡回去
        self.assertEqual(3, len({row[4] for row in rows}))

    def test_duplicate_mode_is_rejected_before_any_insert(self) -> None:
        """同一个组合选两次会被报告页合并成一列，轮次凭空翻倍还看不出来。"""
        with self.assertRaises(ValueError) as caught:
            create_plan(
                self._plan(
                    PlanMode(product="yonwork", model_query="newapi"),
                    PlanMode(product="yonwork", model_query=" newapi "),
                )
            )
        self.assertIn("重复", str(caught.exception))
        self.assertEqual([], self.database.inserted_jobs())

    def test_empty_mode_list_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_plan(self._plan())
        self.assertEqual([], self.database.inserted_jobs())

    def test_failure_midway_rolls_back_the_whole_plan(self) -> None:
        """半个计划比没有计划更糟：报告里缺的那一列和「那个模式全挂了」长得一样。"""
        calls: list[str] = []

        def explode(sql: str, *_args):
            calls.append(sql)
            if "INSERT INTO benchmark_jobs" in sql and len(
                [item for item in calls if "INSERT INTO benchmark_jobs" in item]
            ) == 2:
                raise RuntimeError("第二条插不进去")
            return None

        self.database.cursor.execute.side_effect = explode
        with self.assertRaises(RuntimeError):
            create_plan(
                self._plan(
                    PlanMode(product="yonwork", model_query="a"),
                    PlanMode(product="yonwork", model_query="b"),
                )
            )
        self.assertEqual("rollback", self.database.outcomes[-1])
        self.assertNotIn("commit", self.database.outcomes[1:])

    def test_blank_experiment_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_plan(
                NewPlan(
                    experiment_name="   ",
                    case_set_id="smoke",
                    modes=(PlanMode(),),
                )
            )


class PlanOrderingTests(unittest.TestCase):
    """同一计划的 N 条任务共用一个 created_at，光按时间排全是并列。"""

    def test_claim_and_active_job_order_by_plan_position(self) -> None:
        import inspect

        for source in (
            inspect.getsource(job_store.claim_next_job),
            inspect.getsource(job_store.active_job),
        ):
            self.assertIn("plan_position", source)


class PlanModeTests(unittest.TestCase):
    def test_title_falls_back_to_product_and_default_model(self) -> None:
        self.assertEqual("yonwork / 默认模型", PlanMode(product="yonwork").title)
        self.assertEqual(
            "yonwork / newapi",
            PlanMode(product="yonwork", model_query=" newapi ").title,
        )

    def test_key_ignores_surrounding_whitespace(self) -> None:
        self.assertEqual(
            PlanMode(product="yonwork", model_query="newapi").key,
            PlanMode(product="yonwork", model_query="  newapi  ").key,
        )


if __name__ == "__main__":
    unittest.main()
