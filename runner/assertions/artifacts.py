from __future__ import annotations

from ..models import Check, ChatTurn, Layer, Verdict


def check_artifacts(turn: ChatTurn | None) -> list[Check]:
    """第二层：这一轮留下的产物能不能对上号。

    runId 直接取 idempotencyKey，所以 runId == BenchmarkId 是个硬不变量；
    对不上就说明产物归属不可信，这批数字没法用（Invalid），而不是产品答错了。
    """

    if turn is None:
        return [Check.skipped(Layer.ARTIFACT, "run-id", "没有原材料")]

    checks: list[Check] = []

    if not turn.run_id:
        checks.append(
            Check(
                Layer.ARTIFACT,
                "run-id",
                Verdict.INVALID,
                "服务端没有回 runId，这一轮无法归属",
            )
        )
    elif turn.run_id != turn.benchmark_id:
        checks.append(
            Check(
                Layer.ARTIFACT,
                "run-id",
                Verdict.INVALID,
                f"runId({turn.run_id}) 与 BenchmarkId({turn.benchmark_id}) 不一致",
            )
        )
    else:
        checks.append(Check(Layer.ARTIFACT, "run-id", Verdict.PASS, turn.run_id))

    answer = (turn.answer or "").strip()
    if answer:
        checks.append(
            Check(Layer.ARTIFACT, "answer-present", Verdict.PASS, f"{len(answer)} 字")
        )
    else:
        checks.append(
            Check(Layer.ARTIFACT, "answer-present", Verdict.FAIL, "终止了但没有答案文本")
        )

    checks.append(
        Check(
            Layer.ARTIFACT,
            "tool-calls",
            Verdict.PASS,
            f"{len(turn.tool_calls)} 次：{'、'.join(turn.tool_calls) or '无'}",
        )
    )

    return checks
