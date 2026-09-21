from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from benchmark_companion.models import (
    ModelMode,
    RunStatus,
    SyncStatus,
    TaskItem,
    TokenStats,
    TokenStatus,
)
from benchmark_companion.storage import BenchmarkStore, new_run_record, now_iso


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.database = self.temp_dir / "companion.db"
        self.store = BenchmarkStore(self.database)

    def tearDown(self) -> None:
        for path in self.temp_dir.iterdir():
            path.unlink(missing_ok=True)
        self.temp_dir.rmdir()

    def test_session_progress_token_update_and_sync(self) -> None:
        items = [TaskItem(0, "Case01", 1, "你好"), TaskItem(1, "Case01", 2, "你好")]
        session = self.store.create_session(
            model_mode=ModelMode.NEW_API,
            items=items,
            workbook_path="C:/benchmark.xlsx",
            stats_script_path="C:/stats.ps1",
            token_name="workbuddy",
        )
        self.assertEqual("Cases", session.prompt_sheet)
        start = now_iso()
        self.store.begin_item(session.session_id, 0, start)
        record = new_run_record(
            session=session,
            item=items[0],
            status=RunStatus.SUCCESS,
            start_time=start,
            end_time=now_iso(),
            duration_seconds=1.25,
            note="",
        )
        updated_session = self.store.finish_item(record)
        self.assertEqual(1, updated_session.current_position)
        self.assertEqual(TokenStatus.PENDING, self.store.get_record(record.record_id).token_status)

        updated = self.store.update_token_stats(
            record.record_id, TokenStats(2, 0, 100, 20, 120, 1.5)
        )
        self.assertEqual(TokenStatus.SUCCESS, updated.token_status)
        self.assertEqual(1, len(self.store.pending_records()))

        self.store.mark_synced({record.record_id: 24})
        synced = self.store.get_record(record.record_id)
        self.assertEqual(SyncStatus.SYNCED, synced.sync_status)
        self.assertEqual(24, synced.excel_row)

    def test_existing_database_is_migrated_with_prompt_sheet(self) -> None:
        path = self.temp_dir / "legacy.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                """
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    model_mode TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    workbook_path TEXT NOT NULL,
                    stats_script_path TEXT NOT NULL,
                    token_name TEXT NOT NULL,
                    total_items INTEGER NOT NULL,
                    current_position INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'Active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

        BenchmarkStore(path)
        with closing(sqlite3.connect(path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(sessions)")
            }
        self.assertIn("prompt_sheet", columns)

    def test_skip_needs_no_timer_or_tokens(self) -> None:
        item = TaskItem(0, "Case01", 1, "你好")
        session = self.store.create_session(
            model_mode=ModelMode.WORKBUDDY_DEFAULT,
            items=[item],
            workbook_path="C:/benchmark.xlsx",
            stats_script_path="C:/stats.ps1",
            token_name="workbuddy",
        )
        record = new_run_record(
            session=session,
            item=item,
            status=RunStatus.SKIPPED,
            start_time=None,
            end_time=None,
            duration_seconds=None,
            note="not needed",
        )
        completed = self.store.finish_item(record)
        saved = self.store.get_record(record.record_id)
        self.assertEqual("Completed", completed.status)
        self.assertEqual(TokenStatus.NOT_APPLICABLE, saved.token_status)


if __name__ == "__main__":
    unittest.main()
