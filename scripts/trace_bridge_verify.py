"""`install_trace_bridge verify`：YonWork 更新或重启后，确认 hook 扩展还在正常工作。

    .venv/bin/python -m scripts.install_trace_bridge verify
    .venv/bin/python -m scripts.install_trace_bridge verify --batch results/<批次目录>

不加 `--batch` 查五件事：配置还在、当前这次网关启动加载了插件、插件投递没出错、
版本是否验收过、本机安全缓解还在（CLAUDE.md 坑 4）。
加 `--batch` 再核对那一批：每轮都采到、账本全部归属、hook 的 runId 等于 BenchmarkId、
hook 看到的 span 和账本请求逐一对得上。**跑批这一步要人触发**，verify 不替你发请求。

判断逻辑是纯函数（喂数据、出 `Check`），IO 集中在 `gather()`，单测不需要 YonWork。
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

PLUGIN_ID = "benchmark-trace-bridge"

# 实机验收过的版本（docs/yonwork-1.0.10-correlation-probe.md）。
# 升级后先 verify + 跑一批 --batch 核对，全部通过再改这里——这就是「重新验收」的记录。
VERIFIED_VERSIONS = {"yonwork": "1.0.10", "openclaw": "2026.7.1-2"}

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}
_LOAD_SUMMARY = "startup trace: plugins.gateway-load "
_PLUGIN_LOAD = f"startup trace: plugins.gateway-load.plugin.{PLUGIN_ID} "


@dataclass(frozen=True, slots=True)
class Check:
    section: str
    name: str
    level: str
    detail: str = ""


def parse_time(value: str | None) -> datetime | None:
    """ISO 时间 → aware datetime。PowerShell 的 'o' 格式带 7 位小数，截到 6 位。"""
    if not value:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ---- 配置 ---------------------------------------------------------------

def check_config(status: dict, *, files_present: bool, files_current: bool) -> list[Check]:
    s = "配置"
    missing = [key for key in ("allowed", "enabled", "load_path") if not status.get(key)]
    checks = [
        Check(s, "openclaw.json 三处登记", FAIL if missing else OK,
              f"缺 {', '.join(missing)}：YonWork 可能在更新时清掉了，重新 install" if missing else ""),
        Check(s, "采集凭据", OK if status.get("token_configured") else FAIL,
              "" if status.get("token_configured") else "collectorToken 为空，插件只会记本地日志"),
        Check(s, "插件文件", OK if files_present else FAIL,
              "" if files_present else "插件目录里缺文件，重新 install"),
    ]
    if files_present and not files_current:
        checks.append(Check(s, "插件文件与仓库一致", WARN,
                            "仓库里的插件改过，重新 install 并重启 YonWork 才生效"))
    return checks


# ---- 网关加载 -----------------------------------------------------------

def parse_log(lines: Iterable[str]) -> list[dict]:
    """openclaw.log 一行一个 JSON：消息在键 "1"，时间和级别在 `_meta`。"""
    entries = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        meta = value.get("_meta") if isinstance(value, dict) else None
        if not isinstance(meta, dict):
            continue
        at = parse_time(meta.get("date"))
        if at is not None:
            entries.append({"at": at, "level": str(meta.get("logLevelName") or ""),
                            "message": str(value.get("1") or "")})
    return entries


def check_gateway_load(entries: list[dict], gateway_started: datetime | None
                       ) -> tuple[list[Check], datetime | None]:
    """只认**当前这次**网关启动：最后一条整体加载汇总之前、上一条汇总之后的那段。

    网关会自己重启（改模型配置之类），日志里「曾经加载过」不代表现在还加载着。
    返回的时间点给下一步筛插件日志用。
    """
    s = "网关加载"
    summaries = [i for i, e in enumerate(entries) if e["message"].startswith(_LOAD_SUMMARY)]
    if not summaries:
        return [Check(s, "找到加载记录", FAIL, "openclaw.log 里没有插件加载汇总")], None
    last = summaries[-1]
    first = summaries[-2] + 1 if len(summaries) > 1 else 0
    loaded_at = entries[last]["at"]
    window = entries[first:last + 1]
    checks = []
    if gateway_started and gateway_started > loaded_at:
        checks.append(Check(s, "加载记录是当前进程的", WARN,
                            "网关进程比最后一次加载记录新：可能还在启动，过一会儿再 verify"))
    load = [e["message"] for e in window if e["message"].startswith(_PLUGIN_LOAD)]
    counts = {}
    for message in load:
        for key, value in re.findall(r"(loadFailedCount|registerFailedCount)=([\d.]+)", message):
            counts[key] = float(value)
    if not load:
        checks.append(Check(s, "当前这次启动加载了插件", FAIL,
                            f"{loaded_at:%m-%d %H:%M:%S}Z 那次加载里没有 {PLUGIN_ID}"))
    elif counts.get("loadFailedCount") != 0 or counts.get("registerFailedCount") != 0:
        checks.append(Check(s, "当前这次启动加载了插件", FAIL, f"失败计数：{counts}"))
    else:
        checks.append(Check(s, "当前这次启动加载了插件", OK,
                            f"{loaded_at:%m-%d %H:%M:%S}Z，load/register 失败均为 0"))
    problems = [e for e in entries[first:] if PLUGIN_ID in e["message"]
                and e["level"].upper() in ("WARN", "ERROR", "FATAL")]
    if problems:
        checks.append(Check(s, "没有针对插件的警告", FAIL,
                            f"{len(problems)} 条，最近一条：{problems[-1]['message'][:160]}"))
    return checks, loaded_at


# ---- 插件投递 -----------------------------------------------------------

def check_bindings(bindings: list[dict], since: datetime | None) -> list[Check]:
    """插件本地日志的回执。只看当前这次网关启动之后的。"""
    s = "插件投递"
    recent = [b for b in bindings if since is None or (parse_time(b.get("at")) or since) >= since]
    if not recent:
        return [Check(s, "有模型调用经过插件", WARN,
                      "当前这次网关启动后还没见过模型调用：用统一代理跑一轮 smoke，再 verify --batch")]
    receipts = Counter(str(b.get("receipt")) for b in recent)
    broken = {k: v for k, v in receipts.items()
              if k.startswith(("error:", "http-", "skipped:")) or k == "no-receipt"}
    summary = "、".join(f"{k} {v}" for k, v in receipts.most_common())
    if broken:
        return [Check(s, "投递到采集代理", FAIL,
                      f"{summary}。error/http 多半是 collector 没起或端口变了；"
                      "http-401 / skipped:no-token 是凭据不对")]
    if not receipts.get("bound"):
        return [Check(s, "投递到采集代理", WARN,
                      f"{summary}：只见过非跑批流量，还不能证明归属正常")]
    level = WARN if receipts.get("conflict") else OK
    return [Check(s, "投递到采集代理", level, summary)]


# ---- 批次核对 -----------------------------------------------------------

def _trace_key(traceparent: str | None) -> tuple[str, str] | None:
    parts = (traceparent or "").lower().split("-")
    return (parts[1], parts[2]) if len(parts) == 4 else None


def _owner(run_id: str, benchmark_ids: set[str]) -> str | None:
    if run_id in benchmark_ids:
        return run_id
    head, marker, _ = run_id.partition(":compaction:")
    return head if marker and head in benchmark_ids else None


def check_batch(records: list[dict], requests: list[dict], bindings: list[dict]) -> list[Check]:
    """一批跑完之后的端到端核对：这就是 1.0.10 实机验收做的那几条。"""
    s = "批次核对"
    ids = {str(r.get("benchmark_id")) for r in records if r.get("benchmark_id")}
    if not ids:
        return [Check(s, "批次有记录", FAIL, "results.jsonl 里没有轮次")]
    statuses = Counter((r.get("model_calls") or {}).get("status") or "disabled" for r in records)
    checks = [Check(s, "每轮都采到请求", OK if set(statuses) == {"observed"} else FAIL,
                    "、".join(f"{k} {v}" for k, v in statuses.items()))]

    attribution = Counter(
        "conflict" if q.get("attribution_source") == "conflict" else str(q.get("attribution"))
        for q in requests)
    ours = [q for q in requests if q.get("run_id") in ids]
    bad = sum(v for k, v in attribution.items() if k not in ("attributed", "late"))
    checks.append(Check(s, "账本请求全部归属", FAIL if bad or not requests else OK,
                        f"{len(requests)} 条：" + "、".join(f"{k} {v}" for k, v in attribution.items())
                        if requests else "账本里没有请求：产品的 baseUrl 可能没指向采集入口"))

    events = [b for b in bindings if _owner(str(b.get("run_id") or ""), ids)]
    if not events:
        checks.append(Check(s, "hook 的 runId 就是 BenchmarkId", FAIL,
                            "插件日志里没有这一批的 hook 事件"))
        return checks
    covered = {_owner(str(b["run_id"]), ids) for b in events}
    checks.append(Check(s, "hook 的 runId 就是 BenchmarkId", OK if covered == ids else FAIL,
                        f"{len(events)} 个事件覆盖 {len(covered)}/{len(ids)} 轮"))

    hook_spans = {(str(b.get("trace_id")).lower(), str(b.get("span_id")).lower()) for b in events}
    ledger_spans = {key for q in ours if (key := _trace_key(q.get("traceparent")))}
    if hook_spans == ledger_spans:
        checks.append(Check(s, "hook span 与账本请求一一对应", OK, f"{len(hook_spans)} 个 span"))
    else:
        checks.append(Check(s, "hook span 与账本请求一一对应", FAIL,
                            f"hook 独有 {len(hook_spans - ledger_spans)}，"
                            f"账本独有 {len(ledger_spans - hook_spans)}"))
    return checks


# ---- 版本与安全 ---------------------------------------------------------

def check_versions(versions: dict[str, str | None]) -> list[Check]:
    checks = []
    for name, expected in VERIFIED_VERSIONS.items():
        actual = versions.get(name)
        if actual is None:
            checks.append(Check("版本", name, WARN, "读不到版本"))
        elif actual != expected:
            checks.append(Check("版本", name, WARN,
                                f"{actual}，验收过的是 {expected}：跑一批 verify --batch 全过后"
                                "再改 VERIFIED_VERSIONS"))
        else:
            checks.append(Check("版本", name, OK, actual))
    return checks


def listening_addresses(netstat_text: str, port: int) -> set[str]:
    found = set()
    for line in netstat_text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "TCP" and parts[3] == "LISTENING":
            host, _, local_port = parts[1].rpartition(":")
            if local_port == str(port):
                found.add(host)
    return found


def check_exposure(netstat_text: str, ports: dict[str, int | None],
                   auth: tuple[int | None, int | None]) -> list[Check]:
    """CLAUDE.md 坑 4：从 WSL 拉起 YonWork 会静默退回 0.0.0.0 + 免鉴权。"""
    s = "安全缓解"
    checks = []
    for name, port in ports.items():
        if port is None:
            checks.append(Check(s, f"{name} 只绑 loopback", WARN, "读不到端口"))
            continue
        hosts = listening_addresses(netstat_text, port)
        exposed = hosts & {"0.0.0.0", "[::]"}
        if exposed:
            checks.append(Check(s, f"{name} 只绑 loopback", FAIL,
                                f":{port} 监听在 {', '.join(sorted(exposed))}，用 uTools 重启 YonWork"))
        elif not hosts:
            checks.append(Check(s, f"{name} 只绑 loopback", WARN, f":{port} 没在监听"))
        else:
            checks.append(Check(s, f"{name} 只绑 loopback", OK, f":{port} {', '.join(sorted(hosts))}"))
    without, with_token = auth
    ok = without == 401 and with_token == 200
    checks.append(Check(s, "Host API 在校验 token", OK if ok else FAIL,
                        f"不带 token {without}，带 token {with_token}（应为 401 / 200）"))
    return checks


# ---- IO -----------------------------------------------------------------

def _get_status(url: str, token: str | None = None, timeout: float = 5.0) -> int | None:
    from runner.transport import build_opener

    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with build_opener().open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def _windows_output(args: list[str]) -> str:
    """Windows 命令按本机代码页（中文系统是 GBK）输出；要的字段都是 ASCII，容错解码即可。"""
    try:
        raw = subprocess.run(args, capture_output=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return raw.decode("utf-8", errors="replace")


def _powershell(command: str) -> str:
    return _windows_output(["powershell.exe", "-NoProfile", "-Command", command])


def _read_jsonl(paths: Iterable[Path]) -> list[dict]:
    rows = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def gather(config_path: Path, target: Path, status: dict, *, files_current: bool,
           batch: Path | None) -> list[Check]:
    from runner.discovery import DiscoveryError, discover
    from runner.modelproxy import CollectorConfig, CollectorConfigError, RemoteCollector, load_ledger
    from runner.modelproxy.ledger import LedgerError

    checks = check_config(status, files_present=all(
        (target / name).is_file() for name in ("index.js", "openclaw.plugin.json", "package.json")),
        files_current=files_current)

    started = parse_time(_powershell(
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
        "? { $_.CommandLine -match 'gateway-bundle' } | "
        "% { $_.CreationDate.ToUniversalTime().ToString('o') } | Select -First 1").strip())
    log = config_path.parent / "logs" / "openclaw.log"
    entries = parse_log(log.read_text(encoding="utf-8", errors="replace").splitlines()) \
        if log.is_file() else []
    load_checks, loaded_at = check_gateway_load(entries, started)
    checks += load_checks

    bindings = _read_jsonl(sorted((target / "logs").glob("bindings-*.jsonl")))
    checks += check_bindings(bindings, loaded_at)

    try:
        collector = CollectorConfig.load()
        healthy = RemoteCollector.healthy(collector)
        detail = collector.health_url
    except CollectorConfigError as exc:
        healthy, detail = False, str(exc)
    checks.append(Check("插件投递", "采集入口可达", OK if healthy else FAIL,
                        detail if healthy else f"{detail}：docker compose up -d collector"))

    user_data = config_path.parents[2]
    cdp_port = None
    port_file = user_data / "DevToolsActivePort"
    if port_file.is_file():
        first = port_file.read_text(encoding="utf-8").splitlines()[:1]
        cdp_port = int(first[0]) if first and first[0].strip().isdigit() else None
    yonwork = None
    if cdp_port:
        from runner.transport import build_opener
        try:
            with build_opener().open(f"http://127.0.0.1:{cdp_port}/json/version", timeout=5) as r:
                agent = json.loads(r.read()).get("User-Agent", "")
            match = re.search(r"YonWork/(\S+)", agent)
            yonwork = match.group(1) if match else None
        except (urllib.error.URLError, OSError, ValueError):
            pass
    openclaw = None
    package = Path("/mnt/d/yonwork/resources/openclaw/package.json")
    if package.is_file():
        openclaw = json.loads(package.read_text(encoding="utf-8")).get("version")
    checks += check_versions({"yonwork": yonwork, "openclaw": openclaw})

    try:
        endpoint = discover()
        host_port = int(endpoint.base_url.rsplit(":", 1)[1])
        url = endpoint.url("/api/auth-runtime/session/status")
        auth = (_get_status(url), _get_status(url, endpoint.token))
    except (DiscoveryError, ValueError):
        host_port, auth = None, (None, None)
    netstat = _windows_output(["netstat.exe", "-ano"])
    checks += check_exposure(netstat, {"Host API": host_port, "CDP": cdp_port}, auth)

    if batch is not None:
        results = batch / "results.jsonl"
        if not results.is_file():
            checks.append(Check("批次核对", "批次有记录", FAIL, f"找不到 {results}"))
        else:
            records = _read_jsonl([results])
            try:
                requests = [r.to_json() for r in load_ledger(batch / "model-requests.jsonl")]
            except LedgerError:
                requests = []
            checks += check_batch(records, requests, bindings)
    return checks


def render(checks: list[Check]) -> str:
    lines, section = [], None
    for check in checks:
        if check.section != section:
            section = check.section
            lines.append(f"\n[{section}]")
        detail = f"  {check.detail}" if check.detail else ""
        lines.append(f"  {_MARK[check.level]} {check.name}{detail}")
    fails = sum(c.level == FAIL for c in checks)
    warns = sum(c.level == WARN for c in checks)
    lines.append(f"\n{'通过' if not fails else '未通过'}：{fails} 项失败，{warns} 项需留意")
    return "\n".join(lines)
