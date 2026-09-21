from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pymysql


ENV_FILE = "infra/.env"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


class DatabaseError(RuntimeError):
    pass


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """读 infra/.env。让命令行直接可用，不用每次手动 export。"""
    target = path or project_root() / ENV_FILE
    if not target.is_file():
        return {}
    values: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 3307
    user: str = "bench"
    password: str = field(default="", repr=False)
    database: str = "benchmark"

    @classmethod
    def load(cls) -> "DatabaseConfig":
        # 新版统一 Compose 使用根目录 .env；infra/.env 继续兼容旧的拆分部署。
        values = {
            **load_env_file(project_root() / ENV_FILE),
            **load_env_file(project_root() / ".env"),
            **os.environ,
        }
        try:
            port = int(values.get("BENCH_DB_PORT", 3307))
        except (TypeError, ValueError) as exc:
            raise DatabaseError(f"BENCH_DB_PORT 不是整数：{values.get('BENCH_DB_PORT')!r}") from exc
        password = values.get("BENCH_DB_PASSWORD") or values.get("MYSQL_PASSWORD", "")
        if not password:
            raise DatabaseError(
                f"没有数据库密码：确认 .env 或 {ENV_FILE} 存在，"
                "或设 BENCH_DB_PASSWORD 环境变量"
            )
        return cls(
            host=values.get("BENCH_DB_HOST", "127.0.0.1"),
            port=port,
            user=values.get("BENCH_DB_USER") or values.get("MYSQL_USER", "bench"),
            password=password,
            database=values.get("BENCH_DB_NAME") or values.get("MYSQL_DATABASE", "benchmark"),
        )


def open_connection(
    config: DatabaseConfig | None = None,
) -> pymysql.connections.Connection:
    """建一条裸连接，调用方自己负责提交和关闭。

    多数地方该用 `connect()`。只有需要跨多条语句持有会话状态的场景
    （比如 Worker 的 GET_LOCK 咨询锁）才直接用这个——那种锁绑在会话上，
    连接一关就没了。
    """
    settings = config or DatabaseConfig.load()
    try:
        return pymysql.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            database=settings.database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.MySQLError as exc:
        raise DatabaseError(
            f"连不上 {settings.host}:{settings.port}/{settings.database}："
            f"{exc}（容器起来了吗：docker compose ps）"
        ) from exc


@contextmanager
def connect(config: DatabaseConfig | None = None) -> Iterator[pymysql.connections.Connection]:
    connection = open_connection(config)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def to_utc(value: Any) -> datetime | None:
    """ISO 字符串 → UTC naive datetime。

    库里一律存 UTC。本机 WSL 比 Windows 慢 8 秒、NewAPI 还算出过负的 use_time，
    再让 MySQL 掺和一层时区转换，时间窗匹配就彻底没法查了。
    """
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value.strip():
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp.astimezone(timezone.utc).replace(tzinfo=None)


def to_millis(seconds: Any) -> int | None:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    return int(round(seconds * 1000))
