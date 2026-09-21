from __future__ import annotations

import unittest
from unittest.mock import patch

from starlette.requests import Request

from web import api


class WebApiTests(unittest.TestCase):
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
            model_query="",
            timeout_seconds="abc",
            limit_runs="0",
            collect_usage=True,
            export_xlsx=True,
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("单轮超时必须是数字", response.body.decode())

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
        self.assertIn("查看报告", response.body.decode())


if __name__ == "__main__":
    unittest.main()
