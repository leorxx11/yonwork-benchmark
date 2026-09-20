from __future__ import annotations

import logging
import os
import sys
import tempfile
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from benchmark_companion.config import AppConfig
from benchmark_companion.cases import load_cases
from benchmark_companion.fonts import load_ui_font
from benchmark_companion.models import ModelMode, expand_cases
from benchmark_companion.storage import BenchmarkStore
from benchmark_companion.ui import MainWindow


def configure_logging(config: AppConfig) -> None:
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        config.log_file,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def run_self_test(config: AppConfig) -> int:
    try:
        result = load_cases(config.workbook, sheet_name=config.prompt_sheet)
        if not expand_cases(result.cases):
            return 11
        if not config.stats_script.exists():
            return 12
        with tempfile.TemporaryDirectory() as temp:
            BenchmarkStore(Path(temp) / "self-test.db")
    except Exception:
        return 10
    return 0


def render_smoke_preview(
    application: QApplication, config: AppConfig, output_path: Path
) -> int:
    temporary = tempfile.TemporaryDirectory()
    temp_root = Path(temporary.name)
    preview_config = replace(
        config,
        database_path=str(temp_root / "preview.db"),
        backup_dir=str(temp_root / "backups"),
        log_path=str(temp_root / "preview.log"),
        always_on_top=False,
    )
    cases = load_cases(
        preview_config.workbook, sheet_name=preview_config.prompt_sheet
    )
    store = BenchmarkStore(preview_config.database)
    store.create_session(
        model_mode=ModelMode.NEW_API,
        items=expand_cases(cases.cases),
        workbook_path=str(preview_config.workbook),
        prompt_sheet=preview_config.prompt_sheet,
        stats_script_path=str(preview_config.stats_script),
        token_name=preview_config.token_name,
    )
    window = MainWindow(preview_config, store)
    window.show()

    def capture() -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        saved = window.grab().save(str(output_path), "PNG")
        window._allow_close = True
        window.close()
        application.exit(0 if saved else 13)

    QTimer.singleShot(500, capture)
    exit_code = application.exec()
    temporary.cleanup()
    return exit_code


def main() -> int:
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    application = QApplication(sys.argv)
    application.setApplicationName("Benchmark Companion")
    application.setOrganizationName("Benchmark Companion")
    application.setFont(load_ui_font(10))

    try:
        config = AppConfig.load()
    except Exception as exc:
        QMessageBox.critical(None, "启动失败", f"Benchmark Companion 无法启动：\n{exc}")
        return 1

    if "--self-test" in sys.argv:
        return run_self_test(config)

    if "--smoke-screenshot" in sys.argv:
        index = sys.argv.index("--smoke-screenshot")
        if index + 1 >= len(sys.argv):
            return 14
        try:
            return render_smoke_preview(application, config, Path(sys.argv[index + 1]).resolve())
        except Exception:
            return 15

    try:
        configure_logging(config)
        store = BenchmarkStore(config.database)
    except Exception as exc:
        QMessageBox.critical(None, "启动失败", f"Benchmark Companion 无法启动：\n{exc}")
        return 1

    lock = QLockFile(str(config.database.with_suffix(".lock")))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.warning(None, "程序已运行", "Benchmark Companion 已经在运行。")
        return 2

    def handle_exception(exc_type, exc_value, exc_traceback):
        logging.getLogger(__name__).critical(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback)
        )
        QMessageBox.critical(None, "程序错误", f"发生未处理错误：\n{exc_value}")

    sys.excepthook = handle_exception
    window = MainWindow(config, store)
    window.show()
    exit_code = application.exec()
    lock.unlock()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
