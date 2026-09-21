from __future__ import annotations

from ..models import Check, ChatTurn, Expectations, Layer, Verdict


def check_artifacts(
    turn: ChatTurn | None, expectations: Expectations | None = None
) -> list[Check]:
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

    checks.append(_tool_calls_check(turn, expectations))

    return checks


def _tool_calls_check(turn: ChatTurn, expectations: Expectations | None) -> Check:
    """工具调用：不声明就只记录，声明了才判。

    以前这条恒 PASS——采了不判，跟 ErrorCalls 当初一模一样。
    后果是「工具用例」跑出 0 次工具调用照样全绿：探针 S5/S6 就是这么
    把两个普通短文本场景当成「工具场景没问题」的证据的（CLAUDE.md 七-2.5）。

    判不过时**要分清是谁的问题**（二-4）：
    - 工具是开着的，模型自己没调 → 产品的问题，Fail。
    - 我们把工具关了却跑了个要求用工具的 Case → 跑法就不对，这轮数据无意义，
      Invalid。**不能记 Fail**，那等于拿自己的配置错误去算产品的失败率。
    """
    count = len(turn.tool_calls)
    detail = f"{count} 次：{'、'.join(turn.tool_calls) or '无'}"
    wanted = expectations.min_tool_calls if expectations else None

    if wanted is None:
        return Check(Layer.ARTIFACT, "tool-calls", Verdict.PASS, detail)
    if count >= wanted:
        return Check(Layer.ARTIFACT, "tool-calls", Verdict.PASS, f"{detail}（要求 ≥{wanted}）")
    if turn.tools_enabled is False:
        return Check(
            Layer.ARTIFACT,
            "tool-calls",
            Verdict.INVALID,
            f"{detail}，但这一批把工具关了——要求 ≥{wanted} 的 Case 不该这么跑",
        )
    return Check(
        Layer.ARTIFACT,
        "tool-calls",
        Verdict.FAIL,
        f"{detail}，少于要求的 {wanted} 次",
    )
