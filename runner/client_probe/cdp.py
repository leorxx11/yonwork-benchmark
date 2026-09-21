from __future__ import annotations

import json
import urllib.request
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import websockets


CDP_URL = "http://127.0.0.1:9222"
BINDING = "__probeEvent"


class ProbeError(RuntimeError):
    pass


def main_renderer(cdp_url: str = CDP_URL) -> dict[str, Any]:
    """挑 YonWork 主 renderer。

    **必须同时看 type 和 title**：内嵌浏览器会多出 `type=webview` 的 target
    （实测打开文库时就有一个），只按 type=="page" 挑会挑错。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开 http_proxy
    try:
        targets = json.loads(opener.open(f"{cdp_url}/json", timeout=5).read())
    except OSError as exc:
        raise ProbeError(f"连不上 CDP {cdp_url}：{exc}（YonWork 在跑吗）") from exc
    for target in targets:
        if target.get("type") == "page" and target.get("title") == "YonWork":
            return target
    kinds = [(t.get("type"), t.get("title")) for t in targets]
    raise ProbeError(f"没找到 YonWork 主 renderer，当前 targets：{kinds}")


class Session:
    """极薄的 CDP 会话。只做 evaluate + binding，不做别的。"""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next = 0
        self.events: list[dict[str, Any]] = []

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next += 1
        message_id = self._next
        await self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            raw = json.loads(await self._ws.recv())
            if raw.get("method") == "Runtime.bindingCalled":
                self.events.append(json.loads(raw["params"]["payload"]))
                continue
            if raw.get("id") == message_id:
                if "error" in raw:
                    raise ProbeError(f"{method} 失败：{raw['error']}")
                return raw.get("result")

    async def evaluate(self, expression: str) -> Any:
        result = await self._call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            raise ProbeError(f"注入的 JS 抛异常：{result['exceptionDetails'].get('text')}")
        return result["result"].get("value")

    async def add_binding(self, name: str = BINDING) -> None:
        await self._call("Runtime.addBinding", {"name": name})

    async def drain_until(
        self, seconds: float, *, stop_on: str, grace: float = 1.5
    ) -> None:
        """收事件，直到出现 `stop_on` 再多收 grace 秒；最多等 seconds。"""
        import asyncio

        deadline = asyncio.get_running_loop().time() + seconds
        hit_at: float | None = None
        while True:
            loop = asyncio.get_running_loop()
            remaining = deadline - loop.time()
            if hit_at is not None:
                remaining = min(remaining, hit_at + grace - loop.time())
            if remaining <= 0:
                return
            try:
                async with asyncio.timeout(remaining):
                    raw = json.loads(await self._ws.recv())
            except TimeoutError:
                return
            if raw.get("method") == "Runtime.bindingCalled":
                event = json.loads(raw["params"]["payload"])
                self.events.append(event)
                if event.get("name") == stop_on and hit_at is None:
                    hit_at = asyncio.get_running_loop().time()

    async def drain(self, seconds: float) -> None:
        """收 binding 事件直到超时。事件驱动，不轮询 DOM。"""
        import asyncio

        try:
            async with asyncio.timeout(seconds):
                while True:
                    raw = json.loads(await self._ws.recv())
                    if raw.get("method") == "Runtime.bindingCalled":
                        self.events.append(json.loads(raw["params"]["payload"]))
        except TimeoutError:
            return


@asynccontextmanager
async def attach(cdp_url: str = CDP_URL) -> AsyncIterator[Session]:
    target = main_renderer(cdp_url)
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
        session = Session(ws)
        await session._call("Runtime.enable")
        await session.add_binding()
        yield session
