from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

from .models import (
    ModelMode,
    RunRecord,
    RunStatus,
    SessionInfo,
    SyncStatus,
    TaskItem,
    TokenStats,
    TokenStatus,
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class StorageError(RuntimeError):
    pass


class BenchmarkStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    model_mode TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    workbook_path TEXT NOT NULL,
                    prompt_sheet TEXT NOT NULL DEFAULT 'Cases',
                    stats_script_path TEXT NOT NULL,
                    token_name TEXT NOT NULL,
                    total_items INTEGER NOT NULL,
                    current_position INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'Active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_items (
                    session_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    case_name TEXT NOT NULL,
                    run_no INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'Pending',
                    start_time TEXT,
                    record_id TEXT,
                    PRIMARY KEY (session_id, position),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    record_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    product TEXT NOT NULL,
                    case_name TEXT NOT NULL,
                    run_no INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    duration_seconds REAL,
                    model TEXT NOT NULL,
                    api_calls INTEGER,
                    error_calls INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    api_use_time REAL,
                    status TEXT NOT NULL,
                    token_status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    sync_status TEXT NOT NULL DEFAULT 'Pending',
                    workbook_path TEXT NOT NULL,
                    stats_script_path TEXT NOT NULL,
                    token_name TEXT NOT NULL,
                    excel_row INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    synced_at TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
                    UNIQUE (session_id, position)
                );

                CREATE INDEX IF NOT EXISTS idx_runs_sync_status
                    ON runs(sync_status, token_status);
                CREATE INDEX IF NOT EXISTS idx_runs_token_status
                    ON runs(token_status, updated_at);
                """
            )
            session_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "prompt_sheet" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN prompt_sheet TEXT NOT NULL DEFAULT 'Cases'"
                )

    def create_session(
        self,
        *,
        model_mode: ModelMode,
        items: Sequence[TaskItem],
        workbook_path: str,
        stats_script_path: str,
        token_name: str,
        prompt_sheet: str = "Cases",
    ) -> SessionInfo:
        if not items:
            raise StorageError("无法创建空批次")
        session_id = str(uuid.uuid4())
        timestamp = now_iso()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, model_mode, model_name, workbook_path,
                    prompt_sheet, stats_script_path, token_name, total_items,
                    current_position, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'Active', ?, ?)
                """,
                (
                    session_id,
                    model_mode.value,
                    model_mode.model_name,
                    workbook_path,
                    prompt_sheet,
                    stats_script_path,
                    token_name,
                    len(items),
                    timestamp,
                    timestamp,
                ),
            )
            connection.executemany(
                """
                INSERT INTO session_items (
                    session_id, position, case_name, run_no, prompt, state
                ) VALUES (?, ?, ?, ?, ?, 'Pending')
                """,
                [
                    (
                        session_id,
                        item.position,
                        item.case_name,
                        item.run_no,
                        item.prompt,
                    )
                    for item in items
                ],
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> SessionInfo:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise StorageError(f"找不到批次：{session_id}")
        return self._session_from_row(row)

    def get_active_session(self) -> SessionInfo | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE status = 'Active'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self._session_from_row(row) if row else None

    def get_session_items(self, session_id: str) -> list[TaskItem]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT position, case_name, run_no, prompt, state, start_time, record_id
                FROM session_items
                WHERE session_id = ?
                ORDER BY position
                """,
                (session_id,),
            ).fetchall()
        return [
            TaskItem(
                position=row["position"],
                case_name=row["case_name"],
                run_no=row["run_no"],
                prompt=row["prompt"],
                state=row["state"],
                start_time=row["start_time"],
                record_id=row["record_id"],
            )
            for row in rows
        ]

    def get_current_item(self, session_id: str) -> TaskItem | None:
        session = self.get_session(session_id)
        if session.current_position >= session.total_items:
            return None
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT position, case_name, run_no, prompt, state, start_time, record_id
                FROM session_items
                WHERE session_id = ? AND position = ?
                """,
                (session_id, session.current_position),
            ).fetchone()
        if row is None:
            raise StorageError("批次游标指向了不存在的任务")
        return TaskItem(
            position=row["position"],
            case_name=row["case_name"],
            run_no=row["run_no"],
            prompt=row["prompt"],
            state=row["state"],
            start_time=row["start_time"],
            record_id=row["record_id"],
        )

    def begin_item(self, session_id: str, position: int, start_time: str) -> None:
        timestamp = now_iso()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE session_items
                SET state = 'Running', start_time = ?
                WHERE session_id = ? AND position = ? AND state = 'Pending'
                """,
                (start_time, session_id, position),
            )
            if cursor.rowcount != 1:
                state = connection.execute(
                    """
                    SELECT state FROM session_items
                    WHERE session_id = ? AND position = ?
                    """,
                    (session_id, position),
                ).fetchone()
                actual = state["state"] if state else "Missing"
                raise StorageError(f"任务无法开始，当前状态为 {actual}")
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (timestamp, session_id),
            )

    def finish_item(self, record: RunRecord) -> SessionInfo:
        timestamp = now_iso()
        with self._connection() as connection:
            item = connection.execute(
                """
                SELECT state FROM session_items
                WHERE session_id = ? AND position = ?
                """,
                (record.session_id, record.position),
            ).fetchone()
            if item is None:
                raise StorageError("找不到要结束的任务")
            if item["state"] == "Completed":
                existing = connection.execute(
                    "SELECT record_id FROM runs WHERE session_id = ? AND position = ?",
                    (record.session_id, record.position),
                ).fetchone()
                if existing and existing["record_id"] == record.record_id:
                    return self.get_session(record.session_id)
                raise StorageError("该任务已经记录，拒绝重复保存")
            expected_state = "Pending" if record.status is RunStatus.SKIPPED else "Running"
            if item["state"] != expected_state:
                raise StorageError(
                    f"任务状态为 {item['state']}，不能保存为 {record.status.value}"
                )

            connection.execute(
                """
                INSERT INTO runs (
                    record_id, session_id, position, product, case_name, run_no,
                    prompt, start_time, end_time, duration_seconds, model,
                    api_calls, error_calls, input_tokens, output_tokens,
                    total_tokens, api_use_time, status, token_status, note,
                    sync_status, workbook_path, stats_script_path, token_name,
                    excel_row, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                self._record_values(record),
            )
            connection.execute(
                """
                UPDATE session_items
                SET state = 'Completed', record_id = ?
                WHERE session_id = ? AND position = ?
                """,
                (record.record_id, record.session_id, record.position),
            )
            session_row = connection.execute(
                "SELECT total_items FROM sessions WHERE session_id = ?",
                (record.session_id,),
            ).fetchone()
            if session_row is None:
                raise StorageError("找不到任务所属批次")
            next_position = record.position + 1
            session_status = (
                "Completed" if next_position >= session_row["total_items"] else "Active"
            )
            connection.execute(
                """
                UPDATE sessions
                SET current_position = ?, status = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (next_position, session_status, timestamp, record.session_id),
            )
        return self.get_session(record.session_id)

    def abandon_session(self, session_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE sessions SET status = 'Abandoned', updated_at = ?
                WHERE session_id = ? AND status = 'Active'
                """,
                (now_iso(), session_id),
            )

    def get_record(self, record_id: str) -> RunRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE record_id = ?", (record_id,)
            ).fetchone()
        if row is None:
            raise StorageError(f"找不到记录：{record_id}")
        return self._record_from_row(row)

    def update_token_stats(self, record_id: str, stats: TokenStats) -> RunRecord:
        token_status = (
            TokenStatus.SUCCESS if stats.api_calls > 0 else TokenStatus.NO_DATA
        )
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE runs SET
                    api_calls = ?, error_calls = ?, input_tokens = ?,
                    output_tokens = ?, total_tokens = ?, api_use_time = ?,
                    token_status = ?, sync_status = 'Pending', updated_at = ?
                WHERE record_id = ?
                """,
                (
                    stats.api_calls,
                    stats.error_calls,
                    stats.input_tokens,
                    stats.output_tokens,
                    stats.total_tokens,
                    stats.api_use_time,
                    token_status.value,
                    now_iso(),
                    record_id,
                ),
            )
        return self.get_record(record_id)

    def update_token_error(self, record_id: str, message: str) -> RunRecord:
        existing = self.get_record(record_id)
        note = existing.note
        token_note = f"Token 查询失败：{message}"
        if token_note not in note:
            note = f"{note} | {token_note}".strip(" |")
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE runs SET token_status = 'Error', note = ?,
                    sync_status = 'Pending', updated_at = ?
                WHERE record_id = ?
                """,
                (note, now_iso(), record_id),
            )
        return self.get_record(record_id)

    def pending_records(self) -> list[RunRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE sync_status = 'Pending' AND token_status <> 'Pending'
                ORDER BY created_at, record_id
                """
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def count_pending(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE sync_status = 'Pending'"
            ).fetchone()
        return int(row["count"])

    def latest_retryable_token_record(self) -> RunRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE token_status IN ('NoData', 'Error')
                  AND start_time IS NOT NULL AND end_time IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self._record_from_row(row) if row else None

    def mark_synced(self, row_by_record: dict[str, int]) -> None:
        timestamp = now_iso()
        with self._connection() as connection:
            connection.executemany(
                """
                UPDATE runs SET sync_status = 'Synced', excel_row = ?,
                    synced_at = ?, updated_at = ?
                WHERE record_id = ?
                """,
                [
                    (excel_row, timestamp, timestamp, record_id)
                    for record_id, excel_row in row_by_record.items()
                ],
            )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionInfo:
        return SessionInfo(
            session_id=row["session_id"],
            model_mode=ModelMode(row["model_mode"]),
            model_name=row["model_name"],
            workbook_path=row["workbook_path"],
            prompt_sheet=row["prompt_sheet"],
            stats_script_path=row["stats_script_path"],
            token_name=row["token_name"],
            total_items=row["total_items"],
            current_position=row["current_position"],
            status=row["status"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _record_values(record: RunRecord) -> tuple[object, ...]:
        return (
            record.record_id,
            record.session_id,
            record.position,
            record.product,
            record.case_name,
            record.run_no,
            record.prompt,
            record.start_time,
            record.end_time,
            record.duration_seconds,
            record.model,
            record.api_calls,
            record.error_calls,
            record.input_tokens,
            record.output_tokens,
            record.total_tokens,
            record.api_use_time,
            record.status.value,
            record.token_status.value,
            record.note,
            record.sync_status.value,
            record.workbook_path,
            record.stats_script_path,
            record.token_name,
            record.excel_row,
            record.created_at,
            record.updated_at,
        )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            record_id=row["record_id"],
            session_id=row["session_id"],
            position=row["position"],
            product=row["product"],
            case_name=row["case_name"],
            run_no=row["run_no"],
            prompt=row["prompt"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            duration_seconds=row["duration_seconds"],
            model=row["model"],
            api_calls=row["api_calls"],
            error_calls=row["error_calls"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            total_tokens=row["total_tokens"],
            api_use_time=row["api_use_time"],
            status=RunStatus(row["status"]),
            token_status=TokenStatus(row["token_status"]),
            note=row["note"],
            sync_status=SyncStatus(row["sync_status"]),
            workbook_path=row["workbook_path"],
            stats_script_path=row["stats_script_path"],
            token_name=row["token_name"],
            excel_row=row["excel_row"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def new_run_record(
    *,
    session: SessionInfo,
    item: TaskItem,
    status: RunStatus,
    start_time: str | None,
    end_time: str | None,
    duration_seconds: float | None,
    note: str,
) -> RunRecord:
    timestamp = now_iso()
    token_status = (
        TokenStatus.PENDING
        if session.model_mode is ModelMode.NEW_API and status is not RunStatus.SKIPPED
        else TokenStatus.NOT_APPLICABLE
    )
    return RunRecord(
        record_id=str(uuid.uuid4()),
        session_id=session.session_id,
        position=item.position,
        product="WorkBuddy",
        case_name=item.case_name,
        run_no=item.run_no,
        prompt=item.prompt,
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration_seconds,
        model=session.model_name,
        api_calls=None,
        error_calls=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        api_use_time=None,
        status=status,
        token_status=token_status,
        note=note,
        sync_status=SyncStatus.PENDING,
        workbook_path=session.workbook_path,
        stats_script_path=session.stats_script_path,
        token_name=session.token_name,
        created_at=timestamp,
        updated_at=timestamp,
    )
