"""로컬 전용 HTTP 서버 (PRD 6.1).

``127.0.0.1``에만 바인딩한다. 외부에 노출되지 않으며 인증도 두지 않는다.
긴 작업은 스레드로 돌리고 브라우저는 SSE로 진행 상황을 본다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from tcgen import __version__
from tcgen.config import MAX_STRESS_ROUNDS
from tcgen.engine.pipeline import Pipeline, PipelineError
from tcgen.jobs import JobConflict, SessionRegistry, WorkspaceSession
from tcgen.llm import ClaudeCLI, LLMError
from tcgen.models import CasePlan, Example, Figure, Limits, Problem
from tcgen.runner.env import probe_environment
from tcgen.workspace import (
    WorkspaceError,
    Workspace,
    create_workspace,
    list_workspaces,
    normalize_slug,
    open_workspace,
    slugify,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
_registry = SessionRegistry()

#: 이 서버를 가리키는 것으로 인정하는 호스트 이름. DNS 리바인딩 방어용이다.
_ALLOWED_HOSTNAMES: set[str] = {"127.0.0.1", "localhost", "::1"}
_SSE_POLL_SECONDS = 1.0
_MAX_RAW_TEXT = 500_000

#: PDF 지문 업로드 상한. 지문 몇 쪽이면 충분하고, 그 이상은 CLI가 읽지 못한다.
MAX_PDF_BYTES = 20 * 1024 * 1024
#: PDF 파일의 머리 네 바이트. 확장자나 Content-Type은 클라이언트가 말하는 대로다.
_PDF_MAGIC = b"%PDF"
_PDF_CHUNK_BYTES = 1024 * 1024


def allow_hostname(hostname: str) -> None:
    """``--host``로 다른 주소에 바인딩할 때 그 이름도 인정한다."""
    _ALLOWED_HOSTNAMES.add(hostname.strip("[]").lower())


class LocalOnlyGuard(BaseHTTPMiddleware):
    """로컬 전용 서버를 브라우저발 교차 출처 요청에서 보호한다.

    ``127.0.0.1`` 바인딩은 외부에서 **접속**하는 것만 막는다. 사용자가 열어 둔
    아무 웹페이지나 이 주소로 요청을 보낼 수 있고, 그 요청 하나로 코드 생성·
    실행 파이프라인이 기동된다. 그래서 두 가지를 본다.

    - ``Host``가 이 서버를 가리키는지 (DNS 리바인딩 차단)
    - 상태를 바꾸는 요청이 같은 출처에서 왔는지 (CSRF 차단)

    curl이나 테스트처럼 브라우저가 아닌 클라이언트는 ``Origin``과
    ``Sec-Fetch-Site``를 보내지 않으므로 그대로 통과한다.
    """

    async def dispatch(self, request: Request, call_next):
        hostname = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if hostname and hostname not in _ALLOWED_HOSTNAMES:
            return JSONResponse({"detail": "허용되지 않은 Host 헤더입니다."}, status_code=421)

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site is not None and fetch_site != "same-origin":
                return JSONResponse({"detail": "교차 출처 요청을 거부했습니다."}, status_code=403)
            origin = request.headers.get("origin")
            if origin is not None and _origin_hostname(origin) not in _ALLOWED_HOSTNAMES:
                return JSONResponse({"detail": "교차 출처 요청을 거부했습니다."}, status_code=403)
        return await call_next(request)


def _origin_hostname(origin: str) -> str:
    without_scheme = origin.split("://", 1)[-1]
    return without_scheme.rsplit(":", 1)[0].strip("[]").lower()


class ProblemCreate(BaseModel):
    title: str = Field(min_length=1)


class ProblemUpdate(BaseModel):
    """폼 수정 요청. 수치는 상·하한을 둔다.

    하한이 없으면 ``stress_rounds=0``으로 브루트포스 대조를 건너뛰고 정답을
    확정시킬 수 있고, ``max_input_bytes``가 크면 출력 상한이 무력화된다.
    """

    title: str | None = Field(default=None, max_length=300)
    statement: str | None = Field(default=None, max_length=200_000)
    input_spec: str | None = Field(default=None, max_length=50_000)
    output_spec: str | None = Field(default=None, max_length=50_000)
    constraints: str | None = Field(default=None, max_length=50_000)
    hints: str | None = Field(default=None, max_length=50_000)
    examples: list[dict[str, str]] | None = Field(default=None, max_length=50)
    figures: list[dict[str, Any]] | None = Field(default=None, max_length=30)
    time_ms: int | None = Field(default=None, ge=1, le=60_000)
    memory_mb: int | None = Field(default=None, ge=1, le=4096)
    case_count: int | None = Field(default=None, ge=1, le=500)
    max_input_bytes: int | None = Field(default=None, ge=1, le=64 * 1024 * 1024)
    stress_rounds: int | None = Field(default=None, ge=1, le=MAX_STRESS_ROUNDS)


class ParseRequest(BaseModel):
    raw_text: str = Field(min_length=1, max_length=_MAX_RAW_TEXT)


class SolutionRequest(BaseModel):
    author_source: str | None = None


class SettingsRequest(BaseModel):
    model: str | None = None


_settings: dict[str, Any] = {"model": None}


def create_app() -> FastAPI:
    app = FastAPI(title="TC_generator", version=__version__)
    app.add_middleware(LocalOnlyGuard)

    # ------------------------------------------------------------- 환경·목록

    @app.get("/api/env")
    def get_env() -> dict[str, Any]:
        report = probe_environment()
        payload = report.to_dict()
        payload["model"] = _settings["model"]
        return payload

    @app.post("/api/settings")
    def update_settings(request: SettingsRequest) -> dict[str, Any]:
        _settings["model"] = request.model or None
        return {"model": _settings["model"]}

    @app.get("/api/problems")
    def get_problems() -> list[dict[str, Any]]:
        return [_summary(workspace) for workspace in list_workspaces()]

    @app.post("/api/problems")
    def post_problem(request: ProblemCreate) -> dict[str, Any]:
        problem = Problem(slug=slugify(request.title), title=request.title)
        workspace = create_workspace(problem)
        return _summary(workspace)

    @app.get("/api/problems/{slug}")
    def get_problem(slug: str) -> dict[str, Any]:
        workspace = _workspace(slug)
        session = _session(slug)
        return {
            "problem": workspace.load_problem().to_dict(),
            "cases": [record.to_dict() for record in workspace.load_cases_index()],
            "job": session.job.to_dict(),
            "report": _report_text(workspace),
            "has_zip": _zip_path(workspace).exists(),
        }

    @app.put("/api/problems/{slug}")
    def put_problem(slug: str, request: ProblemUpdate) -> dict[str, Any]:
        workspace = _workspace(slug)
        updated = _apply_update(workspace.load_problem(), request)
        workspace.save_problem(updated)
        return updated.to_dict()

    # ----------------------------------------------------------------- 단계

    @app.post("/api/problems/{slug}/parse")
    def post_parse(slug: str, request: ParseRequest) -> dict[str, str]:
        return _launch(slug, "parse", lambda pipeline: pipeline.parse_statement(request.raw_text))

    @app.post("/api/problems/{slug}/source-pdf")
    async def post_source_pdf(slug: str, file: UploadFile) -> dict[str, Any]:
        """그림이 든 지문을 PDF로 올린다 (PRD F1-7).

        업로드된 파일 이름은 쓰지 않는다. 작업공간 안의 고정된 이름으로만
        저장해야 경로 조작과 예측 불가능한 ``--add-dir`` 범위를 둘 다 막는다.
        """
        workspace = _workspace(slug)
        workspace.assets_dir.mkdir(parents=True, exist_ok=True)
        await _store_pdf(file, workspace.source_pdf_path)
        problem = workspace.load_problem().with_changes(
            source_pdf=workspace.source_pdf_path.name
        )
        workspace.save_problem(problem)
        return {"source_pdf": problem.source_pdf, "bytes": workspace.source_pdf_path.stat().st_size}

    @app.post("/api/problems/{slug}/parse-pdf")
    def post_parse_pdf(slug: str) -> dict[str, str]:
        workspace = _workspace(slug)
        if not workspace.source_pdf_path.is_file():
            raise HTTPException(status_code=409, detail="PDF 지문을 먼저 올려 주세요.")
        return _launch(slug, "parse-pdf", lambda pipeline: pipeline.parse_pdf())

    @app.post("/api/problems/{slug}/interpret")
    def post_interpret(slug: str) -> dict[str, str]:
        return _launch(slug, "interpret", lambda pipeline: pipeline.interpret())

    @app.post("/api/problems/{slug}/confirm-interpretation")
    def post_confirm(slug: str) -> dict[str, Any]:
        workspace = _workspace(slug)
        problem = workspace.load_problem().with_changes(interpretation_confirmed=True)
        workspace.save_problem(problem)
        return problem.to_dict()

    @app.post("/api/problems/{slug}/solution")
    def post_solution(slug: str, request: SolutionRequest) -> dict[str, str]:
        return _launch(
            slug,
            "solution",
            lambda pipeline: pipeline.build_solution(request.author_source).to_dict(),
        )

    @app.post("/api/problems/{slug}/cases")
    def post_cases(slug: str) -> dict[str, str]:
        return _launch(slug, "cases", lambda pipeline: pipeline.build_cases().to_dict())

    @app.post("/api/problems/{slug}/strength")
    def post_strength(slug: str) -> dict[str, str]:
        return _launch(slug, "strength", lambda pipeline: pipeline.verify_strength().to_dict())

    @app.post("/api/problems/{slug}/export")
    def post_export(slug: str) -> dict[str, str]:
        def work(pipeline: Pipeline) -> dict[str, Any]:
            result = pipeline.export()
            pipeline.write_report()
            return result.to_dict()

        return _launch(slug, "export", work)

    @app.post("/api/problems/{slug}/run-all")
    def post_run_all(slug: str) -> dict[str, str]:
        return _launch(slug, "run-all", lambda pipeline: pipeline.run_all())

    @app.post("/api/problems/{slug}/cancel")
    def post_cancel(slug: str) -> dict[str, bool]:
        return {"cancelled": _session(slug).cancel()}

    # -------------------------------------------------------------- 산출물

    @app.get("/api/problems/{slug}/events")
    async def get_events(slug: str, request: Request) -> StreamingResponse:
        session = _session(slug)
        return StreamingResponse(
            _event_stream(session, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/problems/{slug}/report", response_class=PlainTextResponse)
    def get_report(slug: str) -> str:
        return _report_text(_workspace(slug)) or "아직 리포트가 없습니다."

    @app.get("/api/problems/{slug}/case/{index}")
    def get_case(slug: str, index: int) -> dict[str, str]:
        workspace = _workspace(slug)
        input_path = workspace.input_path(index)
        output_path = workspace.output_path(index)
        if not input_path.exists():
            raise HTTPException(status_code=404, detail="케이스를 찾을 수 없습니다.")
        return {
            "input": _preview(input_path),
            "output": _preview(output_path) if output_path.exists() else "",
        }

    @app.get("/api/problems/{slug}/download")
    def get_download(slug: str) -> FileResponse:
        path = _zip_path(_workspace(slug))
        if not path.exists():
            raise HTTPException(status_code=404, detail="아직 내보내지 않았습니다.")
        return FileResponse(path, filename=path.name, media_type="application/zip")

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


# ----------------------------------------------------------------- 내부 도구


async def _store_pdf(file: UploadFile, destination: Path) -> None:
    """PDF를 조각으로 읽어 저장한다. 통째로 메모리에 올리지 않는다.

    상한을 넘거나 PDF가 아니면 받은 만큼을 지우고 거절한다. 반쯤 쓰다 만
    파일이 남으면 다음 파싱이 그것을 지문으로 읽는다.
    """
    total = 0
    try:
        with destination.open("wb") as sink:
            while chunk := await file.read(_PDF_CHUNK_BYTES):
                if total == 0 and not chunk.startswith(_PDF_MAGIC):
                    raise HTTPException(status_code=400, detail="PDF 파일이 아닙니다.")
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"PDF가 너무 큽니다. "
                            f"{MAX_PDF_BYTES // (1024 * 1024)}MB 이하로 올려 주세요."
                        ),
                    )
                sink.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="빈 파일입니다.")
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"PDF를 저장하지 못했습니다: {error}") from error


def _workspace(slug: str) -> Workspace:
    try:
        return open_workspace(slug)
    except WorkspaceError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _session(slug: str):
    """작업 세션을 정규화된 이름으로 가져온다.

    URL 표기가 달라도(대소문자 등) 같은 폴더면 같은 세션이어야 한다. 아니면
    한 작업폴더에서 두 작업이 동시에 돈다.
    """
    try:
        return _registry.get(normalize_slug(slug))
    except WorkspaceError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _launch(
    slug: str, name: str, work: Callable[[Pipeline], Any]
) -> dict[str, str]:
    workspace = _workspace(slug)
    session = _session(slug)

    def job(cancel_event: threading.Event) -> Any:
        pipeline = _build_pipeline(workspace, session, cancel_event)
        try:
            return work(pipeline)
        except (PipelineError, LLMError) as error:
            raise RuntimeError(str(error)) from error

    try:
        session.start(name, job)
    except JobConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"status": "started", "stage": name}


def _build_pipeline(
    workspace: Workspace, session: WorkspaceSession, cancel_event: threading.Event
) -> Pipeline:
    cli = ClaudeCLI(model=_settings["model"], log_dir=workspace.logs_dir)
    return Pipeline(workspace, cli, bus=session.bus, cancel_event=cancel_event)


async def _event_stream(session: WorkspaceSession, request: Request) -> AsyncIterator[str]:
    """SSE 스트림. 구독 시점까지의 기록을 먼저 보낸다.

    블로킹 제너레이터를 쓰면 Starlette가 스레드풀에 올려 두고, 이벤트가 없는
    동안 그 스레드가 영구히 묶인다. 탭을 몇십 번 열면 서버 전체가 멈춘다.
    짧은 타임아웃으로 폴링하면서 연결 종료를 확인한다.
    """
    backlog, subscriber = session.bus.attach()
    try:
        for event in backlog:
            yield _sse(event.to_dict())
        while True:
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.to_thread(subscriber.get, True, _SSE_POLL_SECONDS)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if event is None:
                return
            yield _sse(event.to_dict())
    finally:
        session.bus.detach(subscriber)


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _summary(workspace: Workspace) -> dict[str, Any]:
    problem = workspace.load_problem()
    return {
        "slug": problem.slug,
        "title": problem.title,
        "solution_confirmed": problem.solution_confirmed,
        "interpretation_confirmed": problem.interpretation_confirmed,
        "case_count": len(workspace.load_cases_index()),
        "has_zip": _zip_path(workspace).exists(),
    }


#: 이 필드들이 바뀌면 기존 정답 확정은 다른 문제에 대한 것이 된다.
_SOLUTION_CRITICAL_FIELDS = (
    "statement",
    # 그림 전사는 풀이의 전제다. 바뀌면 기존 확정은 다른 해석에 대한 것이 된다.
    "figures",
    "input_spec",
    "output_spec",
    "constraints",
    "examples",
    "time_ms",
    "memory_mb",
)


def _apply_update(problem: Problem, request: ProblemUpdate) -> Problem:
    data = request.model_dump(exclude_none=True)
    limits = Limits(
        time_ms=data.get("time_ms", problem.limits.time_ms),
        memory_mb=data.get("memory_mb", problem.limits.memory_mb),
        assumed=False if {"time_ms", "memory_mb"} & data.keys() else problem.limits.assumed,
    )
    case_plan = CasePlan(
        count=data.get("case_count", problem.case_plan.count),
        max_input_bytes=data.get("max_input_bytes", problem.case_plan.max_input_bytes),
        stress_rounds=data.get("stress_rounds", problem.case_plan.stress_rounds),
    )
    examples = (
        tuple(
            Example(input=item.get("input", ""), output=item.get("output", ""))
            for item in data["examples"]
        )
        if "examples" in data
        else problem.examples
    )
    figures = (
        tuple(Figure.from_dict(item) for item in data["figures"])
        if "figures" in data
        else problem.figures
    )
    text_fields = {
        key: data[key]
        for key in ("title", "statement", "input_spec", "output_spec", "constraints", "hints")
        if key in data
    }
    updated = problem.with_changes(
        limits=limits,
        case_plan=case_plan,
        examples=examples,
        figures=figures,
        **text_fields,
    )
    if _solution_inputs_changed(problem, updated, data):
        # 문제가 바뀌었는데 확정 표시가 남아 있으면, A로 확정한 코드로 B의
        # 테스트를 내보내게 된다.
        return updated.with_changes(solution_confirmed=False, interpretation_confirmed=False)
    return updated


def _solution_inputs_changed(before: Problem, after: Problem, data: dict[str, Any]) -> bool:
    if not any(field in data for field in _SOLUTION_CRITICAL_FIELDS):
        return False
    return (
        before.statement != after.statement
        or before.input_spec != after.input_spec
        or before.output_spec != after.output_spec
        or before.constraints != after.constraints
        or before.examples != after.examples
        or before.figures != after.figures
        or before.limits != after.limits
    )


def _zip_path(workspace: Workspace) -> Path:
    return workspace.export_dir / f"{workspace.slug}_testcases.zip"


def _report_text(workspace: Workspace) -> str:
    path = workspace.reports_dir / "report.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


_PREVIEW_LIMIT = 4000


def _preview(path: Path) -> str:
    data = path.read_bytes()[: _PREVIEW_LIMIT + 1]
    text = data.decode("utf-8", errors="replace")
    if len(text) > _PREVIEW_LIMIT:
        return text[:_PREVIEW_LIMIT] + "\n… (이하 생략)"
    return text


app = create_app()
