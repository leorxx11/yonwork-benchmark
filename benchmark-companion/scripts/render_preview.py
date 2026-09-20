from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BENCHMARK_COMPANION_DISABLE_GLOBAL_HOTKEYS", "1")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from benchmark_companion.cases import load_cases
from benchmark_companion.config import AppConfig
from benchmark_companion.fonts import load_ui_font
from benchmark_companion.models import ModelMode, expand_cases
from benchmark_companion.storage import BenchmarkStore
from benchmark_companion.ui import MainWindow


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: render_preview.py OUTPUT.png")
    output = Path(sys.argv[1]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        # workbook_path / stats_script_path 走 AppConfig 的默认值（仓库相对路径），
        # 截图里显示什么路径无关紧要，别再写死某台机器的绝对路径。
        config = AppConfig(
            token_name="workbuddy",
            database_path=str(temp_dir / "preview.db"),
            backup_dir=str(temp_dir / "backups"),
            log_path=str(temp_dir / "preview.log"),
            always_on_top=False,
        )
        cases = load_cases(config.workbook)
        store = BenchmarkStore(config.database)
        store.create_session(
            model_mode=ModelMode.NEW_API,
            items=expand_cases(cases.cases),
            workbook_path=str(config.workbook),
            stats_script_path=str(config.stats_script),
            token_name=config.token_name,
        )

        application = QApplication.instance() or QApplication([])
        application.setFont(load_ui_font(10))
        window = MainWindow(config, store)
        window.show()
        QTest.qWait(250)
        image = window.grab()
        if not image.save(str(output), "PNG"):
            raise RuntimeError(f"Could not save preview to {output}")
        window.close()
        application.processEvents()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
