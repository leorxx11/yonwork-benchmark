from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from scripts import install_trace_bridge as installer
from scripts.trace_bridge_verify import (
    FAIL,
    OK,
    WARN,
    check_batch,
    check_bindings,
    check_config,
    check_exposure,
    check_gateway_load,
    check_versions,
    parse_log,
    parse_time,
)

PLUGIN = "benchmark-trace-bridge"
SPAN_A = ("a" * 32, "1" * 16)
SPAN_B = ("a" * 32, "2" * 16)


def log_line(at: str, message: str, level: str = "INFO") -> str:
    return json.dumps({"0": "{}", "1": message, "_meta": {"date": at, "logLevelName": level}})


def load_block(at: str, *, plugin: bool = True, failed: int = 0) -> list[str]:
    lines = []
    if plugin:
        lines.append(log_line(at, f"startup trace: plugins.gateway-load.plugin.{PLUGIN} "
                                  f"loadMs=11.5 loadFailedCount={failed}.0"))
        lines.append(log_line(at, f"startup trace: plugins.gateway-load.plugin.{PLUGIN} "
                                  "registerMs=0.2 registerFailedCount=0.0"))
    lines.append(log_line(at, "startup trace: plugins.gateway-load autoEnableMs=0.0 loadMs=639.6"))
    return lines


def levels(checks) -> dict[str, str]:
    return {check.name: check.level for check in checks}


class TimeTests(unittest.TestCase):
    def test_powershell_seven_digit_fraction(self) -> None:
        self.assertEqual(parse_time("2026-09-23T03:19:52.8131420Z"),
                         datetime(2026, 9, 23, 3, 19, 52, 813142, tzinfo=timezone.utc))

    def test_garbage_is_none(self) -> None:
        self.assertIsNone(parse_time("昨天"))
        self.assertIsNone(parse_time(None))


class ConfigTests(unittest.TestCase):
    def test_all_registered(self) -> None:
        status = dict(allowed=True, enabled=True, load_path=True, token_configured=True)
        checks = check_config(status, files_present=True, files_current=True)
        self.assertTrue(all(check.level == OK for check in checks))

    def test_wiped_by_update_is_a_failure(self) -> None:
        status = dict(allowed=False, enabled=True, load_path=False, token_configured=True)
        checks = check_config(status, files_present=True, files_current=True)
        self.assertEqual(levels(checks)["openclaw.json 三处登记"], FAIL)

    def test_stale_installed_copy_is_flagged(self) -> None:
        status = dict(allowed=True, enabled=True, load_path=True, token_configured=True)
        checks = check_config(status, files_present=True, files_current=False)
        self.assertEqual(levels(checks)["插件文件与仓库一致"], WARN)

    def test_install_and_uninstall_round_trip(self) -> None:
        config = {"plugins": {"allow": ["x"], "entries": {}, "load": {"paths": ["C:\\other"]}}}
        installer.install(config, "C:\\p", url="http://127.0.0.1:3312", token="t")
        self.assertEqual(installer.status(config, "C:\\p"),
                         dict(allowed=True, enabled=True, load_path=True, token_configured=True))
        installer.uninstall(config, "C:\\p")
        self.assertEqual(config["plugins"],
                         {"allow": ["x"], "entries": {}, "load": {"paths": ["C:\\other"]}})


class GatewayLoadTests(unittest.TestCase):
    def test_loaded_in_the_current_start(self) -> None:
        entries = parse_log(load_block("2026-09-23T03:00:57Z") + load_block("2026-09-23T03:20:09Z"))
        checks, loaded_at = check_gateway_load(entries, parse_time("2026-09-23T03:19:52Z"))
        self.assertEqual(levels(checks), {"当前这次启动加载了插件": OK})
        self.assertEqual(loaded_at, parse_time("2026-09-23T03:20:09Z"))

    def test_loaded_only_in_an_earlier_start_does_not_count(self) -> None:
        """网关会自己重启：日志里「曾经加载过」不代表现在还加载着。"""
        entries = parse_log(load_block("2026-09-23T03:00:57Z")
                            + load_block("2026-09-23T03:20:09Z", plugin=False))
        checks, _ = check_gateway_load(entries, None)
        self.assertEqual(levels(checks)["当前这次启动加载了插件"], FAIL)

    def test_failed_count_is_a_failure(self) -> None:
        checks, _ = check_gateway_load(parse_log(load_block("2026-09-23T03:20:09Z", failed=1)), None)
        self.assertEqual(levels(checks)["当前这次启动加载了插件"], FAIL)

    def test_blocked_hook_warning_is_a_failure(self) -> None:
        lines = load_block("2026-09-23T03:20:09Z") + [log_line(
            "2026-09-23T03:20:10Z", f'typed hook "model_call_started" blocked for {PLUGIN}', "WARN")]
        checks, _ = check_gateway_load(parse_log(lines), None)
        self.assertEqual(levels(checks)["没有针对插件的警告"], FAIL)

    def test_gateway_newer_than_last_load_is_flagged(self) -> None:
        checks, _ = check_gateway_load(parse_log(load_block("2026-09-23T03:00:57Z")),
                                       parse_time("2026-09-23T03:19:52Z"))
        self.assertEqual(levels(checks)["加载记录是当前进程的"], WARN)

    def test_no_load_record_at_all(self) -> None:
        checks, loaded_at = check_gateway_load([], None)
        self.assertEqual(checks[0].level, FAIL)
        self.assertIsNone(loaded_at)


class BindingTests(unittest.TestCase):
    SINCE = parse_time("2026-09-23T03:20:09Z")

    def binding(self, receipt: str, at: str = "2026-09-23T03:30:00Z") -> dict:
        return {"at": at, "receipt": receipt}

    def test_nothing_since_the_current_start(self) -> None:
        checks = check_bindings([self.binding("bound", "2026-09-23T03:05:00Z")], self.SINCE)
        self.assertEqual(checks[0].level, WARN)

    def test_delivery_errors_fail(self) -> None:
        for receipt in ("error:ECONNREFUSED", "http-401", "skipped:no-token", "no-receipt"):
            with self.subTest(receipt=receipt):
                checks = check_bindings([self.binding("bound"), self.binding(receipt)], self.SINCE)
                self.assertEqual(checks[0].level, FAIL)

    def test_only_unregistered_traffic_is_not_proof(self) -> None:
        checks = check_bindings([self.binding("unregistered")], self.SINCE)
        self.assertEqual(checks[0].level, WARN)

    def test_bound_passes(self) -> None:
        checks = check_bindings([self.binding("bound"), self.binding("unregistered")], self.SINCE)
        self.assertEqual(checks[0].level, OK)


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [{"benchmark_id": "b-1", "model_calls": {"status": "observed"}}]
        self.requests = [
            {"run_id": "b-1", "attribution": "attributed", "traceparent": f"00-{s[0]}-{s[1]}-01"}
            for s in (SPAN_A, SPAN_B)]
        self.bindings = [
            {"run_id": "b-1", "trace_id": s[0], "span_id": s[1], "receipt": "bound"}
            for s in (SPAN_A, SPAN_B)]

    def test_clean_batch(self) -> None:
        checks = check_batch(self.records, self.requests, self.bindings)
        self.assertTrue(all(check.level == OK for check in checks), checks)

    def test_unattributed_request_fails(self) -> None:
        self.requests[1] = {**self.requests[1], "run_id": None, "attribution": "unattributed"}
        self.assertEqual(levels(check_batch(self.records, self.requests, self.bindings))
                         ["账本请求全部归属"], FAIL)

    def test_conflict_counts_as_not_attributed(self) -> None:
        self.requests[1] = {**self.requests[1], "run_id": None, "attribution": "unattributed",
                            "attribution_source": "conflict"}
        check = [c for c in check_batch(self.records, self.requests, self.bindings)
                 if c.name == "账本请求全部归属"][0]
        self.assertIn("conflict", check.detail)

    def test_hook_run_id_not_the_benchmark_id_fails(self) -> None:
        """升级后 hook 的 runId 语义变了：插件还在跑，但一轮都对不上。"""
        bindings = [{**b, "run_id": "some-internal-uuid"} for b in self.bindings]
        self.assertEqual(levels(check_batch(self.records, self.requests, bindings))
                         ["hook 的 runId 就是 BenchmarkId"], FAIL)

    def test_span_mismatch_fails(self) -> None:
        """升级后请求不再带同一个 traceparent：hook 在，请求却对不上。"""
        self.bindings[1] = {**self.bindings[1], "span_id": "9" * 16}
        self.assertEqual(levels(check_batch(self.records, self.requests, self.bindings))
                         ["hook span 与账本请求一一对应"], FAIL)

    def test_compaction_run_id_belongs_to_its_turn(self) -> None:
        self.bindings[1] = {**self.bindings[1], "run_id": "b-1:compaction:3"}
        checks = check_batch(self.records, self.requests, self.bindings)
        self.assertTrue(all(check.level == OK for check in checks), checks)

    def test_unavailable_turn_fails(self) -> None:
        self.records.append({"benchmark_id": "b-2", "model_calls": {"status": "unavailable"}})
        self.assertEqual(levels(check_batch(self.records, self.requests, self.bindings))
                         ["每轮都采到请求"], FAIL)

    def test_empty_ledger_fails(self) -> None:
        self.assertEqual(levels(check_batch(self.records, [], self.bindings))["账本请求全部归属"], FAIL)


class VersionAndExposureTests(unittest.TestCase):
    NETSTAT = """
活动连接
  协议  本地地址          外部地址        状态           PID
  TCP    127.0.0.1:3211         0.0.0.0:0              LISTENING       8484
  TCP    127.0.0.1:9223         0.0.0.0:0              LISTENING       8484
  TCP    127.0.0.1:3211         127.0.0.1:50000        ESTABLISHED     8484
"""

    def test_unverified_version_warns(self) -> None:
        checks = check_versions({"yonwork": "1.0.11", "openclaw": "2026.7.1-2"})
        self.assertEqual(levels(checks), {"yonwork": WARN, "openclaw": OK})

    def test_loopback_only_passes(self) -> None:
        checks = check_exposure(self.NETSTAT, {"Host API": 3211, "CDP": 9223}, (401, 200))
        self.assertTrue(all(check.level == OK for check in checks), checks)

    def test_all_interfaces_fails(self) -> None:
        netstat = self.NETSTAT + "  TCP    0.0.0.0:3211           0.0.0.0:0              LISTENING       8484\n"
        checks = check_exposure(netstat, {"Host API": 3211, "CDP": 9223}, (401, 200))
        self.assertEqual(levels(checks)["Host API 只绑 loopback"], FAIL)

    def test_trusted_mode_fails(self) -> None:
        """从 WSL 拉起后 AUTH_MODE 退回 trusted：不带 token 也 200。"""
        checks = check_exposure(self.NETSTAT, {"Host API": 3211, "CDP": 9223}, (200, 200))
        self.assertEqual(levels(checks)["Host API 在校验 token"], FAIL)


if __name__ == "__main__":
    unittest.main()
