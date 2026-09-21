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
    active_job,
    create_job,
    get_job,
    list_events,
    list_jobs,
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


@app.post("/jobs")
def submit_job(
    request: Request,
    experiment_name: str = Form(...),
    case_set_id: str = Form(...),
    product: str = Form("yonwork"),
    model_query: str = Form(""),
    timeout_seconds: str = Form("600"),
    limit_runs: str = Form("0"),
    collect_usage: bool = Form(False),
    export_xlsx: bool = Form(False),
) -> HTMLResponse:
    try:
        if product not in DRIVERS:
            raise ValueError(f"没有名为 {product!r} 的产品驱动")
        selected = load_catalog(CASE_CATALOG_PATH).get(case_set_id)
        # Web 和 Worker 使用同一套环境；这里提前拦住未配置的路径占位符。
        resolve_case_set(selected)
        job = create_job(
            NewJob(
                experiment_name=experiment_name,
                case_set_id=case_set_id,
                case_catalog_path=str(CASE_CATALOG_PATH),
                product=product,
                model_query=model_query,
                timeout_seconds=_number_field(timeout_seconds, "单轮超时", default=600),
                limit_runs=int(_number_field(limit_runs, "最多运行轮次", default=0)),
                collect_usage=collect_usage,
                export_xlsx=export_xlsx,
            )
        )
    except (CaseCatalogError, ValueError) as exc:
        return _render(
            request,
            "new_job.html",
            **_job_form_context(str(exc)),
            status_code=400,
        )
    return RedirectResponse(f"/jobs/{job['job_id']}", status_code=303)


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
