from __future__ import annotations

from dataclasses import dataclass

from ..models import (
    Check,
    ChatTurn,
    Expectations,
    LogStats,
    UsageSample,
    Verdict,
    worst,
)
from .artifacts import check_artifacts
from .completion import check_completion
from .content import check_content
from .cost import check_cost
from .logs import check_logs


@dataclass(frozen=True, slots=True)
class Evaluation:
    verdict: Verdict
    checks: tuple[Check, ...]

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(
            item for item in self.checks if item.verdict not in (None, Verdict.PASS)
        )

    @property
    def summary(self) -> str:
        return "；".join(f"{item.name}: {item.detail}" for item in self.failed)


def evaluate(
    *,
    turn: ChatTurn | None,
    expectations: Expectations,
    usage: UsageSample | None = None,
    log_stats: LogStats | None = None,
    failure: BaseException | None = None,
) -> Evaluation:
    """跑完五层并取最严重的结论。

    上层断言不成立时下层仍然会跑（例如超时了也照样记成本），
    这样 JSONL 里留下的是完整现场，而不是第一条失败就截断。
    """
    checks: list[Check] = []
    checks.extend(check_completion(turn, failure))
    checks.extend(check_artifacts(turn))
    checks.extend(check_logs(log_stats))
    checks.extend(check_content(turn, expectations))
    checks.extend(check_cost(turn, usage, expectations))

    verdicts = [item.verdict for item in checks if item.verdict is not None]
    return Evaluation(verdict=worst(verdicts), checks=tuple(checks))
