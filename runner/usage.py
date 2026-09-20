from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .discovery import HostEndpoint
from .models import ChatTurn, JsonObject, LogStats, UsageSample
from .transport import TransportError, request_json


USAGE_PATH = "/api/usage/recent-token-history"

# 端上写这条记录相对 SSE 结束有延迟，取样前先等一会儿。
DEFAULT_SETTLE_SECONDS = 2.0
# 时间窗匹配的余量（秒）。
MATCH_SLACK_SECONDS = 15.0


def fetch_recent_token_history(
    endpoint: HostEndpoint, timeout: float = 10.0
) -> list[JsonObject]:
    payload = request_json(endpoint.url(USAGE_PATH), token=endpoint.token, timeout=timeout)
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        rows = payload["data"]
    else:
        raise TransportError(f"{USAGE_PATH} 返回了预期外的结构：{type(payload).__name__}")
    return [row for row in rows if isinstance(row, dict)]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # 一律转成带时区，否则和端上的 UTC 时间戳一比就抛 TypeError。
    return stamp if stamp.tzinfo is not None else stamp.astimezone()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _sample(row: JsonObject, match: str) -> UsageSample:
    return UsageSample(
        input_tokens=_as_int(row.get("inputTokens")),
        output_tokens=_as_int(row.get("outputTokens")),
        total_tokens=_as_int(row.get("totalTokens")),
        cache_read_tokens=_as_int(row.get("cacheReadTokens")),
        cache_write_tokens=_as_int(row.get("cacheWriteTokens")),
        cost_usd=row.get("costUsd") if isinstance(row.get("costUsd"), (int, float)) else None,
        model=row.get("model") if isinstance(row.get("model"), str) else None,
        provider=row.get("provider") if isinstance(row.get("provider"), str) else None,
        session_id=row.get("sessionId") if isinstance(row.get("sessionId"), str) else None,
        timestamp=row.get("timestamp") if isinstance(row.get("timestamp"), str) else None,
        match=match,
    )


def match_usage(rows: list[JsonObject], turn: ChatTurn) -> UsageSample | None:
    """把一轮对话对上它的用量记录。

    这条端点目前只给 sessionId，不给 runId，所以主路径仍是时间窗匹配
    （产物关联靠 runId==BenchmarkId 解决，用量关联暂时还解决不了）。
    命中多条时取答案文本能对上的那条，其次取时间最近的那条。
    """
    started = _parse_time(turn.started_at)
    ended = _parse_time(turn.ended_at)
    if started is None or ended is None:
        return None

    low = started - timedelta(seconds=MATCH_SLACK_SECONDS)
    high = ended + timedelta(seconds=MATCH_SLACK_SECONDS)

    # runId 若哪天出现在这条端点上，优先用它（精确匹配，不受时间窗影响）。
    for row in rows:
        if row.get("runId") == turn.benchmark_id:
            return _sample(row, "run-id")

    candidates: list[tuple[datetime, JsonObject]] = []
    for row in rows:
        stamp = _parse_time(row.get("timestamp"))
        if stamp is None:
            continue
        if low <= stamp <= high:
            candidates.append((stamp, row))
    if not candidates:
        return None

    answer = (turn.answer or "").strip()
    if answer:
        for _, row in candidates:
            content = row.get("content")
            if isinstance(content, str) and content.strip() and content.strip() in answer:
                return _sample(row, "time-window+content")

    candidates.sort(key=lambda item: item[0])
    return _sample(candidates[-1][1], "time-window")


def log_stats_from_usage(sample: UsageSample | None, turn: ChatTurn | None) -> LogStats:
    """把端上用量折算成第三层断言要的 APICalls / ErrorCalls。

    ErrorCalls 端上拿不到，只能留 None（等 newapi_stats.ps1 对账那条待办补齐，
    见 CLAUDE.md 七-4）。用 0 冒充会把「没采到」说成「没出错」。
    """
    if sample is not None:
        return LogStats(api_calls=1, error_calls=None, source="recent-token-history")
    if turn is not None and turn.run_id:
        # 拿到了 runId 说明请求确实到了 API，只是用量还没落盘。
        return LogStats(api_calls=1, error_calls=None, source="sse:run-id")
    if turn is not None:
        return LogStats(api_calls=0, error_calls=None, source="sse:no-run-id")
    return LogStats()
