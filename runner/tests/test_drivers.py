from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runner.drivers import DriverError, DriverSpec, build_driver
from runner.drivers.workbuddy import WorkBuddyDriver
from runner.drivers.yonwork import YonWorkDriver
from runner.client import ChatError, ChatTimeout
from runner.models import USAGE_SOURCES, ChatTurn, UsageSample, Verdict
from runner.newapi import NewApiConfig, NewApiError
from runner.transport import TransportError
from runner.assertions import evaluate
from runner.models import Expectations


# docs/workbuddy-probe-report.md 里那份**实测**输出，只删了 UUID 和时间戳。
# 数值一个没改——这份夹具的意义就在于它不是我编的。
PROBE_STDOUT = json.dumps(
    [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Reply with exactly: PONG"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "PONG"}],
            "providerData": {
                "requestModelId": "default",
                "model": "deepseek-v4.1-flash",
                "rawUsage": {
                    "prompt_tokens": 3548,
                    "completion_tokens": 2,
                    "total_tokens": 3550,
                    "completion_thinking_tokens": 0,
                    "credit": 0.1,
                },
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "PONG",
            "session_id": "__RUN_ID__",
            "duration_ms": 2088,
            "duration_api_ms": 2087,
            "num_turns": 2,
            "total_cost_usd": 0,
            "usage": {
                "input_tokens": 3548,
                "output_tokens": 2,
                "cache_creation_input_tokens": 3548,
                "cache_read_input_tokens": 0,
            },
            "permission_denials": [],
        },
    ],
    ensure_ascii=False,
)


def fake_process(stdout: str, returncode: int = 0):
    def run(_command, _env, _timeout):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    return run


def driver_with(stdout: str, returncode: int = 0, **kwargs) -> WorkBuddyDriver:
    driver = WorkBuddyDriver(runner=fake_process(stdout, returncode), **kwargs)
    driver._home = Path("/tmp/workbuddy")
    driver._config_dir = Path("/tmp/.workbuddy")
    return driver


class RegistryTests(unittest.TestCase):
    def test_unknown_product_names_the_alternatives(self) -> None:
        with self.assertRaises(DriverError) as caught:
            build_driver(DriverSpec(product="copilot"))
        self.assertIn("workbuddy", str(caught.exception))
        self.assertIn("yonwork", str(caught.exception))

    def test_both_products_build(self) -> None:
        self.assertIsInstance(build_driver(DriverSpec(product="yonwork")), YonWorkDriver)
        self.assertIsInstance(
            build_driver(DriverSpec(product="workbuddy")), WorkBuddyDriver
        )


class YonWorkDriverTests(unittest.TestCase):
    def test_backend_failure_preserves_other_sources_and_redacts_error_text(self):
        for error in (NewApiError("secret-token"), TransportError("secret-token")):
            with self.subTest(error=type(error).__name__):
                driver = YonWorkDriver(usage_settle_seconds=0)
                driver._uses_newapi = True
                turn = ChatTurn("b1", "b1", "hi", "", "", 1)
                with patch.object(driver, "_device_usage", return_value=None), \
                     patch.object(driver, "_session_usage", return_value=UsageSample(source="session-jsonl")), \
                     patch("runner.drivers.yonwork.NewApiConfig.load", return_value=NewApiConfig()), \
                     patch("runner.drivers.yonwork.collect_turn_logs", side_effect=error):
                    result = driver.collect_usage(turn)
                self.assertEqual(["session-jsonl"], [s.source for s in result.samples])
                self.assertEqual(1, len(result.notes))
                self.assertNotIn("secret-token", result.notes[0])

    def test_default_route_does_not_query_newapi(self):
        driver = YonWorkDriver()
        with patch("runner.drivers.yonwork.NewApiConfig.load") as config:
            self.assertIsNone(driver._backend_usage(ChatTurn("b1", "b1", "hi", "", "", 1)))
        config.assert_not_called()

    def test_session_key_is_lowercase_and_unique_per_round(self) -> None:
        driver = YonWorkDriver()
        first = driver.session_key("Bench-Case01-r1-abc")
        self.assertEqual(first, first.lower())
        self.assertNotEqual(first, driver.session_key("Bench-Case01-r2-abc"))

    def test_running_before_preflight_is_a_driver_error(self) -> None:
        """没做前置检查就发请求，应当明确报错，而不是 AttributeError。"""
        with self.assertRaises(DriverError):
            YonWorkDriver().run_turn(benchmark_id="b1", prompt="你好")


class WorkBuddyParsingTests(unittest.TestCase):
    """解析逻辑的单测。活链路已实测跑通，这里锁的是字段搬运不走样。"""

    def _turn(self, benchmark_id: str = "wb-case01-r1-abc"):
        stdout = PROBE_STDOUT.replace("__RUN_ID__", benchmark_id)
        driver = driver_with(stdout)
        turn = driver.run_turn(benchmark_id=benchmark_id, prompt="Reply with exactly: PONG")
        return driver, turn

    def test_session_id_equals_benchmark_id_so_run_id_assertion_covers_it(self) -> None:
        """报告成功判定第 4 条：session_id 必须等于自己生成的 run id。

        让 `--session-id` 就是 BenchmarkId，这一条就由现成的 run-id 断言覆盖，
        不必在驱动里另写一套判定（那会违反「断言不写在驱动层」）。
        """
        _, turn = self._turn()
        self.assertEqual(turn.benchmark_id, turn.run_id)
        self.assertEqual(turn.benchmark_id, turn.session_key)

    def test_answer_and_termination_come_from_the_result_record(self) -> None:
        _, turn = self._turn()
        self.assertEqual("PONG", turn.answer)
        self.assertEqual("result:success", turn.terminated_by)
        self.assertEqual("completed", turn.final_state)

    def test_usage_is_exact_and_carries_the_actual_model(self) -> None:
        driver, turn = self._turn()
        sample = driver.collect_usage(turn).samples[0]
        self.assertEqual("workbuddy-cli", sample.source)
        self.assertEqual("session-id", sample.match)
        self.assertEqual(3548, sample.input_tokens)
        self.assertEqual(2, sample.output_tokens)
        self.assertEqual(3550, sample.total_tokens)
        # 判模型要用服务端实际执行的那个，不能信命令行参数。
        self.assertEqual("deepseek-v4.1-flash", sample.model)

    def test_cost_is_not_read_from_total_cost_usd(self) -> None:
        """`total_cost_usd` 在这个构建里恒为 0，记下来就是假的成本数据。

        `rawUsage.credit` 是产品积分也不是美元，同样不能往成本列里塞。
        """
        driver, turn = self._turn()
        self.assertIsNone(driver.collect_usage(turn).samples[0].cost_usd)

    def test_usage_source_is_in_the_preference_list_before_newapi(self) -> None:
        """不在 USAGE_SOURCES 里的来源会被 RunRecord.usage 悄悄丢掉。"""
        self.assertIn("workbuddy-cli", USAGE_SOURCES)
        self.assertLess(
            USAGE_SOURCES.index("workbuddy-cli"), USAGE_SOURCES.index("newapi")
        )

    def test_default_model_alias_is_not_reported_as_a_silent_fallback(self) -> None:
        """`default -> deepseek-v4.1-flash` 是正常的别名解析，不是静默回落。

        要是把 "default" 记成请求模型，model-match 断言会把每一轮都判成
        Invalid——整批数据当场作废，而且看起来还很像真的有问题。
        """
        driver, turn = self._turn()
        self.assertIsNone(turn.requested_model)
        evaluation = evaluate(
            turn=turn,
            expectations=Expectations(),
            usage=driver.collect_usage(turn).samples[0],
        )
        self.assertEqual(Verdict.PASS, evaluation.verdict)

    def test_explicit_model_is_kept_so_a_real_fallback_still_trips(self) -> None:
        driver = driver_with(PROBE_STDOUT.replace("__RUN_ID__", "b1"), model_query="glm-5.3")
        turn = driver.run_turn(benchmark_id="b1", prompt="hi")
        self.assertEqual("glm-5.3", turn.requested_model)
        evaluation = evaluate(
            turn=turn,
            expectations=Expectations(),
            usage=driver.collect_usage(turn).samples[0],
        )
        # 请求 glm-5.3 却跑了 deepseek，这批数字没有意义。
        self.assertEqual(Verdict.INVALID, evaluation.verdict)

    def test_missing_result_record_is_a_driver_failure(self) -> None:
        """assistant 的 status=="completed" 不是终点，后面还有 result。"""
        partial = json.dumps(
            [{"type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "PONG"}]}]
        )
        with self.assertRaises(ChatError):
            driver_with(partial).run_turn(benchmark_id="b1", prompt="hi")

    def test_exit_code_zero_is_not_enough(self) -> None:
        """报告实测：无效模型时退出码同样是 0，所以不能拿它当判据。"""
        with self.assertRaises(ChatError):
            driver_with("", returncode=0).run_turn(benchmark_id="b1", prompt="hi")

    def test_timeout_is_a_timeout_not_an_error(self) -> None:
        """混进 Error 就把产品的超时记成工具自己的问题了。"""

        def explode(_command, _env, timeout):
            raise subprocess.TimeoutExpired(cmd="codebuddy", timeout=timeout)

        driver = WorkBuddyDriver(runner=explode, timeout_seconds=5)
        driver._home, driver._config_dir = Path("/tmp/a"), Path("/tmp/b")
        with self.assertRaises(ChatTimeout):
            driver.run_turn(benchmark_id="b1", prompt="hi")


class WorkBuddyCommandTests(unittest.TestCase):
    def test_every_round_gets_a_brand_new_session_and_no_persistence(self) -> None:
        driver = driver_with(PROBE_STDOUT)
        command = driver._command("bench-r1", "hi")
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--session-id", command)
        self.assertEqual("bench-r1", command[command.index("--session-id") + 1])
        # 复用会话的两个开关一个都不能出现。
        self.assertNotIn("--continue", command)
        self.assertNotIn("--resume", command)

    def test_tools_are_off_by_default(self) -> None:
        command = driver_with(PROBE_STDOUT)._command("b1", "hi")
        self.assertEqual("", command[command.index("--tools") + 1])
        self.assertNotIn("--tools", driver_with(PROBE_STDOUT, allow_tools=True)._command("b1", "hi"))

    def test_wslenv_lists_every_variable_and_marks_the_paths(self) -> None:
        """WSLENV 漏掉一个变量，目标进程里就是 undefined，而且不报错。

        所以它是从环境表生成的，不是手写的——手写那份一定会跟新增变量脱节。
        """
        env = driver_with(PROBE_STDOUT)._environment()
        declared = {item.split("/")[0] for item in env["WSLENV"].split(":")}
        self.assertEqual(set(env) - {"WSLENV"}, declared)
        self.assertIn("CODEBUDDY_CONFIG_DIR/p", env["WSLENV"])
        self.assertIn("ELECTRON_RUN_AS_NODE", env["WSLENV"].split(":"))


if __name__ == "__main__":
    unittest.main()
