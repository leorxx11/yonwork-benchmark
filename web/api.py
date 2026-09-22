from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from runner.db import DatabaseError
from runner.case_catalog import (
    DEFAULT_CATALOG_PATH,
    CaseCatalogError,
    load_catalog,
    resolve_case_set,
)
from runner.catalog import CatalogError, list_model_choices
from runner.discovery import DiscoveryError, discover, has_session, health_check, session_status
from runner.drivers import DRIVERS
from runner.job_store import (
    NewJob,
    NewPlan,
    PlanMode,
    active_job,
    cancel_plan,
    create_job,
    create_plan,
    get_job,
    list_events,
    list_jobs,
    plan_jobs,
    request_cancel,
)
from runner.transport import TransportError

from . import queries


BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="YonWork 基准测试", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

VERDICT_ORDER = ("Pass", "Fail", "Timeout", "Error", "Invalid")
CASE_CATALOG_PATH = Path(os.environ.get("BENCH_CASE_CATALOG", str(DEFAULT_CATALOG_PATH)))

# 页面上给产品配的说明。跑批链路只认 runner.drivers.DRIVERS 的键，
# 这里只负责把「选它会发生什么」讲清楚——尤其是有附加条件的那条。
#
# ⚠️ **顺序有意义**：表单里第一个是默认选中项，所以主链路必须排第一。
# 按字母排的话 workbuddy 会跑到前面，而它在容器里的 Worker 上根本跑不了，
# 等于把一个默认会失败的选项设成了默认。
PRODUCTS = (
    ("yonwork", "YonWork 桌面端", "Host API + SSE，主链路，已实测"),
    # 容器里的 Worker 起不了 Windows 进程，所以这里必须把条件写在选项上——
    # 不写的话用户只会看到一条语焉不详的失败日志。
    ("workbuddy", "WorkBuddy 桌面端",
     "每轮一个 headless CLI 进程。需要 Worker 跑在宿主机上，容器里的 Worker 起不了它"),
)


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) / 1000:.2f}s"


def _fmt_int(value: Any) -> str:
    return "—" if value is None else f"{int(value):,}"


templates.env.filters["ms"] = _fmt_ms
templates.env.filters["num"] = _fmt_int


def _active_job() -> dict[str, Any] | None:
    """顶栏那条「后台还在跑」。

    连不上库时**返回 None 而不是抛**：这是个提示条，不该让
    `/jobs/new` 这种本来不碰库的页面跟着一起打不开。
    """
    try:
        return active_job()
    except DatabaseError:
        return None


def _render(
    request: Request,
    name: str,
    *,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "verdict_order": VERDICT_ORDER,
            "active_job": _active_job(),
            **context,
        },
        status_code=status_code,
    )


def _runtime_context() -> dict[str, Any]:
    context: dict[str, Any] = {
        "ok": False,
        "logged_in": False,
        "endpoint": "",
        "version": "",
        "models": [],
        "problem": "",
    }
    try:
        endpoint = discover()
        health_check(endpoint)
        logged_in = has_session(session_status(endpoint))
        models = list_model_choices(endpoint) if logged_in else []
        context.update(
            ok=True,
            logged_in=logged_in,
            endpoint=endpoint.base_url,
            version=endpoint.version or "",
            models=models,
            problem="" if logged_in else "YonWork 正在运行，但尚未登录",
        )
    except (DiscoveryError, CatalogError, TransportError) as exc:
        context["problem"] = str(exc)
    return context


def _job_form_context(error: str = "") -> dict[str, Any]:
    try:
        catalog = load_catalog(CASE_CATALOG_PATH)
        case_sets = catalog.case_sets
        catalog_problem = ""
    except CaseCatalogError as exc:
        case_sets = ()
        catalog_problem = str(exc)
    return {
        "case_sets": case_sets,
        "runtime": _runtime_context(),
        "catalog_path": CASE_CATALOG_PATH,
        "catalog_problem": catalog_problem,
        "default_name": datetime.now().strftime("benchmark-%Y%m%d-%H%M"),
        # 只列真的有驱动的，免得页面上摆着一个提交就报错的选项。
        "products": [item for item in PRODUCTS if item[0] in DRIVERS],
        "error": error,
    }


@app.exception_handler(DatabaseError)
async def _database_error(request: Request, exc: DatabaseError) -> HTMLResponse:
    # 连不上库是最常见的启动问题，直接把排查路径写在页面上。
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={"message": str(exc)},
        status_code=503,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return _render(request, "index.html", suites=queries.list_suites())


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request) -> HTMLResponse:
    return _render(request, "jobs.html", jobs=list_jobs())


@app.get("/jobs/new", response_class=HTMLResponse)
def new_job(request: Request) -> HTMLResponse:
    return _render(request, "new_job.html", **_job_form_context())


def _text_field(raw: str, label: str) -> str:
    """拦住双重编码的表单值。

    `application/x-www-form-urlencoded` 规范只允许 ASCII / percent-encoded，
    所以 Starlette 对裸字节按 **Latin-1** 解。浏览器一定会 percent-encode，
    但 `curl -d '名字=中文'` 发的是裸 UTF-8 字节，于是
    `会`（E4 BC 9A）被当成三个 Latin-1 字符，入库再编一次成 C3A4 C2BC C29A——
    3 字节变 6 字节，页面上就是「ä¼è¯…」。

    **这正是本项目最怕的那类问题：不报错、任务照跑、只有显示是坏的。**
    所以宁可当场拒绝也不猜着修——猜错了就是把用户真正想要的名字改掉。
    2026-09-21 实测清掉过 5 个这样的值，全是用 curl 建的测试任务。
    """
    text = raw.strip()
    try:
        decoded = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text  # 正常情况都走这里：非 Latin-1 字符编不过去
    if decoded == text:
        return text
    raise ValueError(
        f"{label}疑似双重编码（{text!r} 看起来应该是 {decoded!r}）。"
        "表单值没有做 percent 编码——浏览器会自动做，用 curl 的话改成 "
        "`--data-urlencode`，不要用 `-d`。"
    )


def _number_field(raw: Any, label: str, *, default: float) -> float:
    """表单数字按字符串收，自己解析。

    直接标注 int/float 的话，清空输入框提交会在进到函数体之前就被
    pydantic 拒掉，用户拿到的是一页裸 422 JSON，而不是下面那个
    带中文提示、还保留了已填内容的表单。
    """
    text = str(raw).strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{label}必须是数字：{raw!r}") from None


def _form_list(value: Any) -> list[str]:
    """重复表单字段收成列表。

    只认真正的序列：这些参数的默认值是 FastAPI 的 `Form(...)` 描述符，
    走 HTTP 时会被替换成列表，被当普通函数直接调用（单测、脚本）时不会——
    那时拿到的是描述符对象本身，`or []` 判不出来，会当成「有一行模式」。
    """
    return [str(item) for item in value] if isinstance(value, (list, tuple)) else []


def _modes(products: list[str] | None, models: list[str] | None) -> list[PlanMode]:
    """把表单里那组模式行收成 PlanMode 列表。

    两个重复字段按**下标**配对，不用 `产品|模型` 那种拼接值——拼接就要定分隔符，
    而模型 choiceId 里出现什么字符我们说了不算，分隔符一撞就会静默配错组合。

    **label 一律由服务端从 product + model_query 推**，不收客户端传来的显示名。
    落库的标签只能反映真正发出去的那个值；报告页显示的模型名来自实际响应
    （`model_mode`），两者对不上时说明产品静默回落了默认模型（CLAUDE.md 六-4），
    那正是要看见的信号，不能被一个客户端自述的好看标签盖住。
    """
    rows = _form_list(products)
    picked = _form_list(models)
    if len(picked) != len(rows):
        raise ValueError(
            f"模式行的产品数（{len(rows)}）和模型数（{len(picked)}）对不上，请重新提交表单"
        )
    modes: list[PlanMode] = []
    for index, name in enumerate(rows):
        name = name.strip()
        if not name:
            continue
        if name not in DRIVERS:
            raise ValueError(f"没有名为 {name!r} 的产品驱动")
        modes.append(PlanMode(product=name, model_query=picked[index].strip()))
    return modes


@app.post("/jobs")
def submit_job(
    request: Request,
    experiment_name: str = Form(...),
    case_set_id: str = Form(...),
    product: str = Form("yonwork"),
    model_query: str = Form(""),
    mode_product: list[str] | None = Form(None),
    mode_model: list[str] | None = Form(None),
    timeout_seconds: str = Form("600"),
    limit_runs: str = Form("0"),
    collect_usage: bool = Form(False),
    export_xlsx: bool = Form(False),
    allow_tools: bool = Form(False),
) -> HTMLResponse:
    try:
        # 没有模式行就回落成单产品单模型那一套，旧表单和 curl 脚本照常能用。
        modes = _modes(mode_product, mode_model)
        if not modes:
            if product not in DRIVERS:
                raise ValueError(f"没有名为 {product!r} 的产品驱动")
            modes = [PlanMode(product=product, model_query=model_query)]

        selected = load_catalog(CASE_CATALOG_PATH).get(case_set_id)
        # Web 和 Worker 使用同一套环境；这里提前拦住未配置的路径占位符。
        resolve_case_set(selected)
        # 实验名是报告的主键来源（suite_id 就是它的哈希），
        # 编码一旦错了，同一个实验会裂成两份报告。
        name = _text_field(experiment_name, "实验名称")
        timeout = _number_field(timeout_seconds, "单轮超时", default=600)
        limit = int(_number_field(limit_runs, "最多运行轮次", default=0))

        if len(modes) == 1:
            job = create_job(
                NewJob(
                    experiment_name=name,
                    case_set_id=case_set_id,
                    case_catalog_path=str(CASE_CATALOG_PATH),
                    product=modes[0].product,
                    model_query=modes[0].model_query,
                    timeout_seconds=timeout,
                    limit_runs=limit,
                    collect_usage=collect_usage,
                    export_xlsx=export_xlsx,
                    allow_tools=allow_tools,
                )
            )
            return RedirectResponse(f"/jobs/{job['job_id']}", status_code=303)

        plan = create_plan(
            NewPlan(
                experiment_name=name,
                case_set_id=case_set_id,
                modes=tuple(modes),
                case_catalog_path=str(CASE_CATALOG_PATH),
                timeout_seconds=timeout,
                limit_runs=limit,
                collect_usage=collect_usage,
                export_xlsx=export_xlsx,
                allow_tools=allow_tools,
            )
        )
    except (CaseCatalogError, ValueError) as exc:
        return _render(
            request,
            "new_job.html",
            **_job_form_context(str(exc)),
            status_code=400,
        )
    return RedirectResponse(f"/plans/{plan['plan_id']}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str) -> HTMLResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="没有这个任务")
    return _render(request, "job.html", job=job)


@app.get("/jobs/{job_id}/status", response_class=HTMLResponse)
def job_status(request: Request, job_id: str) -> HTMLResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="没有这个任务")
    response = _render(
        request,
        "_job_status.html",
        job=job,
        events=list_events(job_id),
    )
    if job["status"] in {"Completed", "Failed", "Cancelled"}:
        response.headers["X-Benchmark-Poll"] = "stop"
    return response


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> RedirectResponse:
    if not get_job(job_id):
        raise HTTPException(status_code=404, detail="没有这个任务")
    request_cancel(job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


TERMINAL_JOB_STATUSES = frozenset({"Completed", "Failed", "Cancelled"})


def _plan_view(plan_id: str) -> dict[str, Any]:
    """一个计划的聚合视图。

    **聚合的只有进度，不是判定。** 哪个模式跑得好由报告页按断言结果说话；
    这里把 N 个模式的成败混成一个总状态就等于在控制面上做判定了。
    所以 `finished` 只回答「还要不要继续轮询」。
    """
    jobs = plan_jobs(plan_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="没有这个测试计划")
    return {
        "plan_id": plan_id,
        "experiment_name": str(jobs[0]["experiment_name"]),
        "case_set_id": str(jobs[0]["case_set_id"]),
        "jobs": jobs,
        # 同一个实验名 → 同一个 suite_id，所以任意一个跑完入库的模式
        # 都指向那份合并后的对比报告。
        "suite_id": next((job["suite_id"] for job in jobs if job.get("suite_id")), None),
        "total_runs": sum(int(job.get("total_runs") or 0) for job in jobs),
        "completed_runs": sum(int(job.get("completed_runs") or 0) for job in jobs),
        "done_modes": sum(1 for job in jobs if job["status"] in TERMINAL_JOB_STATUSES),
        "finished": all(job["status"] in TERMINAL_JOB_STATUSES for job in jobs),
    }


@app.get("/plans/{plan_id}", response_class=HTMLResponse)
def plan_detail(request: Request, plan_id: str) -> HTMLResponse:
    return _render(request, "plan.html", plan=_plan_view(plan_id))


@app.get("/plans/{plan_id}/status", response_class=HTMLResponse)
def plan_status(request: Request, plan_id: str) -> HTMLResponse:
    plan = _plan_view(plan_id)
    response = _render(request, "_plan_status.html", plan=plan)
    if plan["finished"]:
        response.headers["X-Benchmark-Poll"] = "stop"
    return response


@app.post("/plans/{plan_id}/cancel")
def cancel_plan_route(plan_id: str) -> RedirectResponse:
    _plan_view(plan_id)  # 不存在就 404，不要静默成功
    cancel_plan(plan_id)
    return RedirectResponse(f"/plans/{plan_id}", status_code=303)


@app.get("/suite/{suite_id}", response_class=HTMLResponse)
def suite(request: Request, suite_id: str) -> HTMLResponse:
    found = queries.get_suite(suite_id)
    if not found:
        raise HTTPException(status_code=404, detail="没有这个 suite")
    return _render(
        request,
        "suite.html",
        suite=found,
        modes=queries.mode_summary(suite_id),
    )


@app.get("/matrix/{suite_id}", response_class=HTMLResponse)
def matrix(request: Request, suite_id: str) -> HTMLResponse:
    found = queries.get_suite(suite_id)
    if not found:
        raise HTTPException(status_code=404, detail="没有这个 suite")
    modes, table = queries.matrix(suite_id)
    return _render(request, "matrix.html", suite=found, modes=modes, table=table)


@app.get("/reconcile/{suite_id}", response_class=HTMLResponse)
def reconcile(request: Request, suite_id: str) -> HTMLResponse:
    found = queries.get_suite(suite_id)
    if not found:
        raise HTTPException(status_code=404, detail="没有这个 suite")
    return _render(
        request, "reconcile.html", suite=found, rows=queries.reconcile(suite_id)
    )


@app.get("/run/{benchmark_id}", response_class=HTMLResponse)
def run_detail(request: Request, benchmark_id: str) -> HTMLResponse:
    run = queries.get_run(benchmark_id)
    if not run:
        raise HTTPException(status_code=404, detail="没有这一轮")
    return _render(
        request,
        "run.html",
        run=run,
        checks=queries.get_checks(benchmark_id),
        usage=queries.get_usage(benchmark_id),
        model_calls=queries.get_model_requests(benchmark_id),
    )


@app.get("/run/{benchmark_id}/transcript", response_class=HTMLResponse)
def transcript(request: Request, benchmark_id: str) -> HTMLResponse:
    """局部加载 SSE 原始流，按行展开以复盘终止判定。"""
    run = queries.get_run(benchmark_id)
    if not run:
        raise HTTPException(status_code=404, detail="没有这一轮")

    path = run.get("transcript_path")
    events: list[dict[str, Any]] = []
    problem = ""
    if not path:
        problem = "这一轮没保存 SSE 原始流（跑批时带了 --no-transcript？）"
    elif not Path(path).is_file():
        problem = f"文件不在了：{path}"
    else:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(
                {"event": payload.get("event", ""), "data": payload.get("data", "")}
            )

    return _render(request, "_transcript.html", events=events, problem=problem)
