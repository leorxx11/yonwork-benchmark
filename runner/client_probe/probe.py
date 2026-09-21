from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..client import ChatClient
from ..discovery import HostEndpoint, discover
from ..models import ChatTurn, now_iso
from .cdp import ProbeError, Session, attach


OBSERVER_JS = (Path(__file__).parent / "observer.js").read_text(encoding="utf-8")


@dataclass
class ProbeRecord:
    """一轮客户端体验测量。

    **所有 UI 时间点都是 renderer 自己的 performance.now() 毫秒**，同一时钟内相减。
    Host API 侧只取时长（与时钟无关），不取绝对时间戳——
    实测 Windows 比 WSL 快 7.85s，跨端相减会把 0.5s 的量级淹掉。
    """

    benchmark_id: str
    scenario: str
    started_at: str
    # 开跑时会话里已有的轮数。**这是协变量，不是噪声**：
    # DOM 里挂着的历史越多，渲染成本越高（场景表里的 S7）。
    # 滞后指标本身不受影响——两边测的是同一轮同一上下文，差值仍是客户端开销。
    history_turns: int | None = None
    # ---- UI 侧，renderer 时钟，相对 t0 的毫秒 ----
    user_message_ms: float | None = None      # T1
    assistant_created_ms: float | None = None  # T2
    first_content_dom_ms: float | None = None  # T3
    first_visible_frame_ms: float | None = None
    last_change_ms: float | None = None        # T4
    generation_done_ms: float | None = None    # T5：界面「正在生成」消失，确定性信号
    text_updates: int = 0
    answer_chars: int | None = None
    # ---- Host API 侧，只用时长 ----
    backend_first_delta_ms: float | None = None
    backend_duration_ms: float | None = None
    terminated_by: str | None = None
    # ---- 派生 ----
    t0_roundtrip_ms: float | None = None  # 打 T0 那次 CDP 往返，测量不确定度
    notes: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ui_lag_first_ms(self) -> float | None:
        """首字滞后 = UI 首段文本 - 后端首字。两边都相对同一个 T0。"""
        if self.first_content_dom_ms is None or self.backend_first_delta_ms is None:
            return None
        return round(self.first_content_dom_ms - self.backend_first_delta_ms, 1)

    @property
    def ui_lag_complete_ms(self) -> float | None:
        # T5 优先用「正在生成」消失那个确定性信号，没有再退回最后一次文本变化。
        ui_done = self.generation_done_ms if self.generation_done_ms is not None else self.last_change_ms
        if ui_done is None or self.backend_duration_ms is None:
            return None
        return round(ui_done - self.backend_duration_ms, 1)

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["ui_lag_first_ms"] = self.ui_lag_first_ms
        data["ui_lag_complete_ms"] = self.ui_lag_complete_ms
        return data


def _relative(events: list[dict[str, Any]], name: str, t0: float) -> float | None:
    for event in events:
        if event.get("name") == name:
            return round(event["t"] - t0, 1)
    return None


async def _run(
    benchmark_id: str,
    prompt: str,
    scenario: str,
    session_key: str,
    endpoint: HostEndpoint,
    timeout_seconds: float,
) -> ProbeRecord:
    record = ProbeRecord(benchmark_id=benchmark_id, scenario=scenario, started_at=now_iso())

    async with attach() as session:
        # 把 prompt 前若干字注入观察器，用来排除用户自己那条气泡。
        head = json.dumps(prompt.strip()[:40], ensure_ascii=False)
        status = await session.evaluate(OBSERVER_JS.replace("__PROMPT_HEAD__", head))
        if status != "ok":
            raise ProbeError(f"观察器没装上：{status}")

        # 快速失败：界面没开着会话就别发了，否则白烧一轮 token 才发现采不到。
        # `agent:main:main` 是固定且永远累积的服务端会话，**点「新建任务」不会重置它**，
        # 只会把界面导航到一个还不存在的新会话——那时发过去的轮次界面根本不渲染。
        installed = next((e for e in session.events if e.get("name") == "t0"), {})
        record.history_turns = installed.get("baseline")
        if not installed.get("chatRootPresent"):
            raise ProbeError(
                "界面当前没有打开任何会话（首页状态），这一轮发出去也不会渲染。"
                "请在侧边栏点开目标会话再跑——不要点「新建任务」。"
            )

        # T0 在 renderer 内取，和后续所有 UI 时间点同一个时钟。
        loop = asyncio.get_running_loop()
        before = loop.time()
        t0 = await session.evaluate("performance.now()")
        after = loop.time()
        record.t0_roundtrip_ms = round((after - before) * 1000, 1)

        client = ChatClient(endpoint, timeout_seconds=timeout_seconds)
        turn: ChatTurn = await asyncio.to_thread(
            client.send, benchmark_id=benchmark_id, prompt=prompt, session_key=session_key
        )
        record.backend_first_delta_ms = (
            round(turn.first_delta_seconds * 1000, 1) if turn.first_delta_seconds else None
        )
        record.backend_duration_ms = round(turn.duration_seconds * 1000, 1)
        record.terminated_by = turn.terminated_by

        # 后端已完成，再给 UI 一点收尾时间。T5 暂以「后端完成后 UI 最后一次变化」为准，
        # 这样即使找不到确定性的 DOM 完成标记，指标依然良定义。
        await session.drain(3.0)

        events = session.events
        record.events = events
        record.user_message_ms = _relative(events, "user-message", t0)
        record.assistant_created_ms = _relative(events, "assistant-created", t0)
        record.first_content_dom_ms = _relative(events, "first-content-dom", t0)
        record.first_visible_frame_ms = _relative(events, "first-visible-frame", t0)
        record.generation_done_ms = _relative(events, "generation-done", t0)
        updates = [e for e in events if e.get("name") == "text-update"]
        record.text_updates = len(updates)
        if updates:
            record.last_change_ms = round(updates[-1]["t"] - t0, 1)
            record.answer_chars = updates[-1].get("chars")
        await session.evaluate("window.__probeStop && window.__probeStop()")

    # 自检。宁可显性报废一轮，也不要把假数据混进统计——
    # 第一次跑就踩过：观察器抓到了会话里遗留的上一轮气泡，算出 -1794ms 的首字滞后。
    if record.first_content_dom_ms is None:
        record.notes.append("没采到首段文本：选择器可能已失效，别把这轮当 0 用")
    lag = record.ui_lag_first_ms
    if lag is not None and lag < 0:
        record.notes.append(
            f"首字滞后为负（{lag}ms）：UI 不可能早于后端出字，"
            "多半抓到了本轮之前的气泡。这轮作废"
        )
    if record.first_visible_frame_ms is None and record.first_content_dom_ms is not None:
        record.notes.append("没拿到 first_visible_frame：窗口可能被遮挡，rAF 被节流")
    return record


def watch_once(
    *,
    seconds: float = 180.0,
    label: str = "manual",
) -> ProbeRecord:
    """**模拟用户**：人在界面上手动发一条，探针只观察，不发请求。

    这才是 POC 页最初要的那个数——「点击发送后多久看到回复」。
    所有时间点改成相对 **T1（用户气泡出现）**，那是用户感知的起点；
    真正的点击时刻 T0 在 DOM 里看不到，差的就是输入框到气泡那一小段。
    """

    async def _watch() -> ProbeRecord:
        record = ProbeRecord(benchmark_id=label, scenario="watch", started_at=now_iso())
        async with attach() as session:
            status = await session.evaluate(OBSERVER_JS.replace("__PROMPT_HEAD__", "null"))
            if status != "ok":
                raise ProbeError(f"观察器没装上：{status}")
            installed = next((e for e in session.events if e.get("name") == "t0"), {})
            record.history_turns = installed.get("baseline")
            if not installed.get("chatRootPresent"):
                raise ProbeError("界面没打开会话，先点开一个会话再 watch")
            print(f"观察器已就位（会话已有 {record.history_turns} 轮）。"
                  f"请现在在 YonWork 里手动发一条消息，{seconds:g}s 内有效…", flush=True)
            # 等到界面标记生成完成就收工，不必耗满整个窗口。
            await session.drain_until(seconds, stop_on="generation-done", grace=1.5)
            events = session.events
            record.events = events
            t1 = next((e["t"] for e in events if e["name"] == "user-message"), None)
            if t1 is None:
                raise ProbeError(f"{seconds:g}s 内没观察到你发消息")
            record.user_message_ms = 0.0
            record.assistant_created_ms = _relative(events, "assistant-created", t1)
            record.first_content_dom_ms = _relative(events, "first-content-dom", t1)
            record.first_visible_frame_ms = _relative(events, "first-visible-frame", t1)
            record.generation_done_ms = _relative(events, "generation-done", t1)
            updates = [e for e in events if e.get("name") == "text-update"]
            record.text_updates = len(updates)
            if updates:
                record.last_change_ms = round(updates[-1]["t"] - t1, 1)
                record.answer_chars = updates[-1].get("chars")
            await session.evaluate("window.__probeStop && window.__probeStop()")
        return record

    return asyncio.run(_watch())


def probe_once(
    *,
    benchmark_id: str,
    prompt: str,
    scenario: str = "S1",
    session_key: str = "agent:main:main",
    endpoint: HostEndpoint | None = None,
    timeout_seconds: float = 180.0,
) -> ProbeRecord:
    return asyncio.run(
        _run(benchmark_id, prompt, scenario, session_key, endpoint or discover(), timeout_seconds)
    )
