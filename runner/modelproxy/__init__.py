"""逐请求模型调用账本 + 采集代理。

`产品 → 采集代理 → NewAPI → 上游模型`。这一层的存在理由只有一个：
把 NewAPI 对账从**时间窗匹配**换成**精确关联**。入口和关联能力已于
2026-09-22 实测通过，见 `docs/model-entry-validation.md`。

它**不取代** NewAPI：代理看到的是客户端请求，NewAPI 看到的是上游转发尝试，
两层分开计数、不相加。加上这一层之后 NewAPI 从主要用量来源降级成独立交叉验证。

规矩同 `drivers/`：**只收原材料，不下判断**（CLAUDE.md 二-3）。
账本里没有 verdict，缺失一律标 missing 而不是 0。
"""

from .client import RemoteCollector, build_collector
from .config import CollectorConfig, CollectorConfigError
from .ledger import (
    ATTRIBUTED,
    INCOMPLETE,
    LATE,
    LedgerError,
    LedgerWriter,
    ModelRequestRecord,
    REJECTED,
    UNATTRIBUTED,
    load_ledger,
    summarize,
)
from .proxy import CORRELATION_HEADERS, CollectorProxy, ProxyError

__all__ = [
    "ATTRIBUTED",
    "CORRELATION_HEADERS",
    "CollectorConfig",
    "CollectorConfigError",
    "CollectorProxy",
    "RemoteCollector",
    "build_collector",
    "INCOMPLETE",
    "LATE",
    "LedgerError",
    "LedgerWriter",
    "ModelRequestRecord",
    "ProxyError",
    "REJECTED",
    "UNATTRIBUTED",
    "load_ledger",
    "summarize",
]
