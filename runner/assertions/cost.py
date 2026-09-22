from __future__ import annotations

from ..models import Check, ChatTurn, Expectations, Layer, UsageSample, Verdict


# 能精确对上某一轮的匹配方式。其余（时间窗）都要留痕。
# session-id 是 WorkBuddy 那条：用量随本轮 CLI 输出一起回来，天然就是这一轮的。
EXACT_MATCHES = frozenset({"run-id", "idempotency-key", "session-id"})


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

    checks.extend(_provider_match(turn, usage))

    if usage.match not in EXACT_MATCHES:
        # 时间窗匹配在并发跑批时可能张冠李戴，留痕提醒。
        # 会话 JSONL 走 idempotencyKey，是精确的，不用提醒。
        checks.append(
            Check.skipped(Layer.COST, "usage-match", f"匹配方式：{usage.match}（非精确）")
        )

    # 这里曾经是「全局底噪 20832 × 2」。实测数据证明那个口径不成立，别改回去：
    #   · 同一句「你好！」，inputTokens 实测 5,514 ～ 16,238，3 倍差
    #   · 长文本用例同一轮，session-jsonl 记 30,082、NewAPI 记 228,354，7.6 倍差
    #     ——工具调用里反复重放文件，本来就该这么贵，却会被 41,664 的天花板判成 Fail
    # 一个常数既管不了 5.5k 的寒暄也管不了 228k 的长文本，跨来源比更没有意义。
    # 所以阈值按 Case 显式声明；没声明就只记录实测值，**不猜**。
    # 记录下来的这些值就是将来定分位数基线的原料。
    input_tokens = usage.input_tokens
    if input_tokens is None:
        checks.append(Check.skipped(Layer.COST, "input-tokens", "用量里没有 inputTokens"))
    elif expectations.max_input_tokens is None:
        checks.append(
            Check.skipped(
                Layer.COST,
                "input-tokens",
                f"未设阈值（实测 {input_tokens}，来源 {usage.source}）",
            )
        )
    else:
        over = input_tokens > expectations.max_input_tokens
        checks.append(
            Check(
                Layer.COST,
                "input-tokens",
                Verdict.FAIL if over else Verdict.PASS,
                f"{input_tokens} / 阈值 {expectations.max_input_tokens}"
                f"（来源 {usage.source}）",
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


# 产品的会话日志把自定义 provider id 截短成 `custom-<8 位>`（15 字符），
# 完整 id 是 43 字符。所以只能按前缀比，但**必须卡住长度**——
# 光一个 `custom-` 前缀会匹配上所有自定义 provider，等于没查。
_MIN_PROVIDER_PREFIX = 12


def _provider_match(turn: ChatTurn | None, usage: UsageSample) -> list[Check]:
    """实际跑在哪个 provider 上。

    **为什么光有 model-match 不够**：同一个模型名可以由多个 provider 提供。
    2026-09-22 实测，官方默认和我们的采集代理都叫 `deepseek-flash`，
    所以「默认模型」那一列实际跑在代理上、model-match 全绿，整列 12 轮白跑。

    判 Invalid 而不是 Fail：跑错通路的数字**没有意义**，不是产品的失败（二-4）。
    """
    requested = turn.requested_provider if turn else None
    if requested is None:
        return []  # 没指定 provider（旧数据或别的产品），没什么可比的
    actual = usage.provider
    if not actual:
        return [Check.skipped(Layer.COST, "provider-match", "用量里没有 provider")]
    # 大小写不敏感：WorkBuddy 自报 `Deepseek-V4.1-Flash`，而 CLI 参数要小写的
    # `deepseek-v4.1-flash`——两者是同一个模型，按大小写判 Invalid 是误判，
    # 会把「跑对了」记成「数据无意义」（实测踩过）。
    folded_actual, folded_requested = actual.casefold(), requested.casefold()
    if folded_actual == folded_requested:
        return [Check(Layer.COST, "provider-match", Verdict.PASS, requested)]
    if len(actual) >= _MIN_PROVIDER_PREFIX and folded_requested.startswith(folded_actual):
        return [
            Check(Layer.COST, "provider-match", Verdict.PASS,
                  f"{actual}…（产品记的是截短 id，与 {requested} 前缀一致）")
        ]
    return [
        Check(Layer.COST, "provider-match", Verdict.INVALID,
              f"请求的是 provider {requested}，实际跑在 {actual}")
    ]
