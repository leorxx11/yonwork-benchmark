from __future__ import annotations

import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from ..client import ChatError, ChatTimeout
from ..db import load_env_file, project_root
from ..models import ChatTurn, JsonObject, UsageSample, now_iso
from .base import DriverError, UsageCollection


# 摸底结论见 docs/workbuddy-probe-report.md。一句话：
# 桌面那三个 loopback 端口（11983 / 11986 / 18488）**都不是聊天 API**，别再去试；
# `--serve` 的 REST/ACP 能对话但不返回 token / 实际模型 / 单轮耗时，不能当计量主路线。
# 主路线是每个 case 起一次内置 headless CLI。
USAGE_SOURCE = "workbuddy-cli"

# 契约：一个 case = 一个新进程 + 一个全新 session id + `--no-session-persistence`。
# 不要用 `--continue` / `--resume`，不要复用 session id。
BASE_ARGS = (
    "-p",
    "--output-format", "json",
    "--no-session-persistence",
    "--setting-sources", "user",
    "--strict-mcp-config",
    "--no-plugin-management",
    "--permission-mode", "dontAsk",
    "--max-turns", "1",
)

# 传给 Windows 进程的环境。值里带路径的要在 WSLENV 里加 `/p` 后缀做路径转换。
# WSLENV 从这张表生成，不手写——手写的那份一旦漏掉新变量，
# 目标进程里就是 undefined，而且**不报错**（CLAUDE.md 一-坑 3）。
_PATH_VARS = frozenset({
    "CODEBUDDY_CONFIG_DIR", "WORKBUDDY_CONFIG_DIR", "ACC_PRODUCT_CONFIG_PATH",
})
_FLAGS = (
    "CODEBUDDY_FORCE_HEADLESS_BUNDLE", "CODEBUDDY_DISABLE_COMPILE_CACHE",
    "DISABLE_AUTOUPDATER", "CODEBUDDY_DISABLE_GIT_REPO_SCAN",
    "CODEBUDDY_DISABLE_PROMPT_SUGGESTION", "CODEBUDDY_DISABLE_CRON",
    "CODEBUDDY_DISABLE_AUTO_MEMORY", "CODEBUDDY_DISABLE_SYSTEM_REMINDER",
    "CODEBUDDY_DISABLE_GIT_BASH", "CODEBUDDY_DISABLE_API_KEY_HELPER",
)


def _setting(name: str) -> str:
    """先看环境变量，再看 `.env`。

    容器 Worker 的配置由 Compose 以环境变量注入，宿主机 Worker 和 CLI 则只有 `.env`。
    只读 `os.environ` 的话，`.env` 里写的 `BENCH_WORKBUDDY_*` **会被静默忽略**，
    表现为「明明配了却还是去找默认路径」。口径跟 NewApiConfig.load 保持一致。
    """
    found = os.environ.get(name, "").strip()
    if found:
        return found
    return load_env_file(project_root() / ".env").get(name, "").strip()


def _interop_available() -> bool:
    """这个进程能不能起 Windows 程序。

    查的是 WSL interop 本身，不是「在不在容器里」——真正的前提是
    `binfmt_misc` 里注册了 Windows 可执行格式。容器只是最常见的一种缺失场景，
    关掉 interop 的 WSL、纯 Linux 机器同样跑不了，报错得一视同仁。
    名字在新内核上是 `WSLInterop-late`，老的是 `WSLInterop`，所以用通配。
    """
    registry = Path("/proc/sys/fs/binfmt_misc")
    return registry.is_dir() and any(registry.glob("WSLInterop*"))


def _no_interop_reason() -> str:
    if Path("/.dockerenv").is_file():
        return (
            "当前 Worker 跑在容器里，**起不了 Windows 进程**，也看不到 /mnt/d。"
            "要从 Web 控制台跑 WorkBuddy，Worker 得在宿主机原生起："
            "先 `docker compose stop worker`，再 `./scripts/host_worker.sh`。"
            "（两个 Worker 不会同时跑：MySQL 咨询锁会拦住后启动的那个。）"
        )
    return (
        "这台机器没有 WSL interop（/proc/sys/fs/binfmt_misc 里没有 WSLInterop*），"
        "起不了 WorkBuddy.exe。WorkBuddy 驱动只能在开了 interop 的 WSL 里跑。"
    )


def _default_config_dir() -> Path:
    """找 WorkBuddy 的配置目录。

    不硬编码 Windows 用户名——换台机器就废。显式设 `BENCH_WORKBUDDY_CONFIG_DIR`
    最稳（环境变量或 `.env` 都行）；没设就在 `/mnt/c/Users/*/.workbuddy` 里找，
    **必须唯一**，多个候选时宁可报错也不挑一个，挑错了会读到另一个账号的产品配置。
    """
    configured = _setting("BENCH_WORKBUDDY_CONFIG_DIR")
    if configured:
        return Path(configured)
    found = sorted(Path("/mnt/c/Users").glob("*/.workbuddy")) if Path("/mnt/c/Users").is_dir() else []
    if len(found) == 1:
        return found[0]
    raise DriverError(
        "找不到唯一的 WorkBuddy 配置目录"
        f"（候选 {[str(item) for item in found]}）；请设 BENCH_WORKBUDDY_CONFIG_DIR"
    )


class WorkBuddyDriver:
    """WorkBuddy 桌面端：每轮起一次内置 headless CLI，读 stdout 的 JSON。

    **2026-09-21 从本仓库实测跑通一轮**（默认模型、`--tools ''`）：
    `runId == BenchmarkId`、`result:success`、用量 3645/8 tok、
    实际模型 `deepseek-v4.1-flash`、按 session-id 精确匹配。

    ⚠️ **外层 16.744s，内部 duration_ms 只有 3.087s**——
    每个 case 一个新进程，**冷启动占掉 13.6s**，是模型耗时的 4 倍多。
    做耗时对比时别拿它直接跟 YonWork 的常驻服务比，那不是同一回事；
    两个数都留着就是为了能把这一段拆出来。

    ⚠️ **容器里的 Worker 跑不了这个驱动**：它要起一个 Windows 进程，
    而 compose 里的 worker 既看不到 `/mnt/d`，也没有 WSL interop。
    要从 Web 控制台跑 WorkBuddy，Worker 必须在宿主机上原生起
    （`python -m runner.worker`）。CLI 直接跑则没有这个限制。
    """

    product = "workbuddy"

    def __init__(
        self,
        *,
        model_query: str = "",
        timeout_seconds: float = 600.0,
        transcript_dir: Path | None = None,
        home: Path | None = None,
        config_dir: Path | None = None,
        allow_tools: bool = False,
        runner: Any = None,
    ) -> None:
        self.model_query = model_query.strip()
        self.timeout_seconds = timeout_seconds
        self.transcript_dir = transcript_dir
        self.allow_tools = allow_tools
        self._home = home
        self._config_dir = config_dir
        # 注入点：单测不起真进程。默认就是 subprocess.run。
        self._runner = runner or _run_process
        # 用量随本轮输出一起回来，但 Driver 协议把「跑一轮」和「采用量」分成了两步
        # （YonWork 那边确实要事后去两个来源捞）。这里把它暂存一下，
        # ChatTurn 是 frozen+slots 的，挂不上额外字段。
        self._usage: dict[str, UsageSample] = {}

    # ---- Driver 协议 ----

    def preflight(self) -> Iterator[str]:
        # 这一条必须排在最前面。容器里 /mnt/d 本来就不存在，先查安装目录的话
        # 报出来的是「安装目录不存在，设 BENCH_WORKBUDDY_HOME」——
        # 把人引去设一个设了也没用的变量，真正的原因（起不了 Windows 进程）反而不说。
        if not _interop_available():
            raise DriverError(_no_interop_reason())

        home = self._home or Path(_setting("BENCH_WORKBUDDY_HOME") or "/mnt/d/WorkBuddy")
        if not home.is_dir():
            raise DriverError(f"WorkBuddy 安装目录不存在：{home}（设 BENCH_WORKBUDDY_HOME）")
        launcher = home / "WorkBuddy.exe"
        if not launcher.is_file():
            raise DriverError(f"找不到 {launcher}")
        config_dir = self._config_dir or _default_config_dir()
        if not config_dir.is_dir():
            raise DriverError(f"WorkBuddy 配置目录不存在：{config_dir}")

        self._home = home
        self._config_dir = config_dir
        yield f"WorkBuddy：{home}（配置 {config_dir}）"
        yield f"指定模型：{self.model_query}" if self.model_query else "未指定模型，用产品默认"
        if not self.allow_tools:
            yield "已关闭工具（--tools ''），纯文本基准"

    def session_key(self, benchmark_id: str) -> str:
        """直接用 BenchmarkId 当 `--session-id`。

        报告的成功判定第 4 条要求「`session_id` 等于本次自己生成的 run id」。
        让两者相等，那一条就由现成的 `run-id` 断言自动覆盖，不用另写一套。
        """
        return benchmark_id

    def run_turn(self, *, benchmark_id: str, prompt: str) -> ChatTurn:
        command = self._command(benchmark_id, prompt)
        started_at = now_iso()
        started = time.monotonic()
        try:
            completed = self._runner(command, self._environment(), self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise ChatTimeout(
                f"{benchmark_id}: WorkBuddy CLI 超过 {self.timeout_seconds:g}s 未返回"
            ) from exc
        except OSError as exc:
            raise DriverError(f"{benchmark_id}: 起不来 WorkBuddy CLI：{exc}") from exc

        duration = round(time.monotonic() - started, 3)
        transcript_path = self._save_transcript(benchmark_id, completed.stdout)
        records = _parse_stdout(completed.stdout, benchmark_id, completed.returncode)
        turn, sample = _build_turn(
            benchmark_id=benchmark_id,
            session_key=self.session_key(benchmark_id),
            prompt=prompt,
            started_at=started_at,
            duration=duration,
            records=records,
            requested_model=self._requested_model(),
            transcript_path=transcript_path,
        )
        if sample is not None:
            self._usage[benchmark_id] = sample
        return turn

    def collect_usage(self, turn: ChatTurn) -> UsageCollection:
        """用量随本轮输出一起回来了，不用事后采。

        这正是两个产品形状不同的地方：YonWork 要事后去两个来源捞，
        还得等端上端点落库；这边跑完就有，而且是按 session-id 精确对上的。
        """
        sample = self._usage.pop(turn.benchmark_id, None)
        return UsageCollection(samples=(sample,) if sample is not None else ())

    def close(self) -> None:
        return None

    # ---- 内部 ----

    def _requested_model(self) -> str | None:
        # 空 / "default" 都表示「用产品默认」。这时不能记请求模型，
        # 否则 `default -> deepseek-v4.1-flash` 这种正常的别名解析
        # 会被 model-match 断言误判成静默回落，整轮记成 Invalid。
        return self.model_query if self.model_query.lower() not in ("", "default") else None

    def _command(self, benchmark_id: str, prompt: str) -> list[str]:
        assert self._home is not None
        cli = "\\".join((str(self._home).replace("/mnt/d", "D:"), "resources",
                         "app.asar.unpacked", "cli", "bin", "codebuddy"))
        command = [str(self._home / "WorkBuddy.exe"), cli, *BASE_ARGS]
        if not self.allow_tools:
            # 纯文本基准不让模型自己读写文件或执行命令。
            # 真要评测工具能力，应给那个 case 单独开，而不是全局放开。
            command += ["--tools", ""]
        command += ["--model", self.model_query or "default"]
        command += ["--session-id", benchmark_id, prompt]
        return command

    def _environment(self) -> dict[str, str]:
        assert self._config_dir is not None
        config = str(self._config_dir)
        values: dict[str, str] = {
            # WorkBuddy.exe 靠这个变量退化成安装包自带的 Node 运行时。
            "ELECTRON_RUN_AS_NODE": "1",
            "CODEBUDDY_CONFIG_DIR": config,
            "WORKBUDDY_CONFIG_DIR": config,
            "ACC_PRODUCT_CONFIG_PATH": f"{config}/cache/acc-product-config-v3.json",
            "WORKBUDDY_DATA_FOLDER_NAME": ".workbuddy",
            "CODEBUDDY_INTERNET_ENVIRONMENT": "internal",
            "CODEBUDDY_HOST": "workbuddy-desktop",
        }
        values.update({name: "1" for name in _FLAGS})
        values["WSLENV"] = ":".join(
            f"{name}/p" if name in _PATH_VARS else name for name in sorted(values)
        )
        return values

    def _save_transcript(self, benchmark_id: str, stdout: str) -> str | None:
        if self.transcript_dir is None:
            return None
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        path = self.transcript_dir / f"{benchmark_id}.cli.json"
        path.write_text(stdout, encoding="utf-8")
        return str(path)


def _run_process(command: list[str], env: dict[str, str], timeout: float) -> Any:
    return subprocess.run(
        command,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _parse_stdout(stdout: str, benchmark_id: str, returncode: int) -> list[JsonObject]:
    """stdout 应当是一个 JSON 数组。

    **退出码不能当判据**：报告实测无效模型时退出码同样是 0。
    所以这里只在「压根解析不出来」时才认定驱动失败。
    """
    text = (stdout or "").strip()
    if not text:
        raise ChatError(f"{benchmark_id}: WorkBuddy CLI 没有输出（退出码 {returncode}）")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChatError(
            f"{benchmark_id}: WorkBuddy CLI 输出不是 JSON（退出码 {returncode}）：{exc}"
        ) from exc
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ChatError(f"{benchmark_id}: WorkBuddy CLI 输出既不是数组也不是对象")
    return [item for item in payload if isinstance(item, dict)]


def _build_turn(
    *,
    benchmark_id: str,
    session_key: str,
    prompt: str,
    started_at: str,
    duration: float,
    records: list[JsonObject],
    requested_model: str | None,
    transcript_path: str | None,
) -> tuple[ChatTurn, UsageSample | None]:
    """把 CLI 的记录数组摊平成 ChatTurn。**这里只搬运，不判定。**

    报告列的七条成功判定，有五条落在现成的断言上，不用在驱动里重写：
    `subtype`/`is_error` → 完成性层，`session_id` → 产物层的 run-id，
    实际模型 → 成本层的 model-match，答案文本 → 内容层。
    """
    counts: Counter[str] = Counter()
    tool_calls: list[str] = []
    actual_model: str | None = None
    provider_usage: JsonObject = {}
    result: JsonObject | None = None

    for record in records:
        kind = str(record.get("type") or "unknown")
        role = record.get("role")
        counts[f"{kind}:{role}" if isinstance(role, str) else kind] += 1
        if kind == "result":
            result = record
            continue
        if record.get("role") != "assistant":
            continue
        provider = record.get("providerData")
        if isinstance(provider, dict):
            # 判模型必须用 providerData.model（服务端实际执行的），
            # 不能信命令行参数——别名会被解析成具体模型。
            if isinstance(provider.get("model"), str):
                actual_model = provider["model"]
            raw = provider.get("rawUsage")
            if isinstance(raw, dict):
                provider_usage = raw
        tool_calls.extend(_tool_names(record))

    # 唯一可靠终点是最后那条 result。assistant 的 status=="completed"、
    # 工具调用结束、退出码 0 都**不能**单独当终点。
    if result is None:
        raise ChatError(
            f"{benchmark_id}: WorkBuddy CLI 输出里没有 result 记录，本轮无法判定",
        )

    subtype = result.get("subtype")
    is_error = bool(result.get("is_error"))
    session_id = result.get("session_id")
    answer = result.get("result")

    turn = ChatTurn(
        benchmark_id=benchmark_id,
        session_key=session_key,
        prompt=prompt,
        started_at=started_at,
        ended_at=now_iso(),
        # 外层 wall time，含 CLI 冷启动，更接近用户体验——所以拿它做耗时断言。
        # first_delta 留空：json 模式一次性吐完，本来就没有流式首字这回事
        #（要首字得换 stream-json，那是另一条路）。
        duration_seconds=duration,
        # CLI 自报的内部耗时。外层减它就是冷启动，实测占 13.6s / 16.7s——
        # 不把这个数单独拎出来，跟 YonWork 常驻服务比耗时就是在比冷启动。
        engine_seconds=_seconds(result.get("duration_ms")),
        run_id=session_id if isinstance(session_id, str) else None,
        answer=answer if isinstance(answer, str) else None,
        terminated_by=f"result:{subtype}" if isinstance(subtype, str) else "result",
        stop_reason=subtype if isinstance(subtype, str) else None,
        final_state="error" if is_error else "completed",
        event_counts=dict(counts),
        tool_calls=tuple(tool_calls),
        requested_model=requested_model,
        requested_model_label=requested_model,
        transcript_path=transcript_path,
    )
    return turn, _usage_sample(result, provider_usage, actual_model)


def _tool_names(record: JsonObject) -> list[str]:
    content = record.get("content")
    if not isinstance(content, list):
        return []
    names: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if isinstance(kind, str) and "tool" in kind:
            name = block.get("name") or block.get("toolName")
            names.append(name if isinstance(name, str) and name else "unknown")
    return names


def _usage_sample(
    result: JsonObject, provider_usage: JsonObject, actual_model: str | None
) -> UsageSample | None:
    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = _int(usage.get("input_tokens")) or _int(provider_usage.get("prompt_tokens"))
    output_tokens = _int(usage.get("output_tokens")) or _int(provider_usage.get("completion_tokens"))
    total = _int(provider_usage.get("total_tokens"))
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    if input_tokens is None and output_tokens is None and actual_model is None:
        return None
    return UsageSample(
        source=USAGE_SOURCE,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
        # `total_cost_usd` 在这个构建里恒为 0，不是真实费用，所以不记。
        # `rawUsage.credit` 是产品积分，也不是美元，别改名混进成本列。
        cost_usd=None,
        model=actual_model,
        session_id=result.get("session_id") if isinstance(result.get("session_id"), str) else None,
        match="session-id",
    )


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _seconds(millis: Any) -> float | None:
    """CLI 自报的毫秒 → 秒。缺了就留 None，**不要用外层耗时顶替**。

    顶替的话冷启动会算成 0，看起来就像「这个产品没有冷启动开销」——
    而那正是这个字段存在的唯一目的。
    """
    value = _int(millis)
    return None if value is None else round(value / 1000, 3)
