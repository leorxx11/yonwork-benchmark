"""断言五层：完成性 → 产物 → 日志 → 内容 → 成本与性能。

模型输出不能直接断言，所以只断言工具调用、产物这些**不变量**，
内容层只做弱断言（禁止词、期望关键词、长度下限、JSON 可解析）。

判定全部在这里，驱动层不得掺和（CLAUDE.md 二-3）。
"""

from .base import Evaluation, evaluate
from .completion import check_completion
from .artifacts import check_artifacts
from .logs import check_logs
from .content import check_content
from .cost import check_cost

__all__ = [
    "Evaluation",
    "evaluate",
    "check_completion",
    "check_artifacts",
    "check_logs",
    "check_content",
    "check_cost",
]
