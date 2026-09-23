from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..db import load_env_file, project_root


# 固定端口，不用临时端口。产品配置里存的是 URL，端口每次变就得每轮重配产品，
# 而 `docs/history/model-entry-validation.md` 已经写明：正式实现应固定入口并检查就绪，
# 不在测量轮次中反复新建账户。
DEFAULT_PORT = 3312

# 避开已占用的：YonWork 3211、NewAPI 3000、MySQL 3307、Web 8000、CDP 9222。
_RESERVED_PORTS = {3000, 3211, 3307, 8000, 9222}


class CollectorConfigError(RuntimeError):
    pass


def _setting(name: str, default: str = "") -> str:
    """先环境变量后 `.env`，口径同 `NewApiConfig.load` 和 `BENCH_WORKBUDDY_*`。

    容器 Worker 的配置由 Compose 注入环境变量，宿主机 Worker 和 CLI 只有 `.env`。
    只读 `os.environ` 的话 `.env` 里写的值**会被静默忽略**，
    表现成「明明配了还是没生效」——这个坑 2026-09-21 已经踩过一次。
    """
    found = os.environ.get(name, "").strip()
    if found:
        return found
    return load_env_file(project_root() / ".env").get(name, "").strip()


def _flag(name: str, default: bool) -> bool:
    raw = _setting(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise CollectorConfigError(f"{name} 只认 0/1/true/false，收到：{raw}")


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """采集代理的配置。**默认关闭**。

    关着的时候跑批行为和今天完全一样（三来源事后采集），
    所以「回退」就是把 `BENCH_COLLECTOR_ENABLED` 设回 0——
    ⚠️ 但**只改这里不够**：产品配置里的 baseUrl 还指着我们，
    得同时把它改回直连 NewAPI，否则产品会打到一个没人监听的端口。
    """

    enabled: bool = False
    # auto（默认）/ service / embedded。
    # auto：入口已经有人在服务就用那个常驻服务，否则跑批自己起一个。
    # 常驻服务的好处是**不跑批时入口也活着**——否则手动在产品里选这个模型
    # 会直接报「模型服务暂时不可用」，2026-09-22 踩过。
    mode: str = "auto"
    bind: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    upstream_url: str = "http://127.0.0.1:3000"
    upstream_key: str = field(default="", repr=False)
    client_token: str = field(default="", repr=False)
    ledger_dir: Path = Path("results")
    timeout_seconds: float = 300.0

    @classmethod
    def load(cls) -> "CollectorConfig":
        enabled = _flag("BENCH_COLLECTOR_ENABLED", False)
        raw_port = _setting("BENCH_COLLECTOR_PORT") or str(DEFAULT_PORT)
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise CollectorConfigError(f"BENCH_COLLECTOR_PORT 不是整数：{raw_port}") from exc
        if port in _RESERVED_PORTS:
            raise CollectorConfigError(
                f"BENCH_COLLECTOR_PORT={port} 和已占用端口冲突"
                f"（{sorted(_RESERVED_PORTS)}），换一个"
            )
        raw_timeout = _setting("BENCH_COLLECTOR_TIMEOUT_SECONDS") or "300"
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise CollectorConfigError(
                f"BENCH_COLLECTOR_TIMEOUT_SECONDS 不是数字：{raw_timeout}"
            ) from exc
        mode = (_setting("BENCH_COLLECTOR_MODE") or "auto").lower()
        if mode not in ("auto", "service", "embedded"):
            raise CollectorConfigError(
                f"BENCH_COLLECTOR_MODE 只认 auto/service/embedded，收到：{mode}")
        config = cls(
            enabled=enabled,
            mode=mode,
            bind=_setting("BENCH_COLLECTOR_BIND") or "127.0.0.1",
            port=port,
            upstream_url=(
                _setting("BENCH_COLLECTOR_UPSTREAM")
                or _setting("NEWAPI_BASE_URL")
                or "http://127.0.0.1:3000"
            ).rstrip("/"),
            upstream_key=_setting("BENCH_COLLECTOR_UPSTREAM_KEY"),
            client_token=_setting("BENCH_COLLECTOR_CLIENT_TOKEN"),
            ledger_dir=Path(_setting("BENCH_COLLECTOR_LEDGER_DIR") or "results"),
            timeout_seconds=timeout,
        )
        if enabled:
            config.require_credentials()
        return config

    def require_credentials(self) -> None:
        """开了却没配凭据要**当场报错**。

        少了 `upstream_key` 会让每一轮都收到网关的 401，看起来像产品坏了；
        少了 `client_token` 则等于这个入口对本机任何进程敞开。
        两种都是「静默变成脏数据」，不能等跑批时才发现。
        """
        missing = [
            name
            for name, value in (
                ("BENCH_COLLECTOR_UPSTREAM_KEY", self.upstream_key),
                ("BENCH_COLLECTOR_CLIENT_TOKEN", self.client_token),
            )
            if not value
        ]
        if missing:
            raise CollectorConfigError(
                f"BENCH_COLLECTOR_ENABLED=1 但缺少 {'、'.join(missing)}；"
                "填进 .env 后重启 Worker"
            )

    @property
    def resolved_ledger_dir(self) -> Path:
        root = self.ledger_dir
        return root if root.is_absolute() else project_root() / root

    @property
    def control_url(self) -> str:
        return f"http://{self.bind}:{self.port}/_control"

    @property
    def entry_url(self) -> str:
        """配进产品的 baseUrl。产品自己会在后面接 `/chat/completions`。"""
        return f"http://{self.bind}:{self.port}/v1"

    @property
    def health_url(self) -> str:
        return f"http://{self.bind}:{self.port}/healthz"

    def ledger_path(self, batch_id: str) -> Path:
        return self.resolved_ledger_dir / batch_id / "model-requests.jsonl"
