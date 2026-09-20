from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from runner.cases import CasesError, list_prompt_sheets, load_cases
from runner.models import expand_cases


class CasesTests(unittest.TestCase):
    def _workbook(self, headers: list[str], rows: list[list[object]]) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "cases.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Cases"
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        workbook.save(path)
        workbook.close()
        return path

    def test_loads_the_existing_four_column_sheet(self) -> None:
        path = self._workbook(
            ["CaseName", "Prompt", "Runs", "Enabled"],
            [["Case01", "你好", 2, True], ["Case02", "天气", 3, 0]],
        )
        result = load_cases(path)
        self.assertEqual(1, result.enabled_count)
        self.assertEqual(2, result.total_runs)
        self.assertIn("Case02 已禁用", result.warnings)
        self.assertEqual(["Cases"], list_prompt_sheets(path))

    def test_reads_optional_assertion_columns(self) -> None:
        path = self._workbook(
            ["CaseName", "Prompt", "Runs", "Enabled", "Expect", "Forbid", "MaxSeconds"],
            [["Case01", "输出 JSON", 1, 1, "凤仙郡|回目", "抱歉|无法", 90]],
        )
        expectations = load_cases(path).cases[0].expectations
        self.assertEqual(("凤仙郡", "回目"), expectations.expect_keywords)
        self.assertEqual(("抱歉", "无法"), expectations.forbid_keywords)
        self.assertEqual(90.0, expectations.max_seconds)

    def test_warns_about_unknown_columns(self) -> None:
        path = self._workbook(
            ["CaseName", "Prompt", "Runs", "Enabled", "Comment"],
            [["Case01", "你好", 1, 1, "随手写的"]],
        )
        self.assertIn("忽略未知列：Comment", load_cases(path).warnings)

    def test_rejects_duplicate_case_names(self) -> None:
        path = self._workbook(
            ["CaseName", "Prompt", "Runs", "Enabled"],
            [["Case01", "你好", 1, 1], ["Case01", "天气", 1, 1]],
        )
        with self.assertRaisesRegex(CasesError, "重复"):
            load_cases(path)

    def test_rejects_non_numeric_threshold(self) -> None:
        path = self._workbook(
            ["CaseName", "Prompt", "Runs", "Enabled", "MaxSeconds"],
            [["Case01", "你好", 1, 1, "很快"]],
        )
        with self.assertRaisesRegex(CasesError, "MaxSeconds"):
            load_cases(path)

    def test_expand_repeats_enabled_cases_only(self) -> None:
        path = self._workbook(
            ["CaseName", "Prompt", "Runs", "Enabled"],
            [["Case01", "你好", 2, 1], ["Case02", "天气", 5, 0]],
        )
        items = expand_cases(load_cases(path).cases)
        self.assertEqual([("Case01", 1), ("Case01", 2)], [(i.case_name, i.run_no) for i in items])
        self.assertEqual([0, 1], [item.position for item in items])


if __name__ == "__main__":
    unittest.main()
