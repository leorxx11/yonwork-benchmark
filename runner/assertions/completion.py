from __future__ import annotations

from ..models import Check, ChatTurn, Layer, Verdict


# stopReason 属于「正常收尾」的取值。其余取值都当作产品没跑完。
NORMAL_STOP_REASONS = frozenset({"stop", "end_turn", "endturn", "complete", "completed"})


def _failure_checks(failure: BaseException) -> list[Check]:
    # 延迟导入：断言层不该在导入期就依赖驱动层。
    from ..client import ChatError, ChatTimeout
    from ..transport import TransportError

    if isinstance(failure, ChatTimeout):
        return [
            Check(
                Layer.COMPLETION,
                "turn-completed",
                Verdict.TIMEOUT,
                f"本轮超时：{failure}",
            )
        ]
    if isinstance(failure, (ChatError, TransportError)):
        return [
            Check(
                Layer.COMPLETION,
                "turn-completed",
                Verdict.ERROR,
                f"驱动层失败：{failure}",
            )
        ]
    return [
        Check(
            Layer.COMPLETION,
            "turn-completed",
            Verdict.ERROR,
            f"未预期的异常 {type(failure).__name__}：{failure}",
        )
    ]


def check_completion(turn: ChatTurn | None, failure: BaseException | None) -> list[Check]:
    """第一层：这一轮到底跑完了没有。"""

    if failure is not None:
        return _failure_checks(failure)
    if turn is None:
        return [
            Check(Layer.COMPLETION, "turn-completed", Verdict.ERROR, "没有拿到任何原材料")
        ]

    checks: list[Check] = []

    status = turn.http_status
    if status is not None and status >= 500:
        checks.append(
            Check(Layer.COMPLETION, "http-status", Verdict.FAIL, f"服务端返回 HTTP {status}")
        )
    elif status is not None and status >= 400:
        # 4xx 基本是我们请求构造错了（例如漏了 idempotencyKey），记在自己头上。
        checks.append(
            Check(Layer.COMPLETION, "http-status", Verdict.ERROR, f"请求被拒：HTTP {status}")
        )
    else:
        checks.append(Check(Layer.COMPLETION, "http-status", Verdict.PASS, f"HTTP {status}"))

    if turn.stream_error is not None:
        checks.append(
            Check(
                Layer.COMPLETION,
                "stream-error",
                Verdict.FAIL,
                f"收到 chat.error：{turn.stream_error}",
            )
        )

    if turn.terminated_by is None:
        checks.append(
            Check(
                Layer.COMPLETION,
                "turn-completed",
                Verdict.FAIL,
                "流结束了却没有终止帧（既无 chat.complete 也无 final 消息）",
            )
        )
    else:
        checks.append(
            Check(Layer.COMPLETION, "turn-completed", Verdict.PASS, turn.terminated_by)
        )

    stop_reason = turn.stop_reason
    if stop_reason is None:
        checks.append(
            Check.skipped(Layer.COMPLETION, "stop-reason", "终止帧里没有 stopReason")
        )
    elif stop_reason.casefold() in NORMAL_STOP_REASONS:
        checks.append(Check(Layer.COMPLETION, "stop-reason", Verdict.PASS, stop_reason))
    else:
        checks.append(
            Check(
                Layer.COMPLETION,
                "stop-reason",
                Verdict.FAIL,
                f"非正常收尾：stopReason={stop_reason}",
            )
        )

    return checks
