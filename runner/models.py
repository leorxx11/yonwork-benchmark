from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


JsonObject = dict[str, Any]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class Verdict(str, Enum):
    """CLAUDE.md 二-4 的失败分类，PASS 之外的四类不能混在一起统计。"""

    PASS = "Pass"
    FAIL = "Fail"        # 产品的问题
    TIMEOUT = "Timeout"  # 超时
    ERROR = "Error"      # 工具/流程自己的问题
    INVALID = "Invalid"  # 数据无意义（如请求根本没到 API）


# 越靠后越“严重”：数据无意义时其它判定都失去意义，所以 Invalid 压倒一切；
# 工具自己坏了（Error）也压过产品判定，否则会把自己的 bug 记成产品缺陷。
_SEVERITY: dict[Verdict, int] = {
    Verdict.PASS: 0,
    Verdict.FAIL: 1,
    Verdict.TIMEOUT: 2,
    Verdict.ERROR: 3,
    Verdict.INVALID: 4,
}

EXIT_CODES: dict[Verdict, int] = {
    Verdict.PASS: 0,
    Verdict.FAIL: 1,
    Verdict.TIMEOUT: 2,
    Verdict.ERROR: 3,
    Verdict.INVALID: 4,
}


def worst(verdicts: list[Verdict]) -> Verdict:
    return max(verdicts, key=lambda item: _SEVERITY[item], default=Verdict.PASS)


class Layer(str, Enum):
    """CLAUDE.md 二-5 的断言五层。"""

    COMPLETION = "Completion"
    ARTIFACT = "Artifact"
    LOG = "Log"
    CONTENT = "Content"
    COST = "Cost"


@dataclass(frozen=True, slots=True)
class Expectations:
    """每个 Case 的弱断言参数，全部可选；来自提示词表的额外列。"""

    expect_keywords: tuple[str, ...] = ()
    forbid_keywords: tuple[str, ...] = ()
    min_length: int = 1
    json_parsable: bool = False
    max_seconds: float | None = None
    max_total_tokens: int | None = None
    # 按 Case 定，不要指望一个全局常数：同一句「你好！」实测 5,514～16,238，
    # 长文本用例同一轮 session-jsonl 记 30,082、NewAPI 记 228,354。
    max_input_tokens: int | None = None
    # 这个 Case 至少要调几次工具。None = 只记录不判定（默认）。
    # 声明了才判，规矩同 max_input_tokens——**不声明就不猜**。
    # 探针 S5/S6 就是栽在这里：以为在测工具场景，实测 tool_calls 全是 0，
    # 模型压根没调工具，两个普通短文本场景被当成了「工具场景没问题」的证据。
    min_tool_calls: int | None = None


@dataclass(frozen=True, slots=True)
class CaseDefinition:
    case_name: str
    prompt: str
    runs: int
    enabled: bool
    expectations: Expectations = Expectations()


@dataclass(frozen=True, slots=True)
class TaskItem:
    position: int
    case_name: str
    run_no: int
    prompt: str
    expectations: Expectations = Expectations()


def expand_cases(cases: list[CaseDefinition]) -> list[TaskItem]:
    items: list[TaskItem] = []
    for case in cases:
        if not case.enabled:
            continue
        for run_no in range(1, case.runs + 1):
            items.append(
                TaskItem(
                    position=len(items),
                    case_name=case.case_name,
                    run_no=run_no,
                    prompt=case.prompt,
                    expectations=case.expectations,
                )
            )
    return items


_UNSAFE_ID_CHARS = re.compile(r"[^0-9A-Za-z_.-]+")


def benchmark_id_for(prefix: str, case_name: str, run_no: int, stamp: str) -> str:
    """BenchmarkId 直接当 idempotencyKey 用，所以必须唯一且不含空白。

    服务端对 idempotencyKey 无条件 .trim()，两端空白会被悄悄吃掉，
    这里先把不安全字符压成 '-'，避免 runId 与本地记录对不上。
    """
    safe_case = _UNSAFE_ID_CHARS.sub("-", case_name).strip("-") or "case"
    safe_prefix = _UNSAFE_ID_CHARS.sub("-", prefix).strip("-") or "bench"
    return f"{safe_prefix}-{safe_case}-r{run_no}-{stamp}"


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """一轮对话的**原材料**。这里不做任何判定。"""

    benchmark_id: str
    session_key: str
    prompt: str
    started_at: str
    ended_at: str
    duration_seconds: float
    run_id: str | None = None
    answer: str | None = None
    first_delta_seconds: float | None = None
    # CLI 自报的内部耗时，计时边界尚未校准，不能称为纯模型耗时。
    # 外层减内部仅是两个计时口径的差值，不能全部归因于冷启动。
    # YonWork 没有对应自报字段，留空；字段名保留以兼容历史数据。
    engine_seconds: float | None = None
    terminated_by: str | None = None
    stop_reason: str | None = None
    final_state: str | None = None
    event_counts: dict[str, int] = field(default_factory=dict)
    tool_calls: tuple[str, ...] = ()
    tool_calls_status: str = "unavailable"  # observed / unavailable / error
    tool_calls_source: str = "unavailable"
    tool_calls_detail: str = ""
    requested_model: str | None = None  # 请求里指定的 modelId，None = 用智能体默认
    requested_model_label: str | None = None  # 该模型的显示名，四模式视图按它分组
    # 请求里指定的 providerAccountId。**光比模型名不够**：
    # 同一个模型名可以由多个 provider 提供（官方默认和我们的代理都叫 deepseek-flash），
    # 只比名字的话「跑错通路」看不出来——2026-09-22 就是这么白跑了一列。
    requested_provider: str | None = None
    # 这一轮**允许**用工具吗。None = 该产品没有这个开关（YonWork 就是，
    # 工具由智能体配置决定，我们这边关不掉）。
    # 用来区分「模型自己选择不调工具」（产品的问题 → Fail）和
    # 「我们把工具关了却跑了个需要工具的 Case」（数据无意义 → Invalid）。
    tools_enabled: bool | None = None
    stream_error: JsonObject | None = None
    http_status: int | None = None
    transcript_path: str | None = None


def observed_tool_count(turn: JsonObject) -> int | None:
    """空列表只有在完整采集时才代表 0；旧记录里的非空列表仍是正向证据。"""
    calls = turn.get("tool_calls")
    if isinstance(calls, list) and (calls or turn.get("tool_calls_status") == "observed"):
        return len(calls)
    return None


# 同一轮的 token 可能有多个来源，谁都可能缺：
# 端上 HTTP 端点实测会漏记，NewAPI 只记走它的那些轮，会话 JSONL 端上本地最全。
# 断言取用时按这个顺序优先。
#
# 这张表**跨产品**：前三个是 YonWork 的，`workbuddy-cli` 是 WorkBuddy 的
#（随 CLI 输出一起回来，按 session-id 精确匹配）。一轮只会产出自己产品的来源，
# 所以放在一张表里不会打架；但 `workbuddy-cli` 必须排在 `newapi` 前面——
# 前者精确匹配，后者是时间窗。
USAGE_SOURCES = ("device-api", "session-jsonl", "workbuddy-cli", "newapi")


@dataclass(frozen=True, slots=True)
class UsageSample:
    """一轮对话的 token 用量，一个来源一条。"""

    source: str = "device-api"
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    provider: str | None = None
    session_id: str | None = None
    timestamp: str | None = None
    match: str = "none"  # 怎么匹配上的：idempotency-key（精确）/ time-window / none
    api_calls: int | None = None
    error_calls: int | None = None
    log_entries: tuple[JsonObject, ...] = ()  # 后台证据摘要，不保存可能含凭据的错误正文


@dataclass(frozen=True, slots=True)
class LogStats:
    """第三层断言的输入。缺数据时用 None，不要用 0 冒充。"""

    api_calls: int | None = None
    error_calls: int | None = None
    source: str = "unavailable"


# 逐请求采集的状态。**「没开采集」和「真的 0 次调用」必须分得开**——
# 采集器没开着却报 0，和产品缺陷 #3 那个端上漏记长得一模一样：数字看着正常，全是假的。
MODEL_CALLS_DISABLED = "disabled"        # 采集代理没启用（默认）
MODEL_CALLS_OBSERVED = "observed"        # 这一轮确实有请求经过入口
MODEL_CALLS_UNAVAILABLE = "unavailable"  # 采集开着，但这一轮一个请求都没经过


@dataclass(frozen=True, slots=True)
class ModelCallCollection:
    """一轮里经过采集入口的模型请求。**只搬运，不判定。**

    `requests` 是 `modelproxy.ModelRequestRecord` 的 JSON 形状，逐请求一条；
    完整账本另有一份 `model-requests.jsonl`，这里带路径便于回溯。
    """

    status: str = MODEL_CALLS_DISABLED
    detail: str = ""
    requests: tuple[JsonObject, ...] = ()
    ledger_path: str | None = None


@dataclass(frozen=True, slots=True)
class Check:
    layer: Layer
    name: str
    verdict: Verdict | None  # None = 未执行（缺数据），聚合时忽略
    detail: str = ""

    @classmethod
    def skipped(cls, layer: Layer, name: str, detail: str) -> "Check":
        return cls(layer=layer, name=name, verdict=None, detail=detail)

    @property
    def executed(self) -> bool:
        return self.verdict is not None


@dataclass(frozen=True, slots=True)
class RunRecord:
    """落 JSONL 的一行；一轮一行，整批跑完再汇总。"""

    benchmark_id: str
    batch_id: str
    position: int
    case_name: str
    run_no: int
    prompt: str
    session_key: str
    verdict: Verdict
    product: str = "yonwork"  # 四个模式的产品维度；模型维度看 turn.requested_model
    checks: tuple[Check, ...] = ()
    turn: ChatTurn | None = None
    usage_samples: tuple[UsageSample, ...] = ()
    log_stats: LogStats | None = None
    model_calls: ModelCallCollection | None = None
    note: str = ""
    created_at: str = field(default_factory=now_iso)

    @property
    def usage(self) -> UsageSample | None:
        """断言和报表用的那一条。按 USAGE_SOURCES 的顺序挑，挑不到就是真没有。"""
        for source in USAGE_SOURCES:
            for sample in self.usage_samples:
                if sample.source == source:
                    return sample
        return None

    def to_json(self) -> JsonObject:
        payload = asdict(self)
        payload["verdict"] = self.verdict.value
        payload["checks"] = [
            {
                "layer": item.layer.value,
                "name": item.name,
                "verdict": item.verdict.value if item.verdict else None,
                "detail": item.detail,
            }
            for item in self.checks
        ]
        return payload
