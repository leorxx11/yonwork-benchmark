from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from ..models import ChatTurn, UsageSample


class DriverError(RuntimeError):
    """驱动自己的问题：前置检查不过、进程起不来、运行时文件找不到。

    **不是产品的问题。** 归类由 assertions 决定，这里只负责把「工具坏了」
    和「产品坏了」在类型上分开——混了统计数据就脏了（CLAUDE.md 二-4）。
    """


@dataclass(frozen=True, slots=True)
class UsageCollection:
    """一轮的用量采集结果。

    `notes` 是采集失败的留痕。**采不到不等于产品出错**，所以它只进
    `RunRecord.note`，不参与判定——这和 `LogStats` 缺数据时用 None 而不是 0
    是同一条规矩。
    """

    samples: tuple[UsageSample, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DriverSpec:
    """构造一个 Driver 所需的全部参数，与具体实现无关。

    Web 任务、CLI 参数和测试三个入口都收敛到这一个形状，
    免得每加一个产品就要在三处各改一遍构造代码。
    """

    product: str = "yonwork"
    agent_id: str = "main"
    model_query: str = ""
    timeout_seconds: float = 600.0
    transcript_dir: Path | None = None
    # 允许被测产品调用工具。**批次级，不是 Case 级**——
    # 驱动一个批次只构造一次，而且 YonWork 根本没有这个开关
    # （工具由智能体配置决定）。所以这是「这一批让不让用工具」，
    # 只有 WorkBuddy 真的消费它；要求某个 Case 必须用到工具，
    # 靠的是 Expectations.min_tool_calls，两者分工不同。
    allow_tools: bool = False


@runtime_checkable
class Driver(Protocol):
    """一个被测产品的驱动。**只收原材料，不下判断。**

    这条边界是 CLAUDE.md 二-3：判定逻辑全部留在 `assertions/`，可单测、可 diff。
    任何想在 Driver 里写 `if ... : return Verdict.FAIL` 的改动都要拦下来。

    两个实现的形状差得很远，接口按**两边都成立**的部分写：

    | | YonWork | WorkBuddy |
    |---|---|---|
    | 通路 | 常驻 Host API + SSE | 每轮一个一次性进程 + JSON |
    | 隔离手段 | 全新 `sessionKey` | 全新 `--session-id` + `--no-session-persistence` |
    | 终止判定 | `chat.complete` / `state:"final"` | 最后一条 `type:"result"` |
    | 用量 | 事后三来源采集 | 随本轮输出一起回来 |

    所以接口里没有「端点」「SSE」「进程」这些只有一边成立的概念。
    """

    product: str

    def preflight(self) -> Iterable[str]:
        """跑批前的前置检查。不通过就抛 `DriverError`。

        返回给人看的日志行（端点地址、选中的模型……）。调用方负责打印，
        这样 Web 和 CLI 两个入口的前置日志是同一份，不会各写各的。
        """
        ...

    def session_key(self, benchmark_id: str) -> str:
        """本轮的隔离标识，落到 `RunRecord.session_key`。

        **每轮必须全新。** 复用会让本轮看见上一轮的上下文，
        数据静默作废且不报错——这是最难查的一类污染。
        """
        ...

    def run_turn(self, *, benchmark_id: str, prompt: str) -> ChatTurn:
        """跑完一轮，返回原材料。失败时抛异常，并尽量在 `.partial` 上挂半成品。"""
        ...

    def collect_usage(self, turn: ChatTurn) -> UsageCollection:
        """采这一轮的 token 用量。采不到就返回空，**不要用 0 冒充**。"""
        ...

    def enrich(self, turn: ChatTurn) -> ChatTurn:
        """跑完之后从第二来源补齐本轮原材料，补不到就原样返回。

        **为什么需要这一步**：有的产品主通路看不到全部原材料。实测
        YonWork 的 `/api/chat/send` SSE **只有 text 块，没有工具调用**，
        而同一轮的会话 JSONL 里有 `toolCall`——不补的话 `tool_calls` 恒为 0，
        「这个 Case 必须用到工具」的断言会稳定误判成「产品没调工具」，
        把我们自己的观测盲区算成产品的失败。

        ⚠️ 仍然**只搬运不判定**（二-3）：这里只负责把原材料补全，
        补不到就照实留空，由 assertions 去决定那算不算问题。
        """
        ...

    def close(self) -> None:
        """释放驱动持有的资源。没有资源的实现留空即可。"""
        ...
