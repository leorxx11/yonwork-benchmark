from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import load_env_file, project_root
from .models import JsonObject
from .transport import TransportError, build_opener


CREDENTIALS_FILE = "newapi/credentials.env"
LOG_PATH = "/api/log/self"

# NewAPI 的日志类型。2=消费，4=错误；其余（1=充值等）跟基准测试无关。
TYPE_CONSUME = 2
TYPE_ERROR = 4

# 时间窗余量。端上和 NewAPI 都跑在 WSL 这一侧，用的是同一个时钟，
# 所以不用像和 Windows 对时那样留大余量；15s 够覆盖落库延迟。
MATCH_SLACK_SECONDS = 15


class NewApiError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NewApiConfig:
    base_url: str = "http://127.0.0.1:3000"
    token: str = field(default="", repr=False)
    user_id: str = "1"

    @classmethod
    def load(cls, path: Path | None = None) -> "NewApiConfig":
        # 宿主机沿用 credentials.env；容器通过 Compose 注入环境变量。
        root_values = load_env_file(project_root() / ".env")
        credential_values = load_env_file(path or project_root() / CREDENTIALS_FILE)
        values = (
            {**root_values, **credential_values, **os.environ}
            if path is not None
            else {**credential_values, **root_values, **os.environ}
        )
        token = values.get("NEWAPI_ACCESS_TOKEN", "")
        if not token:
            raise NewApiError(
                f"{CREDENTIALS_FILE} 里没有 NEWAPI_ACCESS_TOKEN；"
                "在 NewAPI「个人设置 → 生成系统访问令牌」里生成后填进去"
            )
        return cls(
            base_url=values.get("NEWAPI_BASE_URL", "http://127.0.0.1:3000").rstrip("/"),
            token=token,
            user_id=values.get("NEWAPI_USER_ID", "1"),
        )


def fetch_logs(
    config: NewApiConfig,
    start: datetime,
    end: datetime,
    *,
    token_name: str = "",
    page_size: int = 100,
    timeout: float = 15.0,
) -> list[JsonObject]:
    """拉一个时间窗内的日志，翻页直到取完。

    start/end 传 UTC；NewAPI 存的是 unix 秒，不受时区影响。
    """
    opener = build_opener()
    start_ts = int(start.replace(tzinfo=timezone.utc).timestamp()) - MATCH_SLACK_SECONDS
    end_ts = int(end.replace(tzinfo=timezone.utc).timestamp()) + MATCH_SLACK_SECONDS

    items: list[JsonObject] = []
    page = 1
    while True:
        url = (
            f"{config.base_url}{LOG_PATH}?p={page}&page_size={page_size}"
            f"&start_timestamp={start_ts}&end_timestamp={end_ts}"
        )
        if token_name:
            url += f"&token_name={urllib.parse.quote(token_name)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {config.token}",
                "New-Api-User": config.user_id,
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read())
        except Exception as exc:  # noqa: BLE001 —— 网络/解析问题统一归到一类
            raise TransportError(f"NewAPI {LOG_PATH} 请求失败：{exc}") from exc

        if not payload.get("success"):
            raise NewApiError(f"NewAPI 查询失败：{payload.get('message')}")
        data = payload.get("data") or {}
        batch = [item for item in (data.get("items") or []) if isinstance(item, dict)]
        items.extend(batch)
        if not batch or len(items) >= int(data.get("total") or 0):
            break
        page += 1
    return items


@dataclass(frozen=True, slots=True)
class BackendSample:
    benchmark_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    model: str | None
    api_calls: int
    error_calls: int
    use_time: int | None
    sampled_at: datetime
    matched_by: str
    raw: JsonObject


def match_logs(
    logs: list[JsonObject], runs: list[dict[str, Any]]
) -> tuple[list[BackendSample], list[JsonObject]]:
    """把后台日志落到具体某一轮上。

    只有时间窗可用：NewAPI 不知道 runId，YonWork 也没把它透给上游。
    落在窗内就归这一轮；窗重叠时归给结束时间最近的那轮。
    一条都对不上的日志会原样返回，**不要悄悄丢掉**——
    那可能正是「有人在跑批期间手动点了对话」这类污染的证据。
    """
    windows = [
        (
            run["benchmark_id"],
            run["started_at"] - timedelta(seconds=MATCH_SLACK_SECONDS),
            run["ended_at"] + timedelta(seconds=MATCH_SLACK_SECONDS),
            run["ended_at"],
        )
        for run in runs
        if run.get("started_at") and run.get("ended_at")
    ]

    buckets: dict[str, list[JsonObject]] = {}
    unmatched: list[JsonObject] = []
    for log in logs:
        if log.get("type") not in (TYPE_CONSUME, TYPE_ERROR):
            continue
        created = log.get("created_at")
        if not isinstance(created, (int, float)):
            unmatched.append(log)
            continue
        stamp = datetime.fromtimestamp(created, tz=timezone.utc).replace(tzinfo=None)
        candidates = [item for item in windows if item[1] <= stamp <= item[2]]
        if not candidates:
            unmatched.append(log)
            continue
        best = min(candidates, key=lambda item: abs((item[3] - stamp).total_seconds()))
        buckets.setdefault(best[0], []).append(log)

    samples: list[BackendSample] = []
    for benchmark_id, rows in buckets.items():
        consume = [row for row in rows if row.get("type") == TYPE_CONSUME]
        errors = [row for row in rows if row.get("type") == TYPE_ERROR]
        input_tokens = sum(int(row.get("prompt_tokens") or 0) for row in consume)
        output_tokens = sum(int(row.get("completion_tokens") or 0) for row in consume)
        use_times = [row.get("use_time") for row in consume if row.get("use_time") is not None]
        latest = max(rows, key=lambda row: row.get("created_at") or 0)
        samples.append(
            BackendSample(
                benchmark_id=benchmark_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                model=next((row.get("model_name") for row in consume if row.get("model_name")), None),
                # 和旧的 newapi_stats.ps1 口径一致：消费 + 错误都算一次调用。
                api_calls=len(consume) + len(errors),
                error_calls=len(errors),
                use_time=sum(int(item) for item in use_times) if use_times else None,
                sampled_at=datetime.fromtimestamp(
                    latest.get("created_at") or 0, tz=timezone.utc
                ).replace(tzinfo=None),
                matched_by="time-window",
                raw={"rows": rows},
            )
        )
    return samples, unmatched
