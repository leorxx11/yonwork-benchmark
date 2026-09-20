from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from runner.db import DatabaseError

from . import queries


BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="YonWork 基准测试", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

VERDICT_ORDER = ("Pass", "Fail", "Timeout", "Error", "Invalid")


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) / 1000:.2f}s"


def _fmt_int(value: Any) -> str:
    return "—" if value is None else f"{int(value):,}"


templates.env.filters["ms"] = _fmt_ms
templates.env.filters["num"] = _fmt_int


def _render(request: Request, name: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name=name, context={"verdict_order": VERDICT_ORDER, **context}
    )


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
    """HTMX 局部加载：SSE 原始流按行展开，用来复盘终止判定。"""
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
