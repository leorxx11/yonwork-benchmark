from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from web import api


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        """顶栏那条「后台还在跑」要断掉，否则这套测试根本不是离线的。

        `_render` 一律调 `_active_job()` → `job_store.ensure_schema()`，
        本机 MySQL 恰好起着的时候它就会**真的连库并执行 DDL**——
        2026-09-21 实测：跑一次 web 单测就把 plan_* 三列 ALTER 进了实验库。
        库没起时它被 DatabaseError 兜住返回 None，所以这件事一直没人发现，
        测试结果还会随「容器开没开」而不同。
        """
        patcher = patch("web.api._active_job", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _request(path: str) -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [],
                "client": ("test", 1),
                "server": ("test", 80),
                "root_path": "",
                "app": api.app,
            }
        )

    def test_healthz_does_not_require_database(self) -> None:
        self.assertEqual({"status": "ok"}, api.healthz())

    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": True,
            "logged_in": True,
            "endpoint": "http://127.0.0.1:3211",
            "version": "1.0.8",
            "models": [],
            "problem": "",
        },
    )
    def test_new_job_page_reads_yaml_catalog(self, _runtime) -> None:
        response = api.new_job(self._request("/jobs/new"))
        self.assertEqual(200, response.status_code)
        body = response.body.decode()
        self.assertIn("基础冒烟", body)
        self.assertIn("cases/catalog.yaml", body)

    @patch("web.api.create_job", return_value={"job_id": "abc123"})
    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": False,
            "logged_in": False,
            "endpoint": "",
            "version": "",
            "models": [],
            "problem": "offline",
        },
    )
    def test_submit_job_redirects_to_persisted_job(self, _runtime, create) -> None:
        response = api.submit_job(
            self._request("/jobs"),
            experiment_name="回归测试",
            case_set_id="smoke",
            product="yonwork",
            model_query="",
            timeout_seconds="60",
            limit_runs="0",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual(303, response.status_code)
        self.assertEqual("/jobs/abc123", response.headers["location"])
        spec = create.call_args.args[0]
        self.assertEqual("smoke", spec.case_set_id)
        self.assertTrue(spec.collect_usage)

    @patch("web.api.create_job", return_value={"job_id": "abc123"})
    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": True,
            "logged_in": True,
            "endpoint": "http://127.0.0.1:3211",
            "version": "",
            "models": [],
            "problem": "",
        },
    )
    def test_blank_number_field_falls_back_instead_of_422(self, _runtime, create) -> None:
        # 数字输入框被清空时浏览器会提交空串。若让 pydantic 直接校验，
        # 用户拿到的是一页裸 422 JSON，而不是这个页面。
        response = api.submit_job(
            self._request("/jobs"),
            experiment_name="回归测试",
            case_set_id="smoke",
            product="yonwork",
            model_query="",
            timeout_seconds="",
            limit_runs="",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual(303, response.status_code)
        spec = create.call_args.args[0]
        self.assertEqual(600, spec.timeout_seconds)
        self.assertEqual(0, spec.limit_runs)

    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": True,
            "logged_in": True,
            "endpoint": "http://127.0.0.1:3211",
            "version": "",
            "models": [],
            "problem": "",
        },
    )
    def test_non_numeric_field_rerenders_form_with_message(self, _runtime) -> None:
        response = api.submit_job(
            self._request("/jobs"),
            experiment_name="回归测试",
            case_set_id="smoke",
            product="yonwork",
            model_query="",
            timeout_seconds="abc",
            limit_runs="0",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("单轮超时必须是数字", response.body.decode())

    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": True,
            "logged_in": True,
            "endpoint": "",
            "version": "",
            "models": [],
            "problem": "",
        },
    )
    def test_unknown_product_is_rejected_before_queueing(self, _runtime) -> None:
        # 表单值和 runner.drivers.DRIVERS 对不上时要当场拒绝，
        # 不能排进队列等 Worker 领了再失败——那时人已经走了。
        response = api.submit_job(
            self._request("/jobs"),
            experiment_name="回归测试",
            case_set_id="smoke",
            product="copilot",
            model_query="",
            timeout_seconds="60",
            limit_runs="0",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("copilot", response.body.decode())

    def test_new_job_page_offers_every_registered_driver(self) -> None:
        with patch("web.api._runtime_context", return_value={
            "ok": True, "logged_in": True, "endpoint": "", "version": "",
            "models": [], "problem": "",
        }):
            body = api.new_job(self._request("/jobs/new")).body.decode()
        for product in ("yonwork", "workbuddy"):
            self.assertIn(f'value="{product}"', body)

    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": True, "logged_in": True, "endpoint": "", "version": "",
            "models": [], "problem": "",
        },
    )
    def test_double_encoded_name_is_rejected_not_silently_stored(self, _runtime) -> None:
        """裸 UTF-8 字节提交的表单会被按 Latin-1 解，静默写出乱码实验名。

        实测过：`curl -d '实验名=会话分裂验证'` 入库变成 'ä¼\x9aè¯\x9d…'，
        任务照跑、不报错，只有页面显示是坏的。实验名还是 suite_id 的来源，
        编码错了同一个实验会裂成两份报告。
        """
        response = api.submit_job(
            self._request("/jobs"),
            experiment_name="会话分裂验证".encode("utf-8").decode("latin-1"),
            case_set_id="smoke",
            product="yonwork",
            model_query="",
            timeout_seconds="60",
            limit_runs="0",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual(400, response.status_code)
        body = response.body.decode()
        self.assertIn("双重编码", body)
        self.assertIn("会话分裂验证", body)  # 把猜到的正确值告诉用户

    @patch("web.api.create_job", return_value={"job_id": "abc123"})
    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": True, "logged_in": True, "endpoint": "", "version": "",
            "models": [], "problem": "",
        },
    )
    def test_normal_chinese_name_passes_through(self, _runtime, create) -> None:
        """别把正常中文名也拦了——浏览器提交的就是这种。"""
        response = api.submit_job(
            self._request("/jobs"),
            experiment_name="会话分裂验证",
            case_set_id="smoke",
            product="yonwork",
            model_query="",
            timeout_seconds="60",
            limit_runs="0",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual(303, response.status_code)
        self.assertEqual("会话分裂验证", create.call_args.args[0].experiment_name)

    # ---- 跨模式一键编排 ----

    @patch("web.api.create_plan", return_value={"plan_id": "p" * 32})
    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": True, "logged_in": True, "endpoint": "", "version": "",
            "models": [], "problem": "",
        },
    )
    def test_multiple_modes_become_one_plan(self, _runtime, create) -> None:
        response = api.submit_job(
            self._request("/jobs"),
            experiment_name="四模式对比",
            case_set_id="smoke",
            mode_product=["yonwork", "yonwork", "workbuddy"],
            mode_model=["newapi", "", ""],
            timeout_seconds="60",
            limit_runs="0",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual(303, response.status_code)
        self.assertEqual(f"/plans/{'p' * 32}", response.headers["location"])
        spec = create.call_args.args[0]
        self.assertEqual(3, len(spec.modes))
        # 顺序就是执行顺序，不能被去重/排序打乱
        self.assertEqual(
            [("yonwork", "newapi"), ("yonwork", ""), ("workbuddy", "")],
            [mode.key for mode in spec.modes],
        )

    @patch("web.api.create_job", return_value={"job_id": "abc123"})
    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": True, "logged_in": True, "endpoint": "", "version": "",
            "models": [], "problem": "",
        },
    )
    def test_single_mode_still_creates_a_plain_job(self, _runtime, create) -> None:
        """一个模式不该被包成计划——那会给最常见的路径多一次跳转。"""
        response = api.submit_job(
            self._request("/jobs"),
            experiment_name="单模式",
            case_set_id="smoke",
            mode_product=["yonwork"],
            mode_model=["newapi"],
            timeout_seconds="60",
            limit_runs="0",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual("/jobs/abc123", response.headers["location"])
        self.assertEqual("newapi", create.call_args.args[0].model_query)

    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": True, "logged_in": True, "endpoint": "", "version": "",
            "models": [], "problem": "",
        },
    )
    def test_mismatched_mode_columns_are_rejected(self, _runtime) -> None:
        """两个重复字段按下标配对，长度对不上就不能猜着配——配错了跑的是别的模型。"""
        response = api.submit_job(
            self._request("/jobs"),
            experiment_name="四模式对比",
            case_set_id="smoke",
            mode_product=["yonwork", "workbuddy"],
            mode_model=["newapi"],
            timeout_seconds="60",
            limit_runs="0",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("对不上", response.body.decode())

    @patch(
        "web.api._runtime_context",
        return_value={
            "ok": True, "logged_in": True, "endpoint": "", "version": "",
            "models": [], "problem": "",
        },
    )
    def test_unknown_product_in_a_mode_row_is_rejected(self, _runtime) -> None:
        response = api.submit_job(
            self._request("/jobs"),
            experiment_name="四模式对比",
            case_set_id="smoke",
            mode_product=["yonwork", "copilot"],
            mode_model=["", ""],
            timeout_seconds="60",
            limit_runs="0",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("copilot", response.body.decode())

    @patch("web.api.plan_jobs")
    def test_plan_page_aggregates_progress_and_links_one_report(self, jobs) -> None:
        jobs.return_value = [
            {
                "job_id": "j1", "plan_id": "p1", "plan_position": 0,
                "plan_label": "yonwork / newapi", "batch_id": "b1",
                "experiment_name": "四模式对比", "case_set_id": "smoke",
                "product": "yonwork", "model_query": "newapi",
                "status": "Completed", "completed_runs": 4, "total_runs": 4,
                "suite_id": "suite-1", "error": "",
            },
            {
                "job_id": "j2", "plan_id": "p1", "plan_position": 1,
                "plan_label": "workbuddy / 默认模型", "batch_id": "b2",
                "experiment_name": "四模式对比", "case_set_id": "smoke",
                "product": "workbuddy", "model_query": "",
                "status": "Running", "completed_runs": 1, "total_runs": 4,
                "suite_id": None, "error": "",
            },
        ]
        response = api.plan_status(self._request("/plans/p1/status"), "p1")
        body = response.body.decode()
        self.assertEqual(200, response.status_code)
        self.assertIn("5/8", body)          # 累计轮次跨模式相加
        self.assertIn("1/2", body)          # 已结束的模式
        self.assertIn('href="/suite/suite-1"', body)
        self.assertIn('href="/matrix/suite-1"', body)
        # 还没跑完就不该让浏览器停轮询
        self.assertNotIn("X-Benchmark-Poll", response.headers)

    @patch("web.api.plan_jobs")
    def test_one_failed_mode_does_not_stall_the_plan(self, jobs) -> None:
        """一个模式挂了，整组照样算「已全部结束」，报告里只是缺那一列。"""
        jobs.return_value = [
            {
                "job_id": "j1", "plan_id": "p1", "plan_position": 0,
                "plan_label": "yonwork / newapi", "batch_id": "b1",
                "experiment_name": "四模式对比", "case_set_id": "smoke",
                "product": "yonwork", "model_query": "newapi",
                "status": "Completed", "completed_runs": 4, "total_runs": 4,
                "suite_id": "suite-1", "error": "",
            },
            {
                "job_id": "j2", "plan_id": "p1", "plan_position": 1,
                "plan_label": "workbuddy / 默认模型", "batch_id": "b2",
                "experiment_name": "四模式对比", "case_set_id": "smoke",
                "product": "workbuddy", "model_query": "",
                "status": "Failed", "completed_runs": 0, "total_runs": 4,
                "suite_id": None, "error": "容器里的 Worker 起不了 Windows 进程",
            },
        ]
        response = api.plan_status(self._request("/plans/p1/status"), "p1")
        body = response.body.decode()
        self.assertEqual("stop", response.headers["X-Benchmark-Poll"])
        self.assertIn('href="/suite/suite-1"', body)
        # 失败原因写在它自己那一行，不汇总到计划层
        self.assertIn("起不了 Windows 进程", body)

    @patch("web.api.plan_jobs", return_value=[])
    def test_unknown_plan_is_404(self, _jobs) -> None:
        with self.assertRaises(HTTPException) as caught:
            api.plan_detail(self._request("/plans/nope"), "nope")
        self.assertEqual(404, caught.exception.status_code)

    @patch("web.api.list_events", return_value=[])
    @patch("web.api.get_job")
    def test_terminal_job_stops_browser_polling(self, get, _events) -> None:
        get.return_value = {
            "job_id": "abc123",
            "batch_id": "batch-1",
            "experiment_name": "回归测试",
            "case_set_id": "smoke",
            "model_query": "",
            "status": "Completed",
            "completed_runs": 1,
            "total_runs": 1,
            "suite_id": "suite-1",
            "error": "",
        }
        response = api.job_status(self._request("/jobs/abc123/status"), "abc123")
        self.assertEqual(200, response.status_code)
        self.assertEqual("stop", response.headers["X-Benchmark-Poll"])
        self.assertIn('href="/suite/suite-1"', response.body.decode())


if __name__ == "__main__":
    unittest.main()
