from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal


WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
HOTKEY_START = 0xB801
HOTKEY_SUCCESS = 0xB802
HOTKEY_FAILED = 0xB803

VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79


class _NativeHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt API
        if os.name != "nt":
            return False, 0
        try:
            address = int(message)
            native_message = ctypes.cast(
                address, ctypes.POINTER(wintypes.MSG)
            ).contents
        except (TypeError, ValueError, OSError):
            return False, 0
        if native_message.message == WM_HOTKEY:
            self._callback(int(native_message.wParam))
            return True, 0
        return False, 0


class GlobalHotkeys(QObject):
    start_requested = Signal()
    success_requested = Signal()
    failed_requested = Signal()

    def __init__(self, application, parent=None):
        super().__init__(parent)
        self._application = application
        self._filter = _NativeHotkeyFilter(self._dispatch)
        self._registered: list[int] = []
        self.errors: list[str] = []

    def register(self) -> bool:
        if os.environ.get("BENCHMARK_COMPANION_DISABLE_GLOBAL_HOTKEYS") == "1":
            self.errors.append("测试模式已禁用全局快捷键")
            return False
        if os.name != "nt":
            self.errors.append("全局快捷键仅支持 Windows")
            return False

        self._application.installNativeEventFilter(self._filter)
        user32 = ctypes.windll.user32
        definitions = (
            (HOTKEY_START, VK_F8, "F8"),
            (HOTKEY_SUCCESS, VK_F9, "F9"),
            (HOTKEY_FAILED, VK_F10, "F10"),
        )
        for identifier, virtual_key, label in definitions:
            if user32.RegisterHotKey(None, identifier, MOD_NOREPEAT, virtual_key):
                self._registered.append(identifier)
            else:
                self.errors.append(f"{label} 已被其他程序占用")
        return len(self._registered) == len(definitions)

    def unregister(self) -> None:
        if os.name == "nt":
            user32 = ctypes.windll.user32
            for identifier in self._registered:
                user32.UnregisterHotKey(None, identifier)
        self._registered.clear()
        try:
            self._application.removeNativeEventFilter(self._filter)
        except RuntimeError:
            pass

    def _dispatch(self, identifier: int) -> None:
        if identifier == HOTKEY_START:
            self.start_requested.emit()
        elif identifier == HOTKEY_SUCCESS:
            self.success_requested.emit()
        elif identifier == HOTKEY_FAILED:
            self.failed_requested.emit()

