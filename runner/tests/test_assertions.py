from __future__ import annotations

import unittest

from runner.assertions import evaluate
from runner.assertions.cost import SYSTEM_PROMPT_BASELINE_INPUT_TOKENS, check_cost
from runner.assertions.logs import check_logs
from runner.client import ChatError, ChatTimeout
from runner.models import (
    ChatTurn,
    Expectations,
    LogStats,
    UsageSample,
    Verdict,
    now_iso,
    worst,
)


def make_turn(**overrides: object) -> ChatTurn:
    defaults = dict(
        benchmark_id="bench-1",
        session_key="agent:main:bench-1",
        prompt="hi",
        started_at=now_iso(),
        ended_at=now_iso(),
        duration_seconds=3.0,
        run_id="bench-1",
        answer="PONG",
        terminated_by="chat.complete",
        stop_reason="stop",
        http_status=200,
    )
    defaults.update(overrides)
    return ChatTurn(**defaults)  # type: ignore[arg-type]


def verdict_of(name: str, evaluation) -> Verdict | None:
    for check in evaluation.checks:
        if check.name == name:
            return check.verdict
    raise AssertionError(f"没有名为 {name} 的断言：{[c.name for c in evaluation.checks]}")


class SeverityTests(unittest.TestCase):
    def test_invalid_beats_everything(self) -> None:
        self.assertEqual(
            Verdict.INVALID, worst([Verdict.PASS, Verdict.FAIL, Verdict.ERROR, Verdict.INVALID])
        )

    def test_error_beats_fail(self) -> None:
        """工具自己坏了不能记成产品缺陷。"""
        self.assertEqual(Verdict.ERROR, worst([Verdict.FAIL, Verdict.ERROR]))

    def test_empty_is_pass(self) -> None:
        self.assertEqual(Verdict.PASS, worst([]))


class CompletionTests(unittest.TestCase):
    def test_happy_path_passes(self) -> None:
        evaluation = evaluate(turn=make_turn(), expectations=Expectations())
        self.assertEqual(Verdict.PASS, evaluation.verdict)

    def test_timeout_is_not_error(self) -> None:
        evaluation = evaluate(
            turn=None, expectations=Expectations(), failure=ChatTimeout("超时了")
        )
        self.assertEqual(Verdict.TIMEOUT, evaluation.verdict)

    def test_driver_failure_is_error(self) -> None:
        evaluation = evaluate(
            turn=None, expectations=Expectations(), failure=ChatError("连不上")
        )
        self.assertEqual(Verdict.ERROR, evaluation.verdict)

    def test_missing_terminal_frame_is_fail(self) -> None:
        evaluation = evaluate(
            turn=make_turn(terminated_by=None, stop_reason=None), expectations=Expectations()
        )
        self.assertEqual(Verdict.FAIL, verdict_of("turn-completed", evaluation))

    def test_server_error_is_fail_and_client_error_is_error(self) -> None:
        self.assertEqual(
            Verdict.FAIL,
            verdict_of("http-status", evaluate(turn=make_turn(http_status=500), expectations=Expectations())),
        )
        self.assertEqual(
            Verdict.ERROR,
            verdict_of("http-status", evaluate(turn=make_turn(http_status=400), expectations=Expectations())),
        )


class ArtifactTests(unittest.TestCase):
    def test_run_id_mismatch_is_invalid(self) -> None:
        """runId 直接取 idempotencyKey；对不上就无法归属，数据作废。"""
        evaluation = evaluate(turn=make_turn(run_id="别的"), expectations=Expectations())
        self.assertEqual(Verdict.INVALID, verdict_of("run-id", evaluation))
        self.assertEqual(Verdict.INVALID, evaluation.verdict)

    def test_missing_answer_is_fail(self) -> None:
        evaluation = evaluate(turn=make_turn(answer=""), expectations=Expectations())
        self.assertEqual(Verdict.FAIL, verdict_of("answer-present", evaluation))


class LogTests(unittest.TestCase):
    def test_error_calls_above_zero_is_fail(self) -> None:
        checks = check_logs(LogStats(api_calls=3, error_calls=1, source="newapi"))
        self.assertEqual(Verdict.FAIL, checks[1].verdict)

    def test_zero_api_calls_is_invalid(self) -> None:
        checks = check_logs(LogStats(api_calls=0, error_calls=0, source="newapi"))
        self.assertEqual(Verdict.INVALID, checks[0].verdict)

    def test_missing_stats_are_skipped_not_passed(self) -> None:
        """采不到就是采不到，不能用 0 冒充「没出错」。"""
        checks = check_logs(LogStats())
        self.assertTrue(all(check.verdict is None for check in checks))


class ContentTests(unittest.TestCase):
    def test_forbidden_keyword_is_fail(self) -> None:
        evaluation = evaluate(
            turn=make_turn(answer="抱歉，我无法完成"),
            expectations=Expectations(forbid_keywords=("抱歉",)),
        )
        self.assertEqual(Verdict.FAIL, verdict_of("forbidden-keywords", evaluation))

    def test_expected_keyword_missing_is_fail(self) -> None:
        evaluation = evaluate(
            turn=make_turn(answer="没提到"),
            expectations=Expectations(expect_keywords=("凤仙郡",)),
        )
        self.assertEqual(Verdict.FAIL, verdict_of("expected-keywords", evaluation))

    def test_json_in_code_fence_is_parsable(self) -> None:
        evaluation = evaluate(
            turn=make_turn(answer='```json\n{"a": 1}\n```'),
            expectations=Expectations(json_parsable=True),
        )
        self.assertEqual(Verdict.PASS, verdict_of("json-parsable", evaluation))

    def test_min_length(self) -> None:
        evaluation = evaluate(
            turn=make_turn(answer="短"), expectations=Expectations(min_length=10)
        )
        self.assertEqual(Verdict.FAIL, verdict_of("min-length", evaluation))


class CostTests(unittest.TestCase):
    def test_duration_over_threshold_is_fail(self) -> None:
        evaluation = evaluate(
            turn=make_turn(duration_seconds=120.0),
            expectations=Expectations(max_seconds=60.0),
        )
        self.assertEqual(Verdict.FAIL, verdict_of("duration", evaluation))

    def test_input_token_baseline_is_not_a_jump(self) -> None:
        """每轮约 20k 系统提示词底噪是常态，不该报警。"""
        checks = check_cost(
            make_turn(),
            UsageSample(input_tokens=SYSTEM_PROMPT_BASELINE_INPUT_TOKENS, match="time-window"),
            Expectations(),
        )
        jump = next(check for check in checks if check.name == "input-token-jump")
        self.assertEqual(Verdict.PASS, jump.verdict)

    def test_input_token_jump_is_fail(self) -> None:
        checks = check_cost(
            make_turn(),
            UsageSample(input_tokens=SYSTEM_PROMPT_BASELINE_INPUT_TOKENS * 5, match="run-id"),
            Expectations(),
        )
        jump = next(check for check in checks if check.name == "input-token-jump")
        self.assertEqual(Verdict.FAIL, jump.verdict)

    def test_model_mismatch_is_invalid(self) -> None:
        """静默回落到默认模型时，测的根本不是目标模型，这批数字没意义。"""
        checks = check_cost(
            make_turn(requested_model="deepseek-flash"),
            UsageSample(model="deepseek-v4-flash", input_tokens=100, match="run-id"),
            Expectations(),
        )
        match = next(check for check in checks if check.name == "model-match")
        self.assertEqual(Verdict.INVALID, match.verdict)

    def test_model_match_passes(self) -> None:
        checks = check_cost(
            make_turn(requested_model="deepseek-flash"),
            UsageSample(model="deepseek-flash", input_tokens=100, match="run-id"),
            Expectations(),
        )
        match = next(check for check in checks if check.name == "model-match")
        self.assertEqual(Verdict.PASS, match.verdict)

    def test_no_usage_is_skipped(self) -> None:
        checks = check_cost(make_turn(), None, Expectations())
        self.assertTrue(any(check.name == "token-usage" and check.verdict is None for check in checks))


class AggregationTests(unittest.TestCase):
    def test_all_layers_run_even_after_a_failure(self) -> None:
        """第一条失败不截断，JSONL 里要留完整现场。"""
        evaluation = evaluate(
            turn=make_turn(answer="抱歉", stop_reason="error"),
            expectations=Expectations(forbid_keywords=("抱歉",), max_seconds=1.0),
            usage=UsageSample(input_tokens=10, total_tokens=20, match="run-id"),
            log_stats=LogStats(api_calls=1, error_calls=0, source="test"),
        )
        names = {check.name for check in evaluation.checks}
        self.assertLessEqual(
            {"turn-completed", "run-id", "api-calls", "forbidden-keywords", "duration"}, names
        )
        self.assertEqual(Verdict.FAIL, evaluation.verdict)
        self.assertIn("forbidden-keywords", evaluation.summary)


if __name__ == "__main__":
    unittest.main()
