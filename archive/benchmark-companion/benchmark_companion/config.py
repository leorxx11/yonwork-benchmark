from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _default_benchmark_dir() -> Path:
    # 仓库根目录 —— 本包在 <root>/benchmark-companion/benchmark_companion/ 下。
    # 旧默认值是 ~/Desktop/benchmark，那是上一台机器的布局，换机后必然指空。
    # 冻结成 exe 时 runtime_root() 是 dist 目录，这个推断不成立，
    # 但那种情况下配置走 exe 旁边的 config.json，默认值只是兜底。
    return runtime_root().parent


@dataclass(slots=True)
class AppConfig:
    workbook_path: str = str(_default_benchmark_dir() / "cases" / "yonwork_benchmark.xlsx")
    prompt_sheet: str = "Cases"
    stats_script_path: str = str(_default_benchmark_dir() / "scripts" / "newapi_stats.ps1")
    token_name: str = "workbuddy"
    stats_settle_seconds: float = 2.0
    stats_timeout_seconds: int = 90
    powershell_executable: str = "powershell.exe"
    database_path: str = "data/benchmark_companion.db"
    backup_dir: str = "data/backups"
    log_path: str = "data/benchmark_companion.log"
    always_on_top: bool = True
    _source_path: Path | None = field(default=None, init=False, repr=False)

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        config_path = path or runtime_root() / "config.json"
        if not config_path.exists():
            config = cls()
            config.save(config_path)
            return config

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        allowed = {item.name for item in fields(cls) if item.init}
        values = {key: value for key, value in raw.items() if key in allowed}
        config = cls(**values)
        config._source_path = config_path
        return config

    def save(self, path: Path | None = None) -> Path:
        config_path = path or self._source_path or runtime_root() / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        values = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.init and not item.name.startswith("_")
        }
        temp_path.write_text(
            json.dumps(values, ensure_ascii=False, indent=2) + os.linesep,
            encoding="utf-8",
        )
        os.replace(temp_path, config_path)
        self._source_path = config_path
        return config_path

    def resolved_path(self, value: str) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(value)))
        if not path.is_absolute():
            path = runtime_root() / path
        return path.resolve()

    @property
    def workbook(self) -> Path:
        return self.resolved_path(self.workbook_path)

    @property
    def stats_script(self) -> Path:
        return self.resolved_path(self.stats_script_path)

    @property
    def database(self) -> Path:
        return self.resolved_path(self.database_path)

    @property
    def backups(self) -> Path:
        return self.resolved_path(self.backup_dir)

    @property
    def log_file(self) -> Path:
        return self.resolved_path(self.log_path)

    def update_from(self, values: dict[str, Any]) -> None:
        allowed = {item.name for item in fields(self) if item.init}
        for key, value in values.items():
            if key in allowed:
                setattr(self, key, value)
