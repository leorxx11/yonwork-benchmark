from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from runner.db import to_millis, to_utc
from runner.ingest import (
    DEFAULT_MODEL_MODE,
    IngestError,
    _infer_model_mode,
    _model_request_rows,
    _run_row,
    _usage_rows,
    find_result_files,
    suite_id_for,
)


def record(**turn_overrides: object) -> dict:
    turn = {
        "run_id": "bench-1",
        "started_at": "2026-09-20T21:44:55.328+08:00",
        "ended_at": "2026-09-20T21:44:58.414+08:00",
        "duration_seconds": 3.086,
        "first_delta_seconds": 1.5,
        "terminated_by": "chat.complete",
        "stop_reason": "stop",
        "tool_calls": ["read_file"],
        "answer": "你好",
        "requested_model": None,
        "requested_model_label": None,
    }
    turn.update(turn_overrides)
    return {
        "benchmark_id": "bench-1",
        "batch_id": "20260920-214455",
        "position": 0,
        "case_name": "Case02",
        "run_no": 1,
        "prompt": "你好！",
        "session_key": "agent:main:bench-1",
        "verdict": "Pass",
        "product": "yonwork",
        "checks": [],
        "turn": turn,
        "usage": None,
        "log_stats": {"api_calls": 1, "error_calls": None, "source": "sse:run-id"},
        "created_at": "2026-09-20T21:44:58.500+08:00",
    }


class TimeTests(unittest.TestCase):
    def test_local_time_is_converted_to_utc(self) -> None:
        """库里一律 UTC：本机 WSL 和 Windows 差 8 秒，再叠时区就没法查了。"""
        self.assertEqual(
            datetime(2026, 9, 20, 13, 44, 55, 328000),
            to_utc("2026-09-20T21:44:55.328+08:00"),
        )

    def test_z_suffix_is_accepted(self) -> None:
        self.assertEqual(
            datetime(2026, 9, 20, 13, 13, 5, 580000), to_utc("2026-09-20T13:13:05.580Z")
        )

    def test_result_is_naive_for_mysql(self) -> None:
        self.assertIsNone(to_utc("2026-09-20T13:13:05.580Z").tzinfo)

    def test_garbage_time_is_none(self) -> None:
        self.assertIsNone(to_utc("昨天"))
        self.assertIsNone(to_utc(None))

    def test_millis(self) -> None:
        self.assertEqual(3086, to_millis(3.086))
        self.assertIsNone(to_millis(None))
        self.assertIsNone(to_millis(True))


class ModelModeTests(unittest.TestCase):
    def test_no_model_selection_is_default_mode(self) -> None:
        mode, ref = _infer_model_mode([record()])
        self.assertEqual(DEFAULT_MODEL_MODE, mode)
        self.assertEqual("", ref)

    def test_display_label_wins_over_model_id(self) -> None:
        """四模式视图按 'newapi' 分组，不是按 'deepseek-flash'。"""
        mode, ref = _infer_model_mode(
            [record(requested_model="deepseek-flash", requested_model_label="newapi")]
        )
        self.assertEqual("newapi", mode)
        self.assertEqual("deepseek-flash", ref)

    def test_old_records_without_label_fall_back_to_model_id(self) -> None:
        mode, _ = _infer_model_mode([record(requested_model="deepseek-flash")])
        self.assertEqual("deepseek-flash", mode)

    def test_mixed_models_in_one_batch_is_rejected(self) -> None:
        """一个批次只能是一个 (product, model_mode)，混了就没法横向比。"""
        with self.assertRaisesRegex(IngestError, "多个请求模型"):
            _infer_model_mode(
                [
                    record(requested_model="deepseek-flash", requested_model_label="newapi"),
                    record(requested_model="deepseek-v4-flash", requested_model_label="默认模型"),
                ]
            )


class RowTests(unittest.TestCase):
    def test_backend_counts_are_not_copied_into_other_sources(self):
        payload = record()
        payload["log_stats"] = {"source": "newapi", "api_calls": 2, "error_calls": 1}
        payload["usage_samples"] = [
            {"source": "device-api"}, {"source": "session-jsonl"},
            {"source": "newapi", "api_calls": 2, "error_calls": 1,
             "log_entries": [{"type": 2}, {"type": 4}]},
        ]
        rows = _usage_rows(payload)
        self.assertEqual([(None, None), (None, None), (2, 1)], [r[10:12] for r in rows])
        self.assertEqual([{"type": 2}, {"type": 4}], json.loads(rows[2][14])["log_entries"])

    def test_run_row_converts_seconds_to_millis(self) -> None:
        row = _run_row(record(), "batch-1")
        self.assertEqual(3086, row[10])
        self.assertEqual(1500, row[11])

    def test_no_rows_when_nothing_was_collected(self) -> None:
        """三来源都没采到时不能伪造一行 0，没采到就是没这行。"""
        self.assertEqual([], _usage_rows(record()))

    def test_one_row_per_source(self) -> None:
        payload = record()
        payload["usage_samples"] = [
            {"source": "device-api", "input_tokens": 20832, "total_tokens": 21123,
             "match": "time-window"},
            {"source": "session-jsonl", "input_tokens": 5586, "cache_read_tokens": 10496,
             "total_tokens": 16188, "model": "deepseek-flash", "match": "idempotency-key"},
        ]
        rows = _usage_rows(payload)
        self.assertEqual(["device-api", "session-jsonl"], [row[1] for row in rows])
        self.assertEqual("idempotency-key", rows[1][12])
        self.assertEqual(5586, rows[1][4])

    def test_legacy_single_usage_object_still_loads(self) -> None:
        """早期 JSONL 里 usage 是单个对象，重放历史批次时不能炸。"""
        payload = record()
        payload["usage"] = {"input_tokens": 20832, "total_tokens": 21123, "match": "time-window"}
        rows = _usage_rows(payload)
        self.assertEqual(1, len(rows))
        self.assertEqual("device-api", rows[0][1])

    def test_ledger_backfill_wins_over_the_turn_snapshot(self) -> None:
        """hook 绑定晚于快照到达：快照里未归属，账本最后一条 closed 已补上归属。"""
        base = {"proxy_id": "p", "received_at": "2026-09-23T10:00:00+08:00", "path": "/v1/x",
                "record_status": "closed", "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"}
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "model-requests.jsonl"
            lines = [
                {**base, "request_id": "r1", "sequence": 1, "attribution": "unattributed"},
                {**base, "request_id": "r1", "sequence": 1, "attribution": "attributed",
                 "run_id": "bench-1", "attribution_source": "traceparent+model_call_started"},
                {**base, "request_id": "r2", "sequence": 2, "attribution": "attributed",
                 "run_id": "other-batch-run"},
            ]
            ledger.write_text("".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8")
            payload = record()
            payload["model_calls"] = {"status": "unavailable", "ledger_path": str(ledger),
                                      "requests": []}
            rows = {row[0]: row for row in _model_request_rows([payload], "batch-1")}
        self.assertEqual("bench-1", rows["r1"][2])
        self.assertEqual("attributed", rows["r1"][5])
        # 归属到别的批次的请求不入本批：由它自己那一批入库，这里记成未归属就是污染
        self.assertNotIn("r2", rows)

    def test_suite_id_is_stable(self) -> None:
        self.assertEqual(suite_id_for("a"), suite_id_for("a"))
        self.assertNotEqual(suite_id_for("a"), suite_id_for("b"))


class DiscoveryTests(unittest.TestCase):
    def test_finds_every_batch_under_a_results_dir(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in ("20260920-1", "20260920-2"):
            (root / name).mkdir()
            (root / name / "results.jsonl").write_text(
                json.dumps(record()) + "\n", encoding="utf-8"
            )
        self.assertEqual(2, len(find_result_files(root)))
        self.assertEqual(1, len(find_result_files(root / "20260920-1" / "results.jsonl")))


if __name__ == "__main__":
    unittest.main()
