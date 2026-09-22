from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..models import JsonObject, now_iso


# 归属状态。**「未归属」是一等结果，不是失败**——按时间猜归属正是这一层要消灭的东西。
ATTRIBUTED = "attributed"        # 原生关联头严格等于某个已注册轮次
LATE = "late"                    # 命中一个已收尾的轮次：仍归旧轮，绝不并入下一轮
UNATTRIBUTED = "unattributed"    # 没有可信标识，单独保留
REJECTED = "rejected"            # 凭据或路径不对，没有转发

# 终止原因（**代理视角**，不是产品视角）。
COMPLETED = "completed"
STREAM_TRUNCATED = "stream-truncated"    # 连接断了但没收到 [DONE]
UPSTREAM_ERROR = "upstream-error"        # 网关回了 4xx/5xx
CLIENT_DISCONNECTED = "client-disconnected"  # 产品自己走了，通常是取消
TRANSPORT_ERROR = "transport-error"      # 根本没连上网关
TIMEOUT = "timeout"
REFUSED = "refused"
# 只有 start 行没有 end 行：采集器被杀或进程崩了。
# 重放时必须能认出来，否则一次崩溃会伪装成一条正常记录。
INCOMPLETE = "incomplete"

OBSERVED = "observed"
MISSING = "missing"


@dataclass(slots=True)
class ModelRequestRecord:
    """一次**客户端到代理**的模型请求。

    三层关联结构里的中间那层：`run`（一轮 benchmark）→ `request`（这里）→
    `upstream attempt`（网关到上游）。最后一层我们看不见，所以
    `upstream_attempts` 恒为 None——**一次客户端请求不等于一次上游尝试**，
    把它当 1 填进去就是拿观测不到的东西冒充证据。

    只记元数据：没有提示词、没有回答正文、没有工具结果、没有鉴权头。
    请求头只留**名字**不留值，唯一的例外是关联头的值，而那本来就是我们自己发的
    BenchmarkId。
    """

    request_id: str          # 稳定 ID，重放的幂等键
    proxy_id: str
    sequence: int
    received_at: str
    path: str
    protocol: str = "openai-completions"
    run_id: str | None = None
    product: str | None = None
    attribution: str = UNATTRIBUTED
    attribution_source: str = ""     # 命中的请求头名；未归属时为空
    requested_model: str | None = None
    response_model: str | None = None   # 响应自称的模型，**不等于已证明的真实部署**
    stream: bool | None = None
    message_count: int | None = None
    header_names: tuple[str, ...] = ()
    # W3C 跟踪头的**值**。1.0.10 起 YonWork 不再发原生轮次头，只剩这个；
    # 将来靠产品 hook 提交 (traceId, spanId) → runId 的映射来补关联，
    # 而补关联的前提是**请求发生时就把它存下来**——事后补不回来。
    # 存值不违反「请求头只留名字」那条：关联头的值本来就是例外。
    traceparent: str | None = None
    http_status: int | None = None
    termination: str | None = None
    # 首个**有效**输出：带内容或工具参数的那一片。
    # 响应头、role-only 和空 delta 都不算——它们先到，算进去会把首字测得偏早。
    first_output_seconds: float | None = None
    duration_seconds: float | None = None
    ended_at: str | None = None
    usage: JsonObject | None = None
    usage_status: str = MISSING      # 缺就是缺，不补零
    sse_done: bool | None = None
    output_events: int = 0
    error_kind: str = ""
    error_detail: str = ""
    upstream_request_id: str | None = None   # NewAPI 的 x-oneapi-request-id
    upstream_attempts: int | None = None     # 网关内部重试不可见，保持未知
    record_status: str = "open"

    def to_json(self) -> JsonObject:
        payload = asdict(self)
        payload["header_names"] = list(self.header_names)
        return payload

    @classmethod
    def from_json(cls, value: JsonObject) -> "ModelRequestRecord":
        fields = {key: value.get(key) for key in cls.__slots__ if key in value}
        fields["header_names"] = tuple(fields.get("header_names") or ())
        return cls(**fields)


class LedgerError(RuntimeError):
    pass


class LedgerWriter:
    """一次请求两行：`open` 在收到请求时写，`closed` 在结束时写。

    **请求开始就落盘**，理由和 `report.append_jsonl` 一样：跑到一半崩了不能什么都不剩。
    两行合并靠 `request_id`，所以重复导入同一份文件不会重复计数。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, record: ModelRequestRecord) -> None:
        line = json.dumps(record.to_json(), ensure_ascii=False)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


def load_ledger(path: Path) -> list[ModelRequestRecord]:
    """重放：按 `request_id` 合并，`closed` 覆盖 `open`。

    只有 `open` 的记录会被标成 `incomplete`，**不会**被当成正常请求，
    也不会被悄悄丢掉——「采集器中断」和「本来就没有请求」必须分得开。
    """
    path = Path(path)
    if not path.is_file():
        raise LedgerError(f"找不到账本文件：{path}")
    merged: dict[str, ModelRequestRecord] = {}
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerError(f"{path} 第 {number} 行不是合法 JSON") from exc
            if not isinstance(value, dict) or not value.get("request_id"):
                raise LedgerError(f"{path} 第 {number} 行缺少 request_id")
            record = ModelRequestRecord.from_json(value)
            previous = merged.get(record.request_id)
            if previous is None or record.record_status == "closed":
                merged[record.request_id] = record
    for request_id, record in merged.items():
        if record.record_status != "closed":
            merged[request_id] = replace(
                record, termination=INCOMPLETE, ended_at=None, record_status="incomplete"
            )
    return sorted(merged.values(), key=lambda item: (item.proxy_id, item.sequence))


def summarize(records: list[ModelRequestRecord]) -> JsonObject:
    """给报告层的原材料汇总。**不判定**，只把「采到了什么」和「哪些不确定」并列摆出来。"""
    counted = [record for record in records if record.attribution in (ATTRIBUTED, LATE)]
    with_usage = [record for record in counted if record.usage_status == OBSERVED]
    return {
        "requests": len(records),
        "attributed": sum(1 for record in records if record.attribution == ATTRIBUTED),
        "late": sum(1 for record in records if record.attribution == LATE),
        "unattributed": sum(1 for record in records if record.attribution == UNATTRIBUTED),
        "rejected": sum(1 for record in records if record.attribution == REJECTED),
        "incomplete": sum(1 for record in records if record.termination == INCOMPLETE),
        "usage_observed": len(with_usage),
        "usage_missing": len(counted) - len(with_usage),
        # 只把**观测到的**加起来，并同时给出覆盖数；缺 usage 的请求不补零，
        # 所以这是「已观测小计」，不是整轮 token。
        "input_tokens_observed": _sum(with_usage, "prompt_tokens"),
        "output_tokens_observed": _sum(with_usage, "completion_tokens"),
        # 网关内部重试看不见，整轮的上游尝试数就只能是未知。
        "upstream_attempts": None,
    }


def _sum(records: list[ModelRequestRecord], key: str) -> int | None:
    values = [record.usage.get(key) for record in records if record.usage]
    numbers = [value for value in values if isinstance(value, int)]
    return sum(numbers) if numbers else None


def now() -> str:
    return now_iso()
