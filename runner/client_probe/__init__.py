"""客户端体验探针：CDP + DOM，只测 UI 本身。

和 `runner/` 主链路**并行存在，不合并**：
主链路测模型 / Agent / token / 成本，这里测「用户什么时候看到什么」。
见 CLAUDE.md 七-第 2 批。
"""

from .probe import ProbeRecord, probe_once, watch_once

__all__ = ["ProbeRecord", "probe_once", "watch_once"]
