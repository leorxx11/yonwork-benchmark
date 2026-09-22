from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runner.drivers import DriverError, DriverSpec, build_driver
from runner.drivers import workbuddy
from runner.drivers.workbuddy import WorkBuddyDriver
from runner.drivers.yonwork import YonWorkDriver
from runner import sessionlog
from runner.sessionlog import SessionLogError
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

    def test_cold_start_is_separable_from_model_time(self) -> None:
        """外层 wall time 含冷启动，直接拿它跟 YonWork 常驻服务比就是误比。

        实测外层 16.744s、内部 3.087s——13.6s 全是起进程。
        两个数都得落到 ChatTurn 上，报告才拆得开。
        """
        turn = driver_with(PROBE_STDOUT).run_turn(benchmark_id="b1", prompt="hi")
        self.assertEqual(2.088, turn.engine_seconds)  # 夹具里的 duration_ms
        self.assertIsNotNone(turn.duration_seconds)
        # 外层是真实测量的 wall time，不该被内部数字顶替
        self.assertNotEqual(turn.engine_seconds, turn.duration_seconds)

    def test_missing_inner_duration_stays_none_instead_of_zero(self) -> None:
        """缺了就留空。用 0 顶替会把冷启动算成满额，读成「这产品全是冷启动」。"""
        records = json.loads(PROBE_STDOUT)
        for record in records:
            record.pop("duration_ms", None)
        turn = driver_with(json.dumps(records)).run_turn(benchmark_id="b1", prompt="hi")
        self.assertIsNone(turn.engine_seconds)

    def test_yonwork_leaves_engine_time_empty(self) -> None:
        """常驻服务没有每轮起进程这回事，不能拿 duration 填进去冒充。"""
        turn = ChatTurn(
            benchmark_id="b1", session_key="s", prompt="p",
            started_at="2026-09-21T00:00:00Z", ended_at="2026-09-21T00:00:03Z",
            duration_seconds=3.0,
        )
        self.assertIsNone(turn.engine_seconds)

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

    def test_empty_output_reports_stderr_instead_of_swallowing_it(self) -> None:
        """只说「没有输出（退出码 0）」的话，CLI 写在 stderr 的原因就丢了。

        2026-09-21 开着工具跑撞到过这个：报错里什么线索都没有，
        只能手工复现一遍才知道为什么。
        """
        def run(_command, _env, _timeout):
            return SimpleNamespace(stdout="", stderr="tool registry unavailable", returncode=0)

        driver = WorkBuddyDriver(runner=run)
        driver._home = Path("/tmp/workbuddy")
        driver._config_dir = Path("/tmp/.workbuddy")
        with self.assertRaises(ChatError) as caught:
            driver.run_turn(benchmark_id="b1", prompt="hi")
        self.assertIn("tool registry unavailable", str(caught.exception))

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

    def test_tool_calls_come_from_top_level_function_call_records(self) -> None:
        """工具调用是顶层记录，不是 assistant 消息里的 content 块。

        2026-09-21 实测形状：
        `{"type": "function_call", "name": "Bash", "callId": "call_…"}`。
        只盯 content 块的话 tool_calls 恒为空，而 event_counts 里
        明明有 function_call:1——模型调了工具却记成没调。
        """
        records = json.loads(PROBE_STDOUT)
        records.insert(1, {
            "type": "function_call", "name": "Bash",
            "callId": "call_x", "arguments": '{"command": "ls -la"}',
        })
        turn = driver_with(json.dumps(records), allow_tools=True).run_turn(
            benchmark_id="b1", prompt="hi"
        )
        self.assertEqual(("Bash",), turn.tool_calls)
        self.assertEqual(1, turn.event_counts.get("function_call"))

    def test_permission_mode_only_opens_up_when_tools_are_explicitly_allowed(self) -> None:
        """bypassPermissions 是真放权：跑批期间模型能在本机执行任意命令。

        所以它**只跟显式的 allow_tools 走**，默认那条路必须还是 dontAsk。
        （dontAsk 的语义是「不问，直接拒」——headless 弹不了窗，
        开着工具还用它的话模型会回「Bash 工具被拒绝执行权限」，实测过。）
        """
        off = driver_with(PROBE_STDOUT)._command("b1", "hi")
        on = driver_with(PROBE_STDOUT, allow_tools=True)._command("b1", "hi")
        self.assertEqual("dontAsk", off[off.index("--permission-mode") + 1])
        self.assertEqual("bypassPermissions", on[on.index("--permission-mode") + 1])
        # 只能出现一次，否则以哪个为准取决于 CLI 的解析顺序
        self.assertEqual(1, on.count("--permission-mode"))

    def test_enabling_tools_raises_max_turns(self) -> None:
        """1 轮只够直接作答。开了工具就必然不够：调工具一轮、作答又一轮。

        2026-09-21 实测这个组合的症状极难查——CLI 以
        `Max turns (1) exceeded` 收场，stdout 空白，**退出码仍是 0**。
        """
        off = driver_with(PROBE_STDOUT)._command("b1", "hi")
        on = driver_with(PROBE_STDOUT, allow_tools=True)._command("b1", "hi")
        self.assertEqual("1", off[off.index("--max-turns") + 1])
        self.assertGreater(int(on[on.index("--max-turns") + 1]), 1)

    def test_wslenv_lists_every_variable_and_marks_the_paths(self) -> None:
        """WSLENV 漏掉一个变量，目标进程里就是 undefined，而且不报错。

        所以它是从环境表生成的，不是手写的——手写那份一定会跟新增变量脱节。
        """
        env = driver_with(PROBE_STDOUT)._environment()
        declared = {item.split("/")[0] for item in env["WSLENV"].split(":")}
        self.assertEqual(set(env) - {"WSLENV"}, declared)
        self.assertIn("CODEBUDDY_CONFIG_DIR/p", env["WSLENV"])
        self.assertIn("ELECTRON_RUN_AS_NODE", env["WSLENV"].split(":"))


class ToolCallVisibilityTests(unittest.TestCase):
    """YonWork 的 SSE 看不到工具调用，必须从会话 JSONL 补。

    2026-09-21 实测同一轮：SSE 的 content 块**全是 text**，
    而会话 JSONL 里有 `{"type":"toolCall"}` + `toolResult`。
    不补的话 `tool_calls` 恒为 0，「这个 Case 必须用到工具」的断言
    会稳定误判成「产品没调工具」——把观测盲区算成产品失败。
    """

    @staticmethod
    def _turn(tool_calls=()):
        return ChatTurn(
            "b1", "agent:main:b1", "p",
            "2026-09-21T00:00:00Z", "2026-09-21T00:00:03Z", 3.0,
            tool_calls=tool_calls,
        )

    def test_tool_calls_are_backfilled_from_the_session_log(self) -> None:
        found = SimpleNamespace(tool_calls=("read_dir",), tool_calls_complete=True, tool_calls_error=False)
        with patch("runner.drivers.yonwork.collect_one", return_value=found):
            turn = YonWorkDriver().enrich(self._turn())
        self.assertEqual(("read_dir",), turn.tool_calls)
        self.assertEqual("observed", turn.tool_calls_status)

    def test_positive_sse_evidence_survives_missing_session(self) -> None:
        with patch("runner.drivers.yonwork.collect_one", return_value=None):
            turn = YonWorkDriver().enrich(self._turn(("from_sse",)))
        self.assertEqual(("from_sse",), turn.tool_calls)
        self.assertEqual("unavailable", turn.tool_calls_status)

    def test_backfill_failure_records_observation_error(self) -> None:
        with patch(
            "runner.drivers.yonwork.collect_one",
            side_effect=SessionLogError("会话文件读不了"),
        ):
            turn = YonWorkDriver().enrich(self._turn())
        self.assertEqual((), turn.tool_calls)
        self.assertEqual("error", turn.tool_calls_status)
        self.assertIn("SessionLogError", turn.tool_calls_detail)

    def test_partial_session_cannot_confirm_zero_calls(self):
        found = SimpleNamespace(tool_calls=(), tool_calls_complete=False, tool_calls_error=False)
        with patch("runner.drivers.yonwork.collect_one", return_value=found):
            turn = YonWorkDriver().enrich(self._turn())
        self.assertEqual("unavailable", turn.tool_calls_status)

    def test_nothing_found_is_not_invented(self) -> None:
        with patch("runner.drivers.yonwork.collect_one", return_value=None):
            self.assertEqual((), YonWorkDriver().enrich(self._turn()).tool_calls)

    def test_session_log_parses_the_real_tool_call_shape(self) -> None:
        """实测的块形状：{"type": "toolCall", "id": "call_…", "name": "tool_call"}。"""
        self.assertEqual(
            ["tool_call"],
            sessionlog._tool_names(
                {"content": [{"type": "toolCall", "id": "call_x", "name": "tool_call"}]}
            ),
        )
        # 名字取不到也要记一次，断言关心的是次数
        self.assertEqual(
            ["unknown"], sessionlog._tool_names({"content": [{"type": "tool_use"}]})
        )
        self.assertEqual([], sessionlog._tool_names({"content": [{"type": "text"}]}))


class WorkBuddyPreflightTests(unittest.TestCase):
    """起不了 Windows 进程时，报错必须指向真正的原因。"""

    def test_container_worker_is_told_to_use_the_host_worker(self) -> None:
        # 容器里 /mnt/d 本来就不存在，所以先查安装目录的话，报出来的是
        # 「设 BENCH_WORKBUDDY_HOME」——把人引去设一个设了也没用的变量。
        with patch("runner.drivers.workbuddy._interop_available", return_value=False):
            with patch("runner.drivers.workbuddy.Path") as path:
                path.return_value.is_file.return_value = True  # /.dockerenv 存在
                with self.assertRaises(DriverError) as caught:
                    list(WorkBuddyDriver().preflight())
        message = str(caught.exception)
        self.assertIn("host_worker.sh", message)
        self.assertNotIn("BENCH_WORKBUDDY_HOME", message)

    def test_plain_linux_gets_the_interop_reason_not_the_container_one(self) -> None:
        with patch("runner.drivers.workbuddy._interop_available", return_value=False):
            with patch("runner.drivers.workbuddy.Path") as path:
                path.return_value.is_file.return_value = False  # 没有 /.dockerenv
                with self.assertRaises(DriverError) as caught:
                    list(WorkBuddyDriver().preflight())
        self.assertIn("WSL interop", str(caught.exception))
        self.assertNotIn("docker compose", str(caught.exception))

    def test_interop_probe_accepts_both_kernel_names(self) -> None:
        """新内核叫 WSLInterop-late，老的叫 WSLInterop，通配两个都要认。"""
        for name in ("WSLInterop", "WSLInterop-late"):
            with tempfile.TemporaryDirectory() as folder:
                registry = Path(folder)
                (registry / name).touch()
                with patch("runner.drivers.workbuddy.Path", return_value=registry):
                    self.assertTrue(workbuddy._interop_available(), name)

    def test_missing_interop_registry_is_not_an_exception(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "nope"
            with patch("runner.drivers.workbuddy.Path", return_value=missing):
                self.assertFalse(workbuddy._interop_available())


class WorkBuddySettingTests(unittest.TestCase):
    def test_env_file_is_read_when_the_variable_is_not_exported(self) -> None:
        """容器靠 Compose 注环境变量，宿主机 Worker 和 CLI 只有 .env。

        只读 os.environ 的话，.env 里写的值会被静默忽略，
        表现为「明明配了却还是去找默认路径」。
        """
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BENCH_WORKBUDDY_HOME", None)
            with patch(
                "runner.drivers.workbuddy.load_env_file",
                return_value={"BENCH_WORKBUDDY_HOME": "/mnt/e/WB"},
            ):
                self.assertEqual("/mnt/e/WB", workbuddy._setting("BENCH_WORKBUDDY_HOME"))

    def test_exported_variable_wins_over_env_file(self) -> None:
        with patch.dict(os.environ, {"BENCH_WORKBUDDY_HOME": "/mnt/f/WB"}):
            with patch(
                "runner.drivers.workbuddy.load_env_file",
                return_value={"BENCH_WORKBUDDY_HOME": "/mnt/e/WB"},
            ):
                self.assertEqual("/mnt/f/WB", workbuddy._setting("BENCH_WORKBUDDY_HOME"))


if __name__ == "__main__":
    unittest.main()
