from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmark_companion.config import AppConfig


class ConfigTests(unittest.TestCase):
    def test_prompt_sheet_is_saved_and_reuses_loaded_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            config = AppConfig(prompt_sheet="long-text")
            config.save(path)

            loaded = AppConfig.load(path)
            self.assertEqual("long-text", loaded.prompt_sheet)
            loaded.prompt_sheet = "Cases"
            loaded.save()

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("Cases", raw["prompt_sheet"])
            self.assertNotIn("_source_path", raw)


if __name__ == "__main__":
    unittest.main()
