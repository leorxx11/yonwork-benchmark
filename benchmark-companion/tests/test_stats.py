from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from benchmark_companion.stats import StatsQueryError, parse_stats_output, query_token_stats


class StatsTests(unittest.TestCase):
    def test_parses_last_matching_line(self) -> None:
        result = parse_stats_output("warning\n2|1|56087|562|56649|34\n")
        self.assertEqual(2, result.api_calls)
        self.assertEqual(1, result.error_calls)
        self.assertEqual(56087, result.input_tokens)
        self.assertEqual(34.0, result.api_use_time)

    def test_rejects_unexpected_output(self) -> None:
        with self.assertRaises(StatsQueryError):
            parse_stats_output("no stats")

    def test_invokes_powershell_script_with_expected_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "stats.ps1"
            script.write_text(
                "param([string]$StartTime,[string]$EndTime,[string]$TokenName)\n"
                "if ($TokenName -ne 'workbuddy') { exit 4 }\n"
                "Write-Output '3|0|100|25|125|1.5'\n",
                encoding="utf-8",
            )
            result = query_token_stats(
                script_path=script,
                start_time="2026-09-14T10:00:00.000+08:00",
                end_time="2026-09-14T10:00:02.000+08:00",
                token_name="workbuddy",
                settle_seconds=0,
                timeout_seconds=10,
            )
        self.assertEqual(3, result.api_calls)
        self.assertEqual(125, result.total_tokens)


if __name__ == "__main__":
    unittest.main()
