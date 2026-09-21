"""探针唯一允许驱动 UI 的地方。**改这个文件前先读完这段。**

CLAUDE.md 八：不要写 UI 自动化（pywinauto / PAD / 坐标点击）。
这里**不是**那种东西，区别是实打实的：

| | PAD / pywinauto | 这里 |
|---|---|---|
| 定位 | 屏幕坐标 / 控件树 | `data-testid` 语义选择器 |
| 通道 | 模拟系统输入事件 | CDP 里调元素自己的 `.click()` |
| 改版后果 | 坐标错位，**静默点错东西** | 选择器找不到，**当场报错** |
| 范围 | 无边界，会一路长成完整流程 | 白名单，见下 |

**白名单就是全部允许的动作。** 想加第四个动作的时候停一下：
「少量驱动 UI」不写死范围，就是当年 PAD 的起点。prompt 一律由 Host API 投递，
这里**不打字、不碰剪贴板、不选模型、不点发送**。
"""

from __future__ import annotations

import asyncio
import json

from .cdp import ProbeError, Session


# 允许的动作。加动作要同时改这里和 CLAUDE.md 的 UI 驱动范围那条。
ALLOWED_ACTIONS = ("open-session",)

SESSION_ITEM = '[data-testid="sidebar-session-item"]'

# 点侧边栏里标题匹配的那条会话。
#
# 按**标题**找，不按位置/索引——实测教训：只要锚点依赖位置，
# 换个会话列表就会悄悄失效（探针早先用 `turns().slice(26)` 就栽过）。
# 标题来自我们自己发的那一轮 prompt，所以调用方保证它唯一即可。
_OPEN_SESSION_JS = r"""
((title) => {
  const items = [...document.querySelectorAll('[data-testid="sidebar-session-item"]')];
  const titles = items.map(e => (e.innerText || '').trim().split('\n')[0]);
  const hit = items.find(e => (e.innerText || '').includes(title));
  if (!hit) return {ok: false, why: 'no-match', titles};
  if (hit.dataset.navSelected === 'true') return {ok: true, already: true};
  // 真正带 role=button 的是里面那层，外层只是容器。
  (hit.querySelector('[role="button"]') || hit).click();
  return {ok: true, already: false};
})(__TITLE__)
"""

_VERIFY_JS = r"""
((title) => {
  const items = [...document.querySelectorAll('[data-testid="sidebar-session-item"]')];
  const sel = items.find(e => e.dataset.navSelected === 'true');
  const root = document.querySelector('.yonclaw-chat-scroll-region');
  return {
    selected: sel ? (sel.innerText || '').trim().split('\n')[0] : null,
    matches: !!sel && (sel.innerText || '').includes(title),
    chatRootPresent: !!root,
  };
})(__TITLE__)
"""


async def open_session(session: Session, title: str, *, timeout: float = 6.0) -> dict:
    """在侧边栏点开标题匹配 `title` 的会话，并确认真的切过去了。

    **为什么非点不可**：Host API 没有任何「切换当前会话」的路由，
    renderer 也没有 `/chat/:sessionKey` 路由——会话是 React state 选的
    （全量查证见 CLAUDE.md 七-1.1）。而界面只渲染**它当前打开的那个会话**，
    所以不切过去，API 发的轮次在界面上根本不出现，探针采到的是零事件。

    ⚠️ **点「新建任务」没用**，2026-09-21 实测：它只把界面导航到一个
    还不存在的会话，API 往新 sessionKey 发过去照样不渲染（turns 恒为 0）。
    必须是先有会话、再点它。
    """
    marker = json.dumps(title, ensure_ascii=False)
    result = await session.evaluate(_OPEN_SESSION_JS.replace("__TITLE__", marker))
    if not result.get("ok"):
        raise ProbeError(
            f"侧边栏里没有标题含 {title!r} 的会话；当前列表：{result.get('titles')}"
        )

    # 切换是 React 重渲染，给它几帧；**必须核对切中的是不是那一条**，
    # 不核对的话点歪了会安安静静地把别的会话当成目标，测出来全是脏数据。
    deadline = asyncio.get_running_loop().time() + timeout
    state: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
        state = await session.evaluate(_VERIFY_JS.replace("__TITLE__", marker))
        if state.get("matches") and state.get("chatRootPresent"):
            return state
    raise ProbeError(
        f"点了但没切过去：当前选中 {state.get('selected')!r}、"
        f"chatRoot={state.get('chatRootPresent')}（选择器可能随客户端升版失效了）"
    )
