from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

import yonwork_usage as collector


DEFAULT_WORKSPACE = r"C:\Users\Administrator\AppData\Roaming\yonwork\profiles\47jm5\userData\workspaces\default"


def make_input(
    run_id: str,
    *,
    session_key: str = "agent:main:session-test",
    agent_id: str = "main",
    workspace_dir: str = DEFAULT_WORKSPACE,
) -> dict[str, object]:
    return {
        "event": "llm_input",
        "runId": run_id,
        "sessionId": f"session-{run_id}",
        "provider": "yonyou-default",
        "model": "deepseek-v4-flash",
        "sessionKey": session_key,
        "agentId": agent_id,
        "workspaceDir": workspace_dir,
        "ts": "2026-09-15T06:48:37.655Z",
    }


def make_output(
    run_id: str,
    *,
    session_key: str = "agent:main:session-test",
    agent_id: str = "main",
    workspace_dir: str = DEFAULT_WORKSPACE,
) -> dict[str, object]:
    return {
        "event": "llm_output",
        "runId": run_id,
        "sessionId": f"session-{run_id}",
        "provider": "yonyou-default",
        "model": "deepseek-v4-flash",
        "sessionKey": session_key,
        "agentId": agent_id,
        "workspaceDir": workspace_dir,
        "ts": "2026-09-15T06:49:44.596Z",
        "output": {
            "harnessId": "openclaw",
            "lastAssistant": {
                "usage": {
                    "input": 42_645,
                    "output": 1_026,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 43_671,
                }
            },
            "usage": {"input": 336_497, "output": 3_398, "total": 339_895},
        },
    }


class YonworkUsageCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.log_path = self.root / "observer log.jsonl"
        self.log_path.touch()
        self.original_state_dir = collector.STATE_DIR
        collector.STATE_DIR = self.root / ".state"

    def tearDown(self) -> None:
        collector.STATE_DIR = self.original_state_dir
        self.temp_dir.cleanup()

    def append_records(self, *records: dict[str, object]) -> None:
        with self.log_path.open("ab") as log_file:
            for record in records:
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                log_file.write(line.encode("utf-8") + b"\n")

    def assert_collector_error(self, error_code: str, action: object) -> collector.CollectorError:
        with self.assertRaises(collector.CollectorError) as context:
            action()  # type: ignore[operator]
        self.assertEqual(error_code, context.exception.error_code)
        return context.exception

    def test_normal_input_and_output(self) -> None:
        begin_result = collector.begin_collection("Case01-Run1", str(self.log_path))
        self.append_records(make_input("run-new"), make_output("run-new"))

        result = collector.collect_usage("Case01-Run1", timeout=0)

        self.assertTrue(begin_result["success"])
        self.assertEqual("run-new", result["runId"])
        self.assertEqual(336_497, result["runInputTokens"])
        self.assertEqual(3_398, result["runOutputTokens"])
        self.assertEqual(339_895, result["runTotalTokens"])
        self.assertEqual(42_645, result["finalInputTokens"])
        self.assertEqual(1_026, result["finalOutputTokens"])
        self.assertEqual(43_671, result["finalTotalTokens"])
        self.assertEqual(7.78, result["tokenAmplification"])
        state = json.loads((collector.STATE_DIR / "Case01-Run1.json").read_text(encoding="utf-8"))
        self.assertEqual(0, state["offset"])
        self.assertIn("startedAt", state)

    def test_old_run_before_offset_is_not_scanned(self) -> None:
        self.append_records(make_input("run-old"), make_output("run-old"))
        old_size = self.log_path.stat().st_size
        collector.begin_collection("offset-case", str(self.log_path))
        self.append_records(make_input("run-new"), make_output("run-new"))

        result = collector.collect_usage("offset-case", timeout=0)

        self.assertGreater(old_size, 0)
        self.assertEqual("run-new", result["runId"])

    def test_cron_before_normal_input_is_excluded(self) -> None:
        collector.begin_collection("cron-case", str(self.log_path))
        self.append_records(
            make_input("cron-run", session_key="agent:main:cron:daily"),
            make_output("cron-run", session_key="agent:main:cron:daily"),
            make_input("normal-run"),
            make_output("normal-run"),
        )

        result = collector.collect_usage("cron-case", timeout=0)

        self.assertEqual("normal-run", result["runId"])

    def test_weixin_session_is_excluded(self) -> None:
        collector.begin_collection("weixin-case", str(self.log_path))
        self.append_records(
            make_input("weixin-run", session_key="agent:main:openclaw-weixin:user"),
            make_output("weixin-run", session_key="agent:main:openclaw-weixin:user"),
            make_input("normal-run"),
            make_output("normal-run"),
        )

        result = collector.collect_usage("weixin-case", timeout=0)

        self.assertEqual("normal-run", result["runId"])

    def test_output_is_paired_by_exact_run_id(self) -> None:
        collector.begin_collection("pair-case", str(self.log_path))
        wrong_output = make_output("another-run")
        wrong_output["output"]["usage"]["total"] = 1  # type: ignore[index]
        self.append_records(make_input("wanted-run"), wrong_output, make_output("wanted-run"))

        result = collector.collect_usage("pair-case", timeout=0)

        self.assertEqual("wanted-run", result["runId"])
        self.assertEqual(339_895, result["runTotalTokens"])

    def test_output_not_yet_present_returns_timeout(self) -> None:
        collector.begin_collection("timeout-case", str(self.log_path))
        self.append_records(make_input("waiting-run"))

        error = self.assert_collector_error(
            "OUTPUT_TIMEOUT",
            lambda: collector.collect_usage("timeout-case", timeout=0),
        )

        self.assertEqual("waiting-run", error.details["runId"])

    def test_delayed_output_is_found_incrementally(self) -> None:
        collector.begin_collection("delayed-case", str(self.log_path))
        self.append_records(make_input("delayed-run"))

        def append_later() -> None:
            time.sleep(0.08)
            self.append_records(make_output("delayed-run"))

        writer = threading.Thread(target=append_later)
        writer.start()
        try:
            result = collector.collect_usage("delayed-case", timeout=1, poll_interval=0.02)
        finally:
            writer.join()

        self.assertEqual("delayed-run", result["runId"])

    def test_partial_output_line_can_finish_during_polling(self) -> None:
        collector.begin_collection("partial-case", str(self.log_path))
        self.append_records(make_input("partial-run"))
        output_line = json.dumps(make_output("partial-run"), separators=(",", ":")).encode("utf-8")
        split_at = len(output_line) // 2

        def append_in_parts() -> None:
            with self.log_path.open("ab") as log_file:
                log_file.write(output_line[:split_at])
                log_file.flush()
                time.sleep(0.08)
                log_file.write(output_line[split_at:] + b"\n")
                log_file.flush()

        writer = threading.Thread(target=append_in_parts)
        writer.start()
        try:
            result = collector.collect_usage("partial-case", timeout=1, poll_interval=0.02)
        finally:
            writer.join()

        self.assertEqual("partial-run", result["runId"])

    def test_incomplete_tail_does_not_raise_invalid_jsonl(self) -> None:
        collector.begin_collection("half-line-case", str(self.log_path))
        with self.log_path.open("ab") as log_file:
            log_file.write(b'{"event":"llm_input"')

        self.assert_collector_error(
            "INPUT_NOT_FOUND",
            lambda: collector.collect_usage("half-line-case", timeout=0),
        )

    def test_malformed_complete_line_returns_invalid_jsonl(self) -> None:
        collector.begin_collection("malformed-case", str(self.log_path))
        with self.log_path.open("ab") as log_file:
            log_file.write(b"not-json\n")

        self.assert_collector_error(
            "INVALID_JSONL",
            lambda: collector.collect_usage("malformed-case", timeout=0),
        )

    def test_missing_last_assistant_usage_returns_null_final_values(self) -> None:
        collector.begin_collection("missing-final-case", str(self.log_path))
        output = make_output("no-final-run")
        del output["output"]["lastAssistant"]  # type: ignore[index]
        self.append_records(make_input("no-final-run"), output)

        result = collector.collect_usage("missing-final-case", timeout=0)

        self.assertIsNone(result["finalInputTokens"])
        self.assertIsNone(result["finalOutputTokens"])
        self.assertIsNone(result["finalTotalTokens"])
        self.assertIsNone(result["tokenAmplification"])

    def test_missing_run_usage_returns_usage_not_found(self) -> None:
        collector.begin_collection("missing-run-case", str(self.log_path))
        output = make_output("no-run-usage")
        del output["output"]["usage"]  # type: ignore[index]
        self.append_records(make_input("no-run-usage"), output)

        self.assert_collector_error(
            "USAGE_NOT_FOUND",
            lambda: collector.collect_usage("missing-run-case", timeout=0),
        )

    def test_log_path_does_not_exist(self) -> None:
        missing_path = self.root / "missing.jsonl"
        self.assert_collector_error(
            "LOG_FILE_NOT_FOUND",
            lambda: collector.begin_collection("missing-log", str(missing_path)),
        )

    def test_state_does_not_exist(self) -> None:
        self.assert_collector_error(
            "STATE_NOT_FOUND",
            lambda: collector.collect_usage("missing-state", timeout=0),
        )

    def test_inspect_counts_pairs_and_session_types(self) -> None:
        self.append_records(
            make_input("normal-run"),
            make_output("normal-run"),
            make_input("cron-run", session_key="agent:main:cron:daily"),
            make_input("team-chat-summary:one", session_key="agent:main:team-personal"),
            make_output("team-chat-summary:one", session_key="agent:main:team-personal"),
            make_input("weixin-run", session_key="agent:main:openclaw-weixin:user"),
            make_output("other-run", agent_id="helper", workspace_dir=r"C:\somewhere\else"),
        )

        result = collector.inspect_log(str(self.log_path))

        self.assertEqual(4, result["llmInputCount"])
        self.assertEqual(3, result["llmOutputCount"])
        self.assertEqual(5, result["uniqueRunIds"])
        self.assertEqual(2, result["pairedRunCount"])
        self.assertEqual(["cron-run", "weixin-run"], result["unpairedInputRunIds"])
        self.assertEqual(["other-run"], result["unpairedOutputRunIds"])
        self.assertEqual(
            {"normal": 1, "cron": 1, "team-chat": 1, "weixin": 1, "other": 1},
            result["typeCounts"],
        )

    def test_cli_stdout_is_one_json_line(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = collector.main(
                ["begin", "--benchmark-id", "cli-case", "--log-path", str(self.log_path)]
            )

        lines = stdout.getvalue().splitlines()
        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(lines))
        self.assertTrue(json.loads(lines[0])["success"])
        self.assertEqual("", stderr.getvalue())

    def test_cli_failure_stdout_is_one_json_line_and_exit_is_nonzero(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = collector.main(["collect", "--benchmark-id", "no-state", "--timeout", "0"])

        lines = stdout.getvalue().splitlines()
        self.assertNotEqual(0, exit_code)
        self.assertEqual(1, len(lines))
        self.assertEqual("STATE_NOT_FOUND", json.loads(lines[0])["errorCode"])
        self.assertIn("STATE_NOT_FOUND", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
