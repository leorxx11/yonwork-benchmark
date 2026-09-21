from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import load_env_file, project_root, to_utc
from .models import ChatTurn, JsonObject, UsageSample
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
    token_name: str = ""

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
            token_name=values.get("NEWAPI_TOKEN_NAME", ""),
        )


def fetch_logs(
    config: NewApiConfig,
    start: datetime,
    end: datetime,
    *,
    token_name: str = "",
    page_size: int = 100,
    timeout: float = 15.0,
    slack_seconds: int = MATCH_SLACK_SECONDS,
) -> list[JsonObject]:
    """拉一个时间窗内的日志，翻页直到取完。

    start/end 传 UTC；NewAPI 存的是 unix 秒，不受时区影响。
    """
    opener = build_opener()
    start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start
    end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
    start_ts = int(start.timestamp()) - slack_seconds
    end_ts = int(end.timestamp()) + slack_seconds

    items: list[JsonObject] = []
    page = 1
    expected_total: int | None = None
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

        if not isinstance(payload, dict):
            raise NewApiError("NewAPI 日志响应不是对象")
        if not payload.get("success"):
            raise NewApiError(f"NewAPI 查询失败：{payload.get('message')}")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise NewApiError("NewAPI 日志响应缺少 data.items，不能当作空日志")
        batch = data["items"]
        if any(not isinstance(item, dict) for item in batch):
            raise NewApiError("NewAPI 日志包含无效记录")
        total = data.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise NewApiError("NewAPI 日志响应缺少有效 total，无法确认分页完整")
        if expected_total is not None and total != expected_total:
            raise NewApiError("NewAPI 日志在翻页期间发生变化，无法确认完整快照")
        expected_total = total
        items.extend(batch)
        if not batch and len(items) < total:
            raise NewApiError("NewAPI 日志分页提前结束")
        if len(items) >= total:
            break
        page += 1
    return items


def collect_turn_logs(
    config: NewApiConfig,
    turn: ChatTurn,
    *,
    token_name: str = "",
    attempts: int = 3,
    settle_seconds: float = 1.0,
) -> UsageSample | None:
    """在判定前采一轮后台日志；严格使用本轮时间窗，不带事后对账的 ±15s。

    NewAPI 时间戳精度为秒。串行跑批且同一 token 没有其它流量是匹配前提。
    空查询不能证明请求未发生（默认路由/日志延迟/漏记），所以返回 None。
    即使已看到消费日志也继续短暂补采，避免漏掉稍后落库的重试错误。
    """
    start, end = to_utc(turn.started_at), to_utc(turn.ended_at)
    if start is None or end is None or end < start or not turn.requested_model:
        raise NewApiError("本轮缺少有效时间窗或请求模型，无法匹配后台日志")
    low = int(start.replace(tzinfo=timezone.utc).timestamp())
    high = int(end.replace(tzinfo=timezone.utc).timestamp())
    rows: list[JsonObject] = []
    for attempt in range(max(1, attempts)):
        if attempt and settle_seconds > 0:
            time.sleep(settle_seconds)
        # /api/log/self 会将 id 重编为查询结果序号，绝不能跨快照按 id 合并。
        # 每次完整取样替换上一次；保留同秒同内容的多条真实调用。
        rows = []
        for row in fetch_logs(config, start, end, token_name=token_name, slack_seconds=0):
            if row.get("type") not in (TYPE_CONSUME, TYPE_ERROR):
                continue
            stamp = row.get("created_at")
            if not isinstance(stamp, (int, float)):
                raise NewApiError("NewAPI 日志缺少 created_at")
            if not low <= stamp <= high or row.get("model_name") != turn.requested_model:
                continue
            if token_name and row.get("token_name") != token_name:
                continue
            rows.append(row)
    if not rows:
        return None
    # 已经收紧到本轮窗内，复用现有消费/错误计数口径。
    samples, _ = match_logs(rows, [{
        "benchmark_id": turn.benchmark_id, "started_at": start, "ended_at": end,
    }])
    sample = samples[0]
    return UsageSample(
        source="newapi", input_tokens=sample.input_tokens,
        output_tokens=sample.output_tokens, total_tokens=sample.total_tokens,
        model=sample.model, timestamp=sample.sampled_at.replace(tzinfo=timezone.utc).isoformat(),
        match="time-window+model", api_calls=sample.api_calls,
        error_calls=sample.error_calls,
        log_entries=tuple({key: row.get(key) for key in (
            "created_at", "type", "model_name", "prompt_tokens", "completion_tokens",
        )} for row in rows),
    )


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
