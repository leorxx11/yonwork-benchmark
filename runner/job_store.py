from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Iterator

import pymysql

from .db import DatabaseError, connect, open_connection


QUEUED = "Queued"
RUNNING = "Running"
COMPLETED = "Completed"
FAILED = "Failed"
CANCELLED = "Cancelled"
TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, CANCELLED})
WORKER_LOCK_NAME = "yonwork-benchmark:worker"
_SCHEMA_READY = False
_SCHEMA_LOCK = Lock()


class WorkerAlreadyRunning(RuntimeError):
    pass


JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_jobs (
    job_id             CHAR(32)     NOT NULL PRIMARY KEY,
    batch_id           VARCHAR(64)  NOT NULL,
    experiment_name    VARCHAR(128) NOT NULL,
    case_catalog_path  VARCHAR(512) NOT NULL DEFAULT 'cases/catalog.yaml',
    case_set_id        VARCHAR(64)  NOT NULL,
    product            VARCHAR(32)  NOT NULL DEFAULT 'yonwork',
    model_query        VARCHAR(128) NOT NULL DEFAULT '',
    agent_id           VARCHAR(64)  NOT NULL DEFAULT 'main',
    timeout_seconds    DECIMAL(10,3) NOT NULL DEFAULT 600,
    limit_runs         INT NOT NULL DEFAULT 0,
    collect_usage      BOOLEAN NOT NULL DEFAULT TRUE,
    export_xlsx        BOOLEAN NOT NULL DEFAULT TRUE,
    status             ENUM('Queued','Running','Completed','Failed','Cancelled')
                       NOT NULL DEFAULT 'Queued',
    total_runs         INT NOT NULL DEFAULT 0,
    completed_runs     INT NOT NULL DEFAULT 0,
    suite_id           VARCHAR(64) NULL,
    results_path       VARCHAR(512) NULL,
    worker_id          VARCHAR(128) NULL,
    cancel_requested   BOOLEAN NOT NULL DEFAULT FALSE,
    error              TEXT,
    created_at         DATETIME(3) NOT NULL,
    started_at         DATETIME(3) NULL,
    finished_at        DATETIME(3) NULL,
    updated_at         DATETIME(3) NOT NULL,
    UNIQUE KEY uk_job_batch (batch_id),
    KEY idx_job_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_job_events (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    job_id      CHAR(32) NOT NULL,
    level       VARCHAR(16) NOT NULL DEFAULT 'info',
    message     TEXT NOT NULL,
    created_at  DATETIME(3) NOT NULL,
    KEY idx_job_event (job_id, id),
    CONSTRAINT fk_job_event FOREIGN KEY (job_id)
        REFERENCES benchmark_jobs (job_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class NewJob:
    experiment_name: str
    case_set_id: str
    case_catalog_path: str = "cases/catalog.yaml"
    product: str = "yonwork"
    model_query: str = ""
    agent_id: str = "main"
    timeout_seconds: float = 600.0
    limit_runs: int = 0
    collect_usage: bool = True
    export_xlsx: bool = True


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(JOBS_SCHEMA)
                cursor.execute(EVENTS_SCHEMA)
        _SCHEMA_READY = True


def create_job(spec: NewJob) -> dict[str, Any]:
    name = spec.experiment_name.strip()
    if not name:
        raise ValueError("实验名称不能为空")
    if spec.timeout_seconds <= 0:
        raise ValueError("单轮超时必须大于 0")
    if spec.limit_runs < 0:
        raise ValueError("轮次上限不能小于 0")

    ensure_schema()
    job_id = uuid.uuid4().hex
    stamp = utc_now()
    batch_id = f"web-{stamp:%Y%m%d-%H%M%S}-{job_id[:8]}"
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO benchmark_jobs (
                    job_id, batch_id, experiment_name, case_catalog_path,
                    case_set_id, product, model_query, agent_id, timeout_seconds,
                    limit_runs, collect_usage, export_xlsx, status,
                    created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    job_id,
                    batch_id,
                    name,
                    spec.case_catalog_path,
                    spec.case_set_id,
                    spec.product,
                    spec.model_query.strip(),
                    spec.agent_id,
                    spec.timeout_seconds,
                    spec.limit_runs,
                    spec.collect_usage,
                    spec.export_xlsx,
                    QUEUED,
                    stamp,
                    stamp,
                ),
            )
            cursor.execute(
                "INSERT INTO benchmark_job_events (job_id, level, message, created_at)"
                " VALUES (%s,%s,%s,%s)",
                (job_id, "info", "任务已进入队列", stamp),
            )
    found = get_job(job_id)
    if found is None:  # pragma: no cover - 数据库刚插入却读不到，只能算基础设施故障
        raise RuntimeError(f"任务创建后无法读取：{job_id}")
    return found


def get_job(job_id: str) -> dict[str, Any] | None:
    ensure_schema()
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM benchmark_jobs WHERE job_id = %s", (job_id,))
            return cursor.fetchone()


def list_jobs(limit: int = 100) -> list[dict[str, Any]]:
    ensure_schema()
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM benchmark_jobs ORDER BY created_at DESC LIMIT %s",
                (max(1, min(limit, 500)),),
            )
            return list(cursor.fetchall())


def list_events(job_id: str, limit: int = 200) -> list[dict[str, Any]]:
    ensure_schema()
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, level, message, created_at
                FROM benchmark_job_events
                WHERE job_id = %s
                ORDER BY id DESC LIMIT %s
                """,
                (job_id, max(1, min(limit, 1000))),
            )
            return list(reversed(cursor.fetchall()))


def append_event(job_id: str, message: str, level: str = "info") -> None:
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO benchmark_job_events (job_id, level, message, created_at)"
                " VALUES (%s,%s,%s,%s)",
                (job_id, level[:16], message, utc_now()),
            )


def claim_next_job(worker_id: str) -> dict[str, Any] | None:
    """原子领取一条任务；MySQL 8 的 SKIP LOCKED 防止多个 Worker 重复执行。"""

    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM benchmark_jobs
                WHERE status = 'Queued' AND cancel_requested = FALSE
                ORDER BY created_at
                LIMIT 1 FOR UPDATE SKIP LOCKED
                """
            )
            job = cursor.fetchone()
            if not job:
                return None
            stamp = utc_now()
            cursor.execute(
                """
                UPDATE benchmark_jobs
                SET status='Running', worker_id=%s, started_at=%s, updated_at=%s
                WHERE job_id=%s AND status='Queued'
                """,
                (worker_id, stamp, stamp, job["job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            job.update(status=RUNNING, worker_id=worker_id, started_at=stamp, updated_at=stamp)
            return job


def set_total_runs(job_id: str, total: int) -> None:
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE benchmark_jobs SET total_runs=%s, updated_at=%s WHERE job_id=%s",
                (total, utc_now(), job_id),
            )


def set_completed_runs(job_id: str, completed: int) -> None:
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE benchmark_jobs SET completed_runs=%s, updated_at=%s WHERE job_id=%s",
                (completed, utc_now(), job_id),
            )


def request_cancel(job_id: str) -> bool:
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE benchmark_jobs
                SET cancel_requested=TRUE,
                    finished_at=IF(status='Queued',%s,finished_at),
                    status=IF(status='Queued','Cancelled',status),
                    updated_at=%s
                WHERE job_id=%s AND status IN ('Queued','Running')
                """,
                (utc_now(), utc_now(), job_id),
            )
            changed = cursor.rowcount > 0
    if changed:
        append_event(job_id, "停止请求已记录；若正在执行，将在当前单轮结束后停止", "warning")
    return changed


def is_cancel_requested(job_id: str) -> bool:
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT cancel_requested FROM benchmark_jobs WHERE job_id=%s", (job_id,)
            )
            row = cursor.fetchone()
            return bool(row and row["cancel_requested"])


def finish_job(
    job_id: str,
    *,
    status: str,
    suite_id: str | None = None,
    results_path: str | None = None,
    error: str = "",
) -> None:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"非法终态：{status}")
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE benchmark_jobs
                SET status=%s, suite_id=%s, results_path=%s, error=%s,
                    finished_at=%s, updated_at=%s
                WHERE job_id=%s
                """,
                (status, suite_id, results_path, error, utc_now(), utc_now(), job_id),
            )


@contextmanager
def exclusive_worker_lock() -> Iterator[str]:
    """全局只允许一个 Worker 在跑，拿不到锁就拒绝启动。

    这不是为了将来好扩展才留的余地，恰恰相反：NewAPI 后台用量按时间窗匹配，
    并发跑批必然张冠李戴（CLAUDE.md 七-5），所以「同时只有一个 Worker」
    是这套系统当前的正确性前提，不是运维约定。

    锁本身也是 `fail_running_jobs()` 成立的依据——持锁期间再没有别人在跑，
    所以此刻还挂着 Running 的任务一定是上次异常退出留下的。
    没有这把锁的话，第二个 Worker 一启动就会把第一个正在跑的任务
    收拢成 Failed，而它其实还在好好地往 JSONL 里写。

    用 MySQL 会话级 GET_LOCK：连接断了锁自动释放，Worker 被 kill -9
    也不会留下需要人工清理的锁。
    """

    connection = open_connection()
    try:
        with connection.cursor() as cursor:
            # 第二个参数是等待秒数；0 = 立刻返回，不排队。
            cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (WORKER_LOCK_NAME,))
            row = cursor.fetchone()
        if not row or row["acquired"] != 1:
            raise WorkerAlreadyRunning(
                "已经有另一个 Worker 在跑（MySQL 锁 "
                f"{WORKER_LOCK_NAME} 被占用）。"
                "串行是刻意的：并发跑批会让 NewAPI 用量按时间窗张冠李戴。"
            )
        yield WORKER_LOCK_NAME
    except pymysql.MySQLError as exc:
        raise DatabaseError(f"获取 Worker 锁失败：{exc}") from exc
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (WORKER_LOCK_NAME,))
        except pymysql.MySQLError:
            pass  # 连接已经断了，锁本来就随之释放
        connection.close()


def fail_running_jobs(reason: str) -> int:
    """收拢上次异常退出留下的 Running 任务。

    **必须在持有 `exclusive_worker_lock()` 时调用。** 它按 status 全表更新，
    只有在「确定没有别的 Worker 在跑」的前提下才是对的。
    """

    stamp = utc_now()
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE benchmark_jobs
                SET status='Failed', error=%s, finished_at=%s, updated_at=%s
                WHERE status='Running'
                """,
                (reason, stamp, stamp),
            )
            return cursor.rowcount
