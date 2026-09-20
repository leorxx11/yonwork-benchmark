from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThread, Signal


class FunctionWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str, str)

    def __init__(self, function: Callable[[], Any], parent=None):
        super().__init__(parent)
        self._function = function

    def run(self) -> None:
        try:
            result = self._function()
        except Exception as exc:
            self.failed.emit(str(exc), traceback.format_exc())
        else:
            self.succeeded.emit(result)

