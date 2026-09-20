from __future__ import annotations

from ..models import Check, Layer, LogStats, Verdict


def check_logs(stats: LogStats | None) -> list[Check]:
    """第三层：PAD 时代唯一活下来的那条缺失断言（CLAUDE.md 五-2、七-3）。

    ErrorCalls / APICalls 以前只采不判，数据再多也没用：
      - ErrorCalls > 0 → Fail（产品的问题）
      - APICalls == 0  → Invalid（请求根本没到 API，这轮数据无意义）
    采不到就记 skipped，**不要**用 0 冒充「没出错」。
    """

    if stats is None:
        return [Check.skipped(Layer.LOG, "api-calls", "没有采集到调用统计")]

    checks: list[Check] = []

    if stats.api_calls is None:
        checks.append(
            Check.skipped(Layer.LOG, "api-calls", f"未采集（来源 {stats.source}）")
        )
    elif stats.api_calls == 0:
        checks.append(
            Check(Layer.LOG, "api-calls", Verdict.INVALID, "APICalls == 0，请求没到 API")
        )
    else:
        checks.append(Check(Layer.LOG, "api-calls", Verdict.PASS, str(stats.api_calls)))

    if stats.error_calls is None:
        checks.append(
            Check.skipped(Layer.LOG, "error-calls", f"未采集（来源 {stats.source}）")
        )
    elif stats.error_calls > 0:
        checks.append(
            Check(Layer.LOG, "error-calls", Verdict.FAIL, f"ErrorCalls = {stats.error_calls}")
        )
    else:
        checks.append(Check(Layer.LOG, "error-calls", Verdict.PASS, "0"))

    return checks
