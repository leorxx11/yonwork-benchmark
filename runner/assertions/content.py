from __future__ import annotations

import json
import re

from ..models import Check, ChatTurn, Expectations, Layer, Verdict


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _json_payload(answer: str) -> str:
    """模型爱把 JSON 包在代码围栏里，先扒一层再判可解析。"""
    match = _FENCE.search(answer)
    return match.group(1).strip() if match else answer.strip()


def check_content(turn: ChatTurn | None, expectations: Expectations) -> list[Check]:
    """第四层：只做弱断言。

    模型输出不确定，任何「答得对不对」的强断言都会变成噪声，
    这里只判禁止词、期望关键词、长度下限、JSON 可解析这四样。
    """

    answer = (turn.answer or "").strip() if turn else ""
    if not answer:
        return [Check.skipped(Layer.CONTENT, "content", "没有答案文本，内容层跳过")]

    checks: list[Check] = []

    if expectations.forbid_keywords:
        hits = [word for word in expectations.forbid_keywords if word in answer]
        checks.append(
            Check(
                Layer.CONTENT,
                "forbidden-keywords",
                Verdict.FAIL if hits else Verdict.PASS,
                f"命中禁止词：{'、'.join(hits)}" if hits else "无命中",
            )
        )

    if expectations.expect_keywords:
        missing = [word for word in expectations.expect_keywords if word not in answer]
        checks.append(
            Check(
                Layer.CONTENT,
                "expected-keywords",
                Verdict.FAIL if missing else Verdict.PASS,
                f"缺少关键词：{'、'.join(missing)}" if missing else "全部命中",
            )
        )

    if expectations.min_length > 1:
        too_short = len(answer) < expectations.min_length
        checks.append(
            Check(
                Layer.CONTENT,
                "min-length",
                Verdict.FAIL if too_short else Verdict.PASS,
                f"{len(answer)} 字 / 下限 {expectations.min_length}",
            )
        )

    if expectations.json_parsable:
        payload = _json_payload(answer)
        try:
            json.loads(payload)
        except json.JSONDecodeError as exc:
            checks.append(
                Check(Layer.CONTENT, "json-parsable", Verdict.FAIL, f"不可解析：{exc}")
            )
        else:
            checks.append(Check(Layer.CONTENT, "json-parsable", Verdict.PASS, "可解析"))

    if not checks:
        checks.append(Check.skipped(Layer.CONTENT, "content", "该 Case 没配置内容断言"))
    return checks
