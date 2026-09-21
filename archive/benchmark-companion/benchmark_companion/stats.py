from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from .models import TokenStats


class StatsQueryError(RuntimeError):
    pass


_STATS_LINE = re.compile(
    r"^\s*(-?\d+)\|(-?\d+)\|(-?\d+)\|(-?\d+)\|(-?\d+)\|(-?\d+(?:\.\d+)?)\s*$"
)


def parse_stats_output(output: str) -> TokenStats:
    for line in reversed(output.splitlines()):
        match = _STATS_LINE.match(line)
        if not match:
            continue
        values = match.groups()
        return TokenStats(
            api_calls=int(values[0]),
            error_calls=int(values[1]),
            input_tokens=int(values[2]),
            output_tokens=int(values[3]),
            total_tokens=int(values[4]),
            api_use_time=float(values[5]),
        )
    raise StatsQueryError("统计脚本没有返回预期的 6 段数值")


def query_token_stats(
    *,
    script_path: Path,
    start_time: str,
    end_time: str,
    token_name: str,
    powershell_executable: str = "powershell.exe",
    settle_seconds: float = 2.0,
    timeout_seconds: int = 90,
) -> TokenStats:
    if not script_path.exists():
        raise StatsQueryError(f"找不到统计脚本：{script_path}")
    executable = shutil.which(powershell_executable)
    if executable is None:
        raise StatsQueryError(f"找不到 PowerShell：{powershell_executable}")
    if settle_seconds > 0:
        time.sleep(settle_seconds)

    command = [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-StartTime",
        start_time,
        "-EndTime",
        end_time,
        "-TokenName",
        token_name,
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        creationflags=creation_flags,
        check=False,
    )
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0:
        message = combined.strip().splitlines()[-1] if combined.strip() else "未知错误"
        raise StatsQueryError(f"统计脚本执行失败：{message[:500]}")
    return parse_stats_output(combined)

