"""把 `plugins/benchmark-trace-bridge` 装进 / 卸出 YonWork 的 OpenClaw。

    .venv/bin/python -m scripts.install_trace_bridge status
    .venv/bin/python -m scripts.install_trace_bridge install
    .venv/bin/python -m scripts.install_trace_bridge uninstall
    .venv/bin/python -m scripts.install_trace_bridge verify [--batch results/<批次>]

YonWork 更新或重启后跑 `verify`（见 `scripts/trace_bridge_verify.py`）。

装完/卸完都要**重启 YonWork** 才生效：网关启动时才从 `openclaw.json`
生成 `openclaw.runtime.json` 并加载插件（CLAUDE.md 坑 3：它是 uTools 拉起来的，
但这里只改文件不改环境变量，直接重启 YonWork 即可）。

⚠️ 装上之后**被测对象不再是出厂状态**：每次模型调用都会跑我们的 hook。
报告里要标出这批数据是加载扩展跑的，见 `docs/yonwork-1.0.10-correlation-probe.md`。

放在哪、为什么：
- 插件文件放 `C:\\Users\\<用户>\\.benchmark\\benchmark-trace-bridge`，**不放** YonWork 的
  `extensions/`——那个目录是从安装包同步过来的，不归我们管。也不碰 `D:\\yonwork\\`（CLAUDE.md 七）。
- YonWork 启动时清理 `openclaw.json`，但保留「绝对路径、存在、含 openclaw.plugin.json、
  不在 node_modules/openclaw/extensions 下」的 `plugins.load.paths`，只删一份写死的旧插件名单。
  我们的插件 ID 不在名单里。（1.0.10 主进程 bundle，2026-09-23 查证）
- 不申请 `allowConversationAccess`：model_call_* 不受它管，最小权限。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from runner.db import load_env_file, project_root

PLUGIN_ID = "benchmark-trace-bridge"
PLUGIN_FILES = ("index.js", "openclaw.plugin.json", "package.json")
SOURCE = project_root() / "plugins" / PLUGIN_ID


def _setting(name: str) -> str:
    import os

    return os.environ.get(name, "").strip() or load_env_file(project_root() / ".env").get(name, "").strip()


def _openclaw_json() -> Path:
    data_dir = Path(_setting("YONWORK_DATA_DIR") or "")
    if not data_dir.is_dir():
        sys.exit("YONWORK_DATA_DIR 没配或不存在（.env），例：/mnt/c/Users/<用户>/AppData/Roaming/yonwork")
    found = sorted(data_dir.glob("profiles/*/userData/runtime/openclaw/openclaw.json"))
    if len(found) != 1:
        sys.exit(f"期望恰好一个 profile 的 openclaw.json，找到 {len(found)} 个：{found}")
    return found[0]


def _target_dir() -> Path:
    # YONWORK_DATA_DIR = /mnt/c/Users/<用户>/AppData/Roaming/yonwork → /mnt/c/Users/<用户>
    home = Path(_setting("YONWORK_DATA_DIR")).parents[2]
    return home / ".benchmark" / PLUGIN_ID


def _windows_path(path: Path) -> str:
    return subprocess.run(["wslpath", "-w", str(path)], check=True, capture_output=True,
                          text=True).stdout.strip()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, config: dict) -> Path:
    backup = path.with_name(f"{path.name}.bak-trace-bridge-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return backup


def status(config: dict, load_path: str) -> dict:
    plugins = config.get("plugins") or {}
    entry = (plugins.get("entries") or {}).get(PLUGIN_ID) or {}
    return {
        "allowed": PLUGIN_ID in (plugins.get("allow") or []),
        "enabled": entry.get("enabled") is True,
        "load_path": load_path in ((plugins.get("load") or {}).get("paths") or []),
        "token_configured": bool((entry.get("config") or {}).get("collectorToken")),
    }


def install(config: dict, load_path: str, *, url: str, token: str) -> dict:
    plugins = config.setdefault("plugins", {})
    allow = plugins.setdefault("allow", [])
    if PLUGIN_ID not in allow:
        allow.append(PLUGIN_ID)
    plugins.setdefault("entries", {})[PLUGIN_ID] = {
        "enabled": True,
        "config": {"collectorUrl": url, "collectorToken": token},
    }
    paths = plugins.setdefault("load", {}).setdefault("paths", [])
    if load_path not in paths:
        paths.append(load_path)
    return config


def uninstall(config: dict, load_path: str) -> dict:
    plugins = config.get("plugins") or {}
    if PLUGIN_ID in (plugins.get("allow") or []):
        plugins["allow"].remove(PLUGIN_ID)
    (plugins.get("entries") or {}).pop(PLUGIN_ID, None)
    paths = (plugins.get("load") or {}).get("paths") or []
    if load_path in paths:
        paths.remove(load_path)
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("status", "install", "uninstall", "verify"))
    parser.add_argument("--batch", type=Path,
                        help="verify 时顺带核对这一批（results/<批次目录>），要求用统一代理模式跑过")
    args = parser.parse_args(argv)

    config_path = _openclaw_json()
    target = _target_dir()
    load_path = _windows_path(target.parent) + "\\" + PLUGIN_ID
    config = _load(config_path)

    if args.action == "status":
        print(json.dumps({"openclaw_json": str(config_path), "plugin_dir": load_path,
                          "files_present": all((target / f).is_file() for f in PLUGIN_FILES),
                          **status(config, load_path)}, ensure_ascii=False, indent=2))
        return 0

    if args.action == "verify":
        from scripts.trace_bridge_verify import FAIL, gather, render

        current = all((target / name).is_file()
                      and (target / name).read_bytes() == (SOURCE / name).read_bytes()
                      for name in PLUGIN_FILES)
        checks = gather(config_path, target, status(config, load_path),
                        files_current=current, batch=args.batch)
        print(render(checks))
        return 1 if any(check.level == FAIL for check in checks) else 0

    if args.action == "install":
        token = _setting("BENCH_COLLECTOR_CLIENT_TOKEN")
        if not token:
            sys.exit("BENCH_COLLECTOR_CLIENT_TOKEN 没配：插件投递不了绑定，装了也只会记本地日志")
        port = _setting("BENCH_COLLECTOR_PORT") or "3312"
        bind = _setting("BENCH_COLLECTOR_BIND") or "127.0.0.1"
        target.mkdir(parents=True, exist_ok=True)
        for name in PLUGIN_FILES:
            shutil.copy2(SOURCE / name, target / name)
        backup = _save(config_path, install(config, load_path, url=f"http://{bind}:{port}",
                                            token=token))
    else:
        backup = _save(config_path, uninstall(config, load_path))
        for name in PLUGIN_FILES:
            (target / name).unlink(missing_ok=True)

    print(f"已{'安装' if args.action == 'install' else '卸载'}；openclaw.json 备份：{backup}")
    print("重启 YonWork 后生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
