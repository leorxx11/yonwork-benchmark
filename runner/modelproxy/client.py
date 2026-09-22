from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from ..transport import build_opener
from .config import CollectorConfig
from .ledger import ModelRequestRecord
from .proxy import CollectorProxy, ProxyError


class RemoteCollector:
    """连到**常驻**采集服务的客户端。

    接口形状和 `CollectorProxy` 一样（`register` / `close_run` / `records_for` /
    `ledger_path` / `stop`），所以 `batch.run_batch` 完全不用区分这一轮的代理
    是自己起的还是别人常驻的。

    **为什么要有常驻模式**（2026-09-22 改的）：原来代理跟着跑批进程起停，
    不跑批时入口是死的——在产品界面里手动选这个模型会直接报
    「模型服务暂时不可用」，而且这个报错看起来像产品坏了。
    常驻之后入口一直在，手动用和跑批用同一个地址。

    代价是多了一个可能挂掉的依赖。所以 `start()` 会先查健康，
    **连不上就抛错不跑批**——宁可不开始，也不要跑出一批「产品好像全挂了」的数据。
    """

    def __init__(self, config: CollectorConfig, *, batch_id: str, timeout: float = 15.0) -> None:
        self._config = config
        self._batch_id = batch_id
        self._timeout = timeout
        self._ledger_path = config.ledger_path(batch_id)

    # ---- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        if not self.healthy(self._config, timeout=self._timeout):
            raise ProxyError(
                f"采集服务不可达：{self._config.health_url}。"
                "用 `docker compose up -d collector` 起它，"
                "或把 BENCH_COLLECTOR_MODE 设成 embedded 让跑批自己起一个"
            )

    def stop(self) -> None:
        """常驻服务不归我们管，不停它。"""

    @staticmethod
    def healthy(config: CollectorConfig, *, timeout: float = 5.0) -> bool:
        try:
            with build_opener().open(config.health_url, timeout=timeout) as response:
                return bool(json.loads(response.read()).get("ok"))
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # ---- 轮次 -----------------------------------------------------------

    def register(self, *, run_id: str, product: str) -> None:
        self._call("POST", "/runs", {"run_id": run_id, "product": product,
                                     "batch_id": self._batch_id})

    def close_run(self, run_id: str) -> None:
        # 收尾失败不该拖垮这一轮：轮次已经跑完了，归属标记晚一点只影响
        # 之后的迟到请求归哪一轮，不影响已经记下的东西。
        try:
            self._call("POST", f"/runs/{run_id}/close", {})
        except ProxyError:
            pass

    def records_for(self, run_id: str) -> tuple[ModelRequestRecord, ...]:
        try:
            payload = self._call("GET", f"/runs/{run_id}", None)
        except ProxyError:
            return ()
        return tuple(
            ModelRequestRecord.from_json(item) for item in payload.get("requests") or []
        )

    @property
    def ledger_path(self) -> Path:
        """**本机视角**的账本路径。

        服务可能跑在容器里，它自己报的是容器内路径（`/app/results/...`），
        入库时按那个路径去读会读不到，于是未归属请求会悄悄丢掉。
        两边的相对布局是一样的（`<ledger_dir>/<batch_id>/model-requests.jsonl`，
        靠 bind mount 指向同一个文件），所以这里一律用本机配置算出来的那个。
        """
        return self._ledger_path

    # ---- 传输 -----------------------------------------------------------

    def _call(self, method: str, path: str, payload: dict | None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self._config.control_url + path,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._config.client_token}",
            },
        )
        try:
            with build_opener().open(request, timeout=self._timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise ProxyError(f"采集服务 {method} {path} 返回 {exc.code}：{detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProxyError(f"采集服务 {method} {path} 失败：{type(exc).__name__}") from exc


def build_collector(config: CollectorConfig, *, batch_id: str):
    """按配置挑一个采集器；关着就返回 None。

    `auto` 是默认：入口已经有常驻服务就用它，否则自己起一个。
    这样装了 docker 的机器无感用常驻服务，纯 CLI 的机器也照常能跑。
    """
    if not config.enabled:
        return None
    if config.mode != "embedded" and RemoteCollector.healthy(config):
        remote = RemoteCollector(config, batch_id=batch_id)
        remote.start()
        return remote
    if config.mode == "service":
        # 显式要求用服务却连不上，**不能静默回落**去抢端口——
        # 那会把「服务没起来」变成「端口冲突」，报错指向完全错误的地方。
        RemoteCollector(config, batch_id=batch_id).start()
    from .ledger import LedgerWriter

    local = CollectorProxy.from_config(
        config, ledger=LedgerWriter(config.ledger_path(batch_id)), proxy_id=batch_id
    )
    local.start()
    return local
