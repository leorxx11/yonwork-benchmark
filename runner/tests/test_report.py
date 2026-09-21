from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from openpyxl import load_workbook

from runner.models import Check, ChatTurn, Layer, LogStats, RunRecord, UsageSample, Verdict, now_iso
from runner.report import append_jsonl, build_database, export_xlsx, summarize


def make_record(position: int, verdict: Verdict) -> RunRecord:
    benchmark_id = f"bench-Case0{position}-r1-abc{position}"
    return RunRecord(
        benchmark_id=benchmark_id,
        batch_id="20260920-120000",
        position=position,
        case_name=f"Case0{position}",
        run_no=1,
        prompt="你好",
        session_key=f"agent:main:{benchmark_id}",
        verdict=verdict,
        checks=(
            Check(Layer.COMPLETION, "turn-completed", Verdict.PASS, "chat.complete"),
            Check.skipped(Layer.LOG, "error-calls", "未采集"),
        ),
        turn=ChatTurn(
            benchmark_id=benchmark_id,
            session_key=f"agent:main:{benchmark_id}",
            prompt="你好",
            started_at=now_iso(),
            ended_at=now_iso(),
            duration_seconds=3.5,
            run_id=benchmark_id,
            answer="你好呀",
            terminated_by="chat.complete",
            stop_reason="stop",
            event_counts={"chat.message": 4},
            tool_calls=("read_file",),
        ),
        usage_samples=(
            UsageSample(
                source="device-api", input_tokens=20832, output_tokens=35,
                total_tokens=21123, match="time-window",
            ),
            UsageSample(
                source="session-jsonl", input_tokens=10336, output_tokens=35,
                total_tokens=21123, cache_read_tokens=10496, match="idempotency-key",
            ),
        ),
        log_stats=LogStats(api_calls=1, error_calls=None, source="recent-token-history"),
    )


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out_dir = Path(temporary.name)
        self.jsonl = self.out_dir / "results.jsonl"

    def test_jsonl_is_appended_one_line_per_run(self) -> None:
        append_jsonl(self.jsonl, make_record(1, Verdict.PASS))
        append_jsonl(self.jsonl, make_record(2, Verdict.FAIL))
        lines = self.jsonl.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(2, len(lines))
        payload = json.loads(lines[0])
        self.assertEqual("Pass", payload["verdict"])
        self.assertEqual("chat.complete", payload["turn"]["terminated_by"])
        self.assertIsNone(payload["checks"][1]["verdict"])

    def test_jsonl_to_sqlite_to_xlsx(self) -> None:
        append_jsonl(self.jsonl, make_record(1, Verdict.PASS))
        append_jsonl(self.jsonl, make_record(2, Verdict.INVALID))

        database = self.out_dir / "results.db"
        self.assertEqual(2, build_database(self.jsonl, database))

        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM runs ORDER BY position").fetchall()
            checks = connection.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
        self.assertEqual(["Pass", "Invalid"], [row["verdict"] for row in rows])
        self.assertEqual(20832, rows[0]["input_tokens"])  # 端上优先
        self.assertEqual("device-api", rows[0]["usage_source"])
        self.assertIsNone(rows[0]["error_calls"])
        self.assertEqual(4, checks)

        xlsx = export_xlsx(database, self.out_dir / "results.xlsx")
        workbook = load_workbook(xlsx)
        self.assertEqual(["Results", "Checks", "Summary"], workbook.sheetnames)
        self.assertEqual("BenchmarkId", workbook["Results"].cell(1, 1).value)
        self.assertEqual(3, workbook["Results"].max_row)
        workbook.close()

    def test_rerunning_the_same_benchmark_id_overwrites(self) -> None:
        append_jsonl(self.jsonl, make_record(1, Verdict.FAIL))
        append_jsonl(self.jsonl, make_record(1, Verdict.PASS))
        database = self.out_dir / "results.db"
        build_database(self.jsonl, database)
        with closing(sqlite3.connect(database)) as connection:
            verdicts = [row[0] for row in connection.execute("SELECT verdict FROM runs")]
        self.assertEqual(["Pass"], verdicts)

    def test_summary_takes_the_worst_verdict(self) -> None:
        append_jsonl(self.jsonl, make_record(1, Verdict.PASS))
        append_jsonl(self.jsonl, make_record(2, Verdict.FAIL))
        append_jsonl(self.jsonl, make_record(3, Verdict.ERROR))
        summary = summarize(self.jsonl)
        self.assertEqual(3, summary.total)
        self.assertEqual(Verdict.ERROR, summary.worst_verdict)
        self.assertIn("Fail=1", summary.as_text())


if __name__ == "__main__":
    unittest.main()
