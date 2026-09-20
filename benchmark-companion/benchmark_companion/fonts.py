from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase


def load_ui_font(point_size: int = 10) -> QFont:
    candidates = (
        (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc", "Microsoft YaHei UI"),
        (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "Deng.ttf", "DengXian"),
        (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "simhei.ttf", "SimHei"),
    )
    for path, preferred_family in candidates:
        if not path.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if preferred_family in families:
            return QFont(preferred_family, point_size)
        if families:
            return QFont(families[0], point_size)
    return QFont("Arial", point_size)

