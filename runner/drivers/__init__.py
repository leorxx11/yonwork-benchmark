"""被测产品的驱动层。

CLAUDE.md 二-3：**驱动只发请求、收原材料、记时间窗；判定全部在 `assertions/`。**
任何把 `Verdict` 写进这个包的改动都要拦下来——那样判定就不可单测、不可 diff 了。
"""

from __future__ import annotations

from .base import Driver, DriverError, DriverSpec, UsageCollection
from .workbuddy import WorkBuddyDriver
from .yonwork import YonWorkDriver


#「四个模式」里的产品维度。模型维度在 DriverSpec.model_query 上，两者正交。
DRIVERS = {
    YonWorkDriver.product: YonWorkDriver,
    WorkBuddyDriver.product: WorkBuddyDriver,
}


def build_driver(spec: DriverSpec) -> Driver:
    """按产品名造一个驱动。Web、CLI、测试三个入口共用这一处。"""
    if spec.product == YonWorkDriver.product:
        return YonWorkDriver(
            agent_id=spec.agent_id,
            model_query=spec.model_query,
            timeout_seconds=spec.timeout_seconds,
            transcript_dir=spec.transcript_dir,
        )
    if spec.product == WorkBuddyDriver.product:
        # WorkBuddy 没有 agent 概念，隔离靠每轮全新的 --session-id。
        return WorkBuddyDriver(
            model_query=spec.model_query,
            timeout_seconds=spec.timeout_seconds,
            transcript_dir=spec.transcript_dir,
        )
    raise DriverError(
        f"没有名为 {spec.product!r} 的驱动；可选：{'、'.join(sorted(DRIVERS))}"
    )


__all__ = [
    "DRIVERS",
    "Driver",
    "DriverError",
    "DriverSpec",
    "UsageCollection",
    "WorkBuddyDriver",
    "YonWorkDriver",
    "build_driver",
]
