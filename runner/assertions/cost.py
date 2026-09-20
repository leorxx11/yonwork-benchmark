from __future__ import annotations

from ..models import Check, ChatTurn, Expectations, Layer, UsageSample, Verdict


# 最小 prompt（`Reply with exactly: PONG`）实测 inputTokens = 20832，
# 也就是每轮约 20k 系统提示词底噪。成本断言的阈值都以它为基准。
SYSTEM_PROMPT_BASELINE_INPUT_TOKENS = 20832

# 超过底噪这么多倍就算跳变，需要人看一眼（多半是上下文没清干净或系统提示词涨了）。
INPUT_TOKEN_JUMP_FACTOR = 2.0

# 能精确对上某一轮的匹配方式。其余（时间窗）都要留痕。
EXACT_MATCHES = frozenset({"run-id", "idempotency-key"})


def check_cost(
    turn: ChatTurn | None,
    usage: UsageSample | None,
    expectations: Expectations,
) -> list[Check]:
    """第五层：耗时阈值 + token 异常跳变。"""

    checks: list[Check] = []

    if turn is None:
        checks.append(Check.skipped(Layer.COST, "duration", "没有原材料"))
    elif expectations.max_seconds is None:
        checks.append(
            Check.skipped(
                Layer.COST, "duration", f"未设阈值（实测 {turn.duration_seconds:g}s）"
            )
        )
    else:
        over = turn.duration_seconds > expectations.max_seconds
        checks.append(
            Check(
                Layer.COST,
                "duration",
                Verdict.FAIL if over else Verdict.PASS,
                f"{turn.duration_seconds:g}s / 阈值 {expectations.max_seconds:g}s",
            )
        )

    if usage is None:
        checks.append(Check.skipped(Layer.COST, "token-usage", "没有匹配到用量记录"))
        return checks

    requested = turn.requested_model if turn else None
    if requested is None:
        pass  # 没指定模型就没什么可比的，用的是智能体默认
    elif usage.model is None:
        checks.append(Check.skipped(Layer.COST, "model-match", "用量里没有 model"))
    elif usage.model != requested:
        # modelSelection 字段名写错时不会报错，只会静默回落到默认模型。
        # 那样测出来的根本不是目标模型，这批数字没有意义。
        checks.append(
            Check(
                Layer.COST,
                "model-match",
                Verdict.INVALID,
                f"请求的是 {requested}，实际跑的是 {usage.model}",
            )
        )
    else:
        checks.append(Check(Layer.COST, "model-match", Verdict.PASS, requested))

    if usage.match not in EXACT_MATCHES:
        # 时间窗匹配在并发跑批时可能张冠李戴，留痕提醒。
        # 会话 JSONL 走 idempotencyKey，是精确的，不用提醒。
        checks.append(
            Check.skipped(Layer.COST, "usage-match", f"匹配方式：{usage.match}（非精确）")
        )

    input_tokens = usage.input_tokens
    if input_tokens is None:
        checks.append(Check.skipped(Layer.COST, "input-token-jump", "用量里没有 inputTokens"))
    else:
        ceiling = SYSTEM_PROMPT_BASELINE_INPUT_TOKENS * INPUT_TOKEN_JUMP_FACTOR
        jumped = input_tokens > ceiling
        checks.append(
            Check(
                Layer.COST,
                "input-token-jump",
                Verdict.FAIL if jumped else Verdict.PASS,
                f"inputTokens={input_tokens} / 底噪 {SYSTEM_PROMPT_BASELINE_INPUT_TOKENS}"
                f" × {INPUT_TOKEN_JUMP_FACTOR:g}",
            )
        )

    total_tokens = usage.total_tokens
    if expectations.max_total_tokens is None:
        checks.append(
            Check.skipped(Layer.COST, "total-tokens", f"未设阈值（实测 {total_tokens}）")
        )
    elif total_tokens is None:
        checks.append(Check.skipped(Layer.COST, "total-tokens", "用量里没有 totalTokens"))
    else:
        over = total_tokens > expectations.max_total_tokens
        checks.append(
            Check(
                Layer.COST,
                "total-tokens",
                Verdict.FAIL if over else Verdict.PASS,
                f"{total_tokens} / 阈值 {expectations.max_total_tokens}",
            )
        )

    return checks
