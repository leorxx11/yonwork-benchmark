from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runner.case_catalog import CaseCatalogError, load_case_set, load_catalog


class CaseCatalogTests(unittest.TestCase):
    def _catalog(self, body: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "catalog.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_loads_case_sets_and_assertions(self) -> None:
        path = self._catalog(
            """
version: 1
case_sets:
  - id: smoke
    name: 冒烟
    cases:
      - id: Case01
        prompt: 你好
        runs: 2
        assertions:
          expect: [你好]
          max_seconds: 30
"""
        )
        case_set = load_case_set(path, "smoke")
        self.assertEqual(1, case_set.enabled_count)
        self.assertEqual(2, case_set.total_runs)
        self.assertEqual(("你好",), case_set.cases[0].expectations.expect_keywords)
        self.assertEqual(30.0, case_set.cases[0].expectations.max_seconds)

    def test_environment_placeholders_are_resolved(self) -> None:
        path = self._catalog(
            """
version: 1
case_sets:
  - id: files
    name: 文件
    cases:
      - id: Case01
        prompt: 打开 ${FIXTURE_PATH}
"""
        )
        unresolved = load_case_set(path, "files", resolve_environment=False)
        self.assertEqual(("FIXTURE_PATH",), unresolved.required_env)
        with self.assertRaisesRegex(CaseCatalogError, "FIXTURE_PATH"):
            load_case_set(path, "files")

        from runner.case_catalog import resolve_case_set

        resolved = resolve_case_set(unresolved, {"FIXTURE_PATH": "C:\\data\\book.txt"})
        self.assertEqual("打开 C:\\data\\book.txt", resolved.cases[0].prompt)

    def test_rejects_unknown_fields_instead_of_silently_ignoring_typos(self) -> None:
        path = self._catalog(
            """
version: 1
case_sets:
  - id: smoke
    name: 冒烟
    cases:
      - id: Case01
        prompt: 你好
        runz: 2
"""
        )
        with self.assertRaisesRegex(CaseCatalogError, "runz"):
            load_catalog(path)

    def test_disabled_case_does_not_require_its_machine_environment(self) -> None:
        path = self._catalog(
            """
version: 1
case_sets:
  - id: smoke
    name: 冒烟
    cases:
      - id: disabled-file
        prompt: 打开 ${UNCONFIGURED_PATH}
        enabled: false
      - id: enabled-chat
        prompt: 你好
"""
        )
        case_set = load_case_set(path, "smoke")
        self.assertEqual(1, case_set.enabled_count)
        self.assertEqual("打开 ${UNCONFIGURED_PATH}", case_set.cases[0].prompt)

    def test_rejects_duplicate_case_ids(self) -> None:
        path = self._catalog(
            """
version: 1
case_sets:
  - id: smoke
    name: 冒烟
    cases:
      - {id: Case01, prompt: A}
      - {id: Case01, prompt: B}
"""
        )
        with self.assertRaisesRegex(CaseCatalogError, "重复"):
            load_catalog(path)


if __name__ == "__main__":
    unittest.main()
