from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .discovery import candidate_runtime_files
from .models import JsonObject, UsageSample


SOURCE = "session-jsonl"
ENV_SESSIONS_DIR = "YONWORK_SESSIONS_DIR"

SESSIONS_GLOB = "profiles/*/userData/runtime/openclaw/agents/{agent}/sessions"

# 用户消息上的 idempotencyKey 是 "<BenchmarkId>:user"，
# 所以这条路是**精确匹配**，不像端上用量和 NewAPI 后台那样只能靠时间窗。
USER_KEY_SUFFIX = ":user"


class SessionLogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SessionUsage:
    benchmark_id: str
    session_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    model: str | None
    provider: str | None
    assistant_turns: int
    timestamp: str | None
    source_path: str
    # SSE 里**看不到**工具调用：实测 /api/chat/send 的流只有 text 块，
    # 而同一轮的会话 JSONL 里有 toolCall / toolResult。
    # 不从这儿补的话 `turn.tool_calls` 对 YonWork 恒为 0，
    # 「这个 Case 必须用到工具」的断言会稳定误判成「产品没调工具」。
    tool_calls: tuple[str, ...] = ()
    tool_calls_complete: bool = False
    tool_calls_error: bool = False

    def as_sample(self) -> UsageSample:
        return UsageSample(
            source=SOURCE,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            model=self.model,
            provider=self.provider,
            session_id=self.session_id,
            timestamp=self.timestamp,
            match="idempotency-key",
        )


def find_sessions_dir(agent_id: str = "main", base_dir: Path | None = None) -> Path:
    override = os.environ.get(ENV_SESSIONS_DIR)
    if override:
        path = Path(override).expanduser()
        if not path.is_dir():
            raise SessionLogError(f"{ENV_SESSIONS_DIR} 指向的目录不存在：{path}")
        return path

    root = base_dir
    if root is None:
        candidates = candidate_runtime_files()
        if not candidates:
            raise SessionLogError(
                f"定位不到 YonWork 数据目录；用 {ENV_SESSIONS_DIR} 直接指定 sessions 目录"
            )
        root = candidates[0].parent

    matches = sorted(
        (path for path in root.glob(SESSIONS_GLOB.format(agent=agent_id)) if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise SessionLogError(f"{root} 下找不到 agent {agent_id} 的 sessions 目录")
    # 多 profile 时取最近写过的那个；真要指定就用环境变量。
    return matches[0]


def iter_session_files(sessions_dir: Path, since: datetime | None = None) -> list[Path]:
    """只要会话主文件。

    `.trajectory.jsonl` 是同一会话的完整轨迹（130KB 量级，里面没有 usage 汇总），
    跑批时逐个解析纯属浪费。
    """
    cutoff = since.timestamp() if since else None
    files = []
    for path in sessions_dir.glob("*.jsonl"):
        if path.name.endswith(".trajectory.jsonl"):
            continue
        if cutoff is not None and path.stat().st_mtime < cutoff:
            continue
        files.append(path)
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def _tool_names(message: dict) -> list[str]:
    """助手消息里的工具调用块。

    实测块长这样：`{"type": "toolCall", "id": "call_…", "name": "tool_call"}`。
    宽松匹配 `"tool" in type`，跟 client.extract_tool_names 一个口径——
    名字取不到就记 `unknown`，**不要丢掉这次调用**，因为断言关心的是次数。
    """
    content = message.get("content")
    if not isinstance(content, list):
        return []
    names: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if isinstance(kind, str) and "tool" in kind.lower():
            name = block.get("toolName") or block.get("name")
            names.append(name if isinstance(name, str) and name else "unknown")
    return names


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def parse_session_file(path: Path) -> list[SessionUsage]:
    """一个会话文件里可能有多轮问答，按「用户消息 → 其后的助手消息」归属。

    我们每轮用全新 sessionKey，所以正常情况下一个文件只对一轮；
    但这里不假设这一点——万一哪天 sessionKey 复用了，这里能把它暴露出来
    而不是把两轮的 token 加到一起。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SessionLogError(f"会话文件读不了：{path}") from exc

    session_id = path.stem
    current: str | None = None
    buckets: dict[str, dict[str, object]] = {}
    malformed = False

    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(record, dict):
            malformed = True
            continue

        if record.get("type") == "session" and isinstance(record.get("id"), str):
            session_id = record["id"]
            continue
        if record.get("type") != "message":
            continue

        message = record.get("message")
        if not isinstance(message, dict):
            continue

        if message.get("role") == "user":
            key = message.get("idempotencyKey")
            if isinstance(key, str) and key.endswith(USER_KEY_SUFFIX):
                current = key[: -len(USER_KEY_SUFFIX)]
                buckets.setdefault(
                    current,
                    {
                        "input": 0, "output": 0, "total": 0,
                        "cache_read": 0, "cache_write": 0,
                        "model": None, "provider": None, "turns": 0, "ts": None,
                        "tools": [], "terminal": False,
                    },
                )
            else:
                current = None
            continue

        if message.get("role") != "assistant" or current is None:
            continue

        bucket = buckets[current]
        bucket["terminal"] = message.get("stopReason") in (
            "stop", "end_turn", "length", "max_tokens", "error", "aborted",
        )
        # 工具块和 usage 不在同一条消息上：带 toolCall 的那条助手消息也有 usage，
        # 但顺序不保证，所以先收工具再判 usage，别被 continue 跳过去。
        for name in _tool_names(message):
            bucket_tools = bucket["tools"]
            assert isinstance(bucket_tools, list)
            bucket_tools.append(name)

        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        bucket["input"] = _as_int(bucket["input"]) + _as_int(usage.get("input"))
        bucket["output"] = _as_int(bucket["output"]) + _as_int(usage.get("output"))
        bucket["total"] = _as_int(bucket["total"]) + _as_int(usage.get("totalTokens"))
        bucket["cache_read"] = _as_int(bucket["cache_read"]) + _as_int(usage.get("cacheRead"))
        bucket["cache_write"] = _as_int(bucket["cache_write"]) + _as_int(usage.get("cacheWrite"))
        bucket["turns"] = _as_int(bucket["turns"]) + 1
        bucket["model"] = message.get("model") or bucket["model"]
        bucket["provider"] = message.get("provider") or bucket["provider"]
        if isinstance(record.get("timestamp"), str):
            bucket["ts"] = record["timestamp"]

    return [
        SessionUsage(
            benchmark_id=benchmark_id,
            session_id=session_id,
            input_tokens=_as_int(bucket["input"]),
            output_tokens=_as_int(bucket["output"]),
            total_tokens=_as_int(bucket["total"]),
            cache_read_tokens=_as_int(bucket["cache_read"]),
            cache_write_tokens=_as_int(bucket["cache_write"]),
            model=bucket["model"] if isinstance(bucket["model"], str) else None,
            provider=bucket["provider"] if isinstance(bucket["provider"], str) else None,
            assistant_turns=_as_int(bucket["turns"]),
            tool_calls=tuple(bucket["tools"]) if isinstance(bucket["tools"], list) else (),
            tool_calls_complete=bool(bucket["terminal"]) and not malformed,
            tool_calls_error=malformed,
            timestamp=bucket["ts"] if isinstance(bucket["ts"], str) else None,
            source_path=str(path),
        )
        for benchmark_id, bucket in buckets.items()
        if _as_int(bucket["turns"]) > 0
    ]


def collect(
    benchmark_ids: set[str] | None = None,
    *,
    agent_id: str = "main",
    sessions_dir: Path | None = None,
    since: datetime | None = None,
) -> dict[str, SessionUsage]:
    """扫会话目录，返回 BenchmarkId → 用量。

    `since` 用来按文件 mtime 裁剪扫描范围；跑批时传本轮开始时间就够。
    """
    directory = sessions_dir or find_sessions_dir(agent_id)
    found: dict[str, SessionUsage] = {}
    for path in iter_session_files(directory, since):
        for usage in parse_session_file(path):
            if benchmark_ids is not None and usage.benchmark_id not in benchmark_ids:
                continue
            found[usage.benchmark_id] = usage
            if benchmark_ids is not None and len(found) == len(benchmark_ids):
                return found
    return found


def collect_one(
    benchmark_id: str,
    *,
    agent_id: str = "main",
    sessions_dir: Path | None = None,
    started_at: datetime | None = None,
) -> SessionUsage | None:
    """跑批时给单轮用。文件刚写完，按开始时间前推一点裁剪扫描范围。"""
    since = started_at - timedelta(minutes=5) if started_at else None
    return collect({benchmark_id}, agent_id=agent_id, sessions_dir=sessions_dir, since=since).get(
        benchmark_id
    )


def to_json(usage: SessionUsage) -> JsonObject:
    return {
        "benchmarkId": usage.benchmark_id,
        "sessionId": usage.session_id,
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "total": usage.total_tokens,
        "cacheRead": usage.cache_read_tokens,
        "cacheWrite": usage.cache_write_tokens,
        "model": usage.model,
        "provider": usage.provider,
        "assistantTurns": usage.assistant_turns,
        "path": usage.source_path,
    }
