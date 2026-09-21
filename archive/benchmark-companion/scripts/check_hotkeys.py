from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from benchmark_companion.hotkeys import GlobalHotkeys, HOTKEY_START, WM_HOTKEY


def main() -> int:
    application = QApplication([])
    hotkeys = GlobalHotkeys(application)
    received: list[bool] = []
    hotkeys.start_requested.connect(lambda: (received.append(True), application.quit()))
    if not hotkeys.register():
        hotkeys.unregister()
        return 2

    thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

    def post_test_message() -> None:
        ctypes.windll.user32.PostThreadMessageW(thread_id, WM_HOTKEY, HOTKEY_START, 0)

    QTimer.singleShot(100, post_test_message)
    QTimer.singleShot(2_000, application.quit)
    application.exec()
    hotkeys.unregister()
    return 0 if received else 3


if __name__ == "__main__":
    raise SystemExit(main())

