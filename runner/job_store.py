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
    plan_id            CHAR(32)     NULL,
    plan_position      INT          NOT NULL DEFAULT 0,
    plan_label         VARCHAR(128) NOT NULL DEFAULT '',
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
    allow_tools        BOOLEAN NOT NULL DEFAULT FALSE,
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
    KEY idx_job_status_created (status, created_at),
    KEY idx_job_plan (plan_id, plan_position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# 计划分组是后加的列，而 CREATE TABLE IF NOT EXISTS 对已存在的表什么也不做，
# 所以旧库必须补一次 ALTER。参照 ingest._widen_source 的按需迁移写法，
# 但**这里不吞异常**：列没补上的话后面每一条 INSERT 都会因为未知列而失败，
# 与其让人对着 "Unknown column 'plan_id'" 猜，不如在建表这一步就炸。
PLAN_COLUMNS = (
    ("plan_id", "ADD COLUMN plan_id CHAR(32) NULL AFTER job_id"),
    ("plan_position", "ADD COLUMN plan_position INT NOT NULL DEFAULT 0 AFTER plan_id"),
    ("plan_label", "ADD COLUMN plan_label VARCHAR(128) NOT NULL DEFAULT '' AFTER plan_position"),
    ("allow_tools", "ADD COLUMN allow_tools BOOLEAN NOT NULL DEFAULT FALSE AFTER export_xlsx"),
)

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
class PlanMode:
    """计划里的一个模式 = 一个（产品 × 模型）组合，跑出来就是一个批次。

    `label` 只用于展示。`model_query` 存的是 choiceId 这类不可读的值，
    报告页按 `model_mode` 分组时用的是模型显示名——两者别混。
    """

    product: str = "yonwork"
    model_query: str = ""
    label: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.product, self.model_query.strip())

    @property
    def title(self) -> str:
        return self.label or f"{self.product} / {self.model_query.strip() or '默认模型'}"


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
    allow_tools: bool = False


@dataclass(frozen=True, slots=True)
class NewPlan:
    """一次提交、多个模式。

    **同一个 `experiment_name` 是刻意的**：`suite_id` 就是它的哈希，
    所以这一组任务天然落进同一份报告，`/matrix/<suite_id>` 的
    Case × 模式对比表不用另写。计划本身只管「一起提交、一起看进度」。
    """

    experiment_name: str
    case_set_id: str
    modes: tuple[PlanMode, ...]
    case_catalog_path: str = "cases/catalog.yaml"
    agent_id: str = "main"
    timeout_seconds: float = 600.0
    limit_runs: int = 0
    collect_usage: bool = True
    export_xlsx: bool = True
    allow_tools: bool = False


def _ensure_plan_columns(cursor: Any) -> None:
    """补齐后加的列。名字叫 plan 是历史原因，实际是「这张表所有按需迁移的列」。"""
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'benchmark_jobs'"
    )
    present = {str(row["COLUMN_NAME"]).lower() for row in cursor.fetchall()}
    missing = [clause for column, clause in PLAN_COLUMNS if column not in present]
    if missing:
        cursor.execute(f"ALTER TABLE benchmark_jobs {', '.join(missing)}")
    cursor.execute(
        "SELECT INDEX_NAME FROM information_schema.STATISTICS"
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'benchmark_jobs'"
        " AND INDEX_NAME = 'idx_job_plan'"
    )
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE benchmark_jobs ADD INDEX idx_job_plan (plan_id, plan_position)"
        )


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
                _ensure_plan_columns(cursor)
        _SCHEMA_READY = True


def _validate(name: str, timeout_seconds: float, limit_runs: int) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("实验名称不能为空")
    if timeout_seconds <= 0:
        raise ValueError("单轮超时必须大于 0")
    if limit_runs < 0:
        raise ValueError("轮次上限不能小于 0")
    return cleaned


INSERT_JOB_SQL = """
INSERT INTO benchmark_jobs (
    job_id, plan_id, plan_position, plan_label, batch_id, experiment_name,
    case_catalog_path, case_set_id, product, model_query, agent_id,
    timeout_seconds, limit_runs, collect_usage, export_xlsx, allow_tools, status,
    created_at, updated_at
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


def _insert_job(
    cursor: Any,
    *,
    spec: NewJob,
    name: str,
    stamp: datetime,
    plan_id: str | None = None,
    position: int = 0,
    label: str = "",
) -> str:
    job_id = uuid.uuid4().hex
    cursor.execute(
        INSERT_JOB_SQL,
        (
            job_id,
            plan_id,
            position,
            label[:128],
            f"web-{stamp:%Y%m%d-%H%M%S}-{job_id[:8]}",
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
            spec.allow_tools,
            QUEUED,
            stamp,
            stamp,
        ),
    )
    cursor.execute(
        "INSERT INTO benchmark_job_events (job_id, level, message, created_at)"
        " VALUES (%s,%s,%s,%s)",
        (
            job_id,
            "info",
            "任务已进入队列" if plan_id is None else f"任务已进入队列（计划第 {position + 1} 个模式）",
            stamp,
        ),
    )
    return job_id


def create_job(spec: NewJob) -> dict[str, Any]:
    name = _validate(spec.experiment_name, spec.timeout_seconds, spec.limit_runs)
    ensure_schema()
    stamp = utc_now()
    with connect() as connection:
        with connection.cursor() as cursor:
            job_id = _insert_job(cursor, spec=spec, name=name, stamp=stamp)
    found = get_job(job_id)
    if found is None:  # pragma: no cover - 数据库刚插入却读不到，只能算基础设施故障
        raise RuntimeError(f"任务创建后无法读取：{job_id}")
    return found


def create_plan(spec: NewPlan) -> dict[str, Any]:
    """一次建出整组任务。**要么全建成，要么一条都不建。**

    半个计划比没有计划更糟：报告页会把它当成一次完整的横向对比，
    而实际上少了一个模式的那一列——缺列和「那个模式全挂了」长得一模一样。
    所以 N 条 INSERT 走同一个事务。
    """
    name = _validate(spec.experiment_name, spec.timeout_seconds, spec.limit_runs)
    if not spec.modes:
        raise ValueError("至少要选一个模式")
    seen: set[tuple[str, str]] = set()
    for mode in spec.modes:
        if mode.key in seen:
            # 同一个模式跑两遍会被报告页合并成一列（mode_summary 按 product+model
            # 分组），轮次凭空翻倍却看不出来——这正是本项目最怕的静默脏数据。
            raise ValueError(f"模式重复了：{mode.title}；同一个组合只能选一次")
        seen.add(mode.key)

    ensure_schema()
    plan_id = uuid.uuid4().hex
    stamp = utc_now()
    with connect() as connection:
        with connection.cursor() as cursor:
            for position, mode in enumerate(spec.modes):
                _insert_job(
                    cursor,
                    spec=NewJob(
                        experiment_name=name,
                        case_set_id=spec.case_set_id,
                        case_catalog_path=spec.case_catalog_path,
                        product=mode.product,
                        model_query=mode.model_query,
                        agent_id=spec.agent_id,
                        timeout_seconds=spec.timeout_seconds,
                        limit_runs=spec.limit_runs,
                        collect_usage=spec.collect_usage,
                        export_xlsx=spec.export_xlsx,
                        allow_tools=spec.allow_tools,
                    ),
                    name=name,
                    stamp=stamp,
                    plan_id=plan_id,
                    position=position,
                    label=mode.title,
                )
    return {"plan_id": plan_id, "experiment_name": name, "jobs": plan_jobs(plan_id)}


def plan_jobs(plan_id: str) -> list[dict[str, Any]]:
    """计划里的全部任务，按执行顺序。"""
    ensure_schema()
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM benchmark_jobs WHERE plan_id = %s"
                " ORDER BY plan_position, created_at",
                (plan_id,),
            )
            return list(cursor.fetchall())


def cancel_plan(plan_id: str) -> int:
    """停掉整组里还没跑完的模式；已经完成的不动。"""
    jobs = plan_jobs(plan_id)
    return sum(1 for job in jobs if request_cancel(str(job["job_id"])))


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
    """原子领取一条任务；MySQL 8 的 SKIP LOCKED 防止多个 Worker 重复执行。

    ⚠️ **`plan_position` 是排序里不能省的那一项。** 同一个计划的 N 条任务共用
    一个 `created_at`（刻意的：它们是一次提交），光按时间排就全是并列，
    MySQL 挑哪条随缘——页面上表现为模式乱序执行，进度看着像跳着走。
    """

    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM benchmark_jobs
                WHERE status = 'Queued' AND cancel_requested = FALSE
                ORDER BY created_at, plan_position, job_id
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


def active_job() -> dict[str, Any] | None:
    """当前在队列里或正在跑的那一条。

    Worker 被咨询锁强制串行，所以「活跃任务」最多一条有意义的——
    取最早排队的那条，它就是 Worker 正在处理或马上要处理的。
    页面顶栏靠它告诉用户「后台还在跑」，否则离开任务页就完全失去感知。

    排序和 `claim_next_job` 必须一致，否则顶栏指的是 A、Worker 领的是 B。
    属于某个计划时把 `plan_id` 一并带出来，顶栏就能直接跳到整组进度。
    """
    ensure_schema()
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT job_id, plan_id, plan_label, experiment_name, status,"
                " completed_runs, total_runs"
                " FROM benchmark_jobs WHERE status IN ('Queued','Running')"
                " ORDER BY created_at, plan_position, job_id LIMIT 1"
            )
            return cursor.fetchone()


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
    """Worker 和直接 CLI 跑批共用的排他锁，拿不到锁就拒绝执行。

    这不是为了将来好扩展才留的余地，恰恰相反：NewAPI 后台用量按时间窗匹配，
    并发跑批必然张冠李戴（CLAUDE.md 四-2），所以「同时只有一个 Worker」
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
                "已有 Worker 或 CLI 跑批持锁（MySQL 锁 "
                f"{WORKER_LOCK_NAME} 被占用）。"
                "请等待 CLI 结束，或先停止空闲 Worker 后再直接跑 CLI。"
                "并发跑批会让 NewAPI 用量按时间窗张冠李戴。"
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
