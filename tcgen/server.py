"""로컬 전용 HTTP 서버 (PRD 6.1).

``127.0.0.1``에만 바인딩한다. 외부에 노출되지 않으며 인증도 두지 않는다.
긴 작업은 스레드로 돌리고 브라우저는 SSE로 진행 상황을 본다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
from pathlib import Path
from html import escape
from typing import Any, AsyncIterator, Callable, Literal

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from tcgen import __version__, boj, drive, figure_preview, history, images, pricing, problem_export, exporter, workspace as workspace_module
from tcgen.batch import BatchManager, MAX_BATCH_SIZE
from tcgen.config import BOJ_MAX_PROBLEM_ID, BOJ_MIN_PROBLEM_ID, MAX_STRESS_ROUNDS, MIN_CASE_COUNT
from tcgen.engine.pipeline import Pipeline, PipelineError
from tcgen.jobs import Cancelled, JobConflict, SessionRegistry, WorkspaceSession
from tcgen.llm import LLMError, make_cli
from tcgen.codex import codex_auth_status
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
SETTINGS_PATH = STATIC_DIR.parent.parent / ".tcgen-settings.json"
_registry = SessionRegistry()
_batch_managers: dict[Path, BatchManager] = {}
_batch_lock = threading.Lock()
_bundle_lock = threading.Lock()
_settings_lock = threading.RLock()

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


class BojImportRequest(BaseModel):
    """백준 수집본에서 문제를 가져오는 요청. 번호 하나면 된다."""

    problem_id: int = Field(ge=BOJ_MIN_PROBLEM_ID, le=BOJ_MAX_PROBLEM_ID)


class UploadRequest(BaseModel):
    uploaded: bool


class ManifestRequest(BaseModel):
    """내려받을 대상. 일괄 실행 묶음(batch_id) 또는 문제 하나(slug)."""
    batch_id: str | None = Field(default=None, max_length=64)
    slug: str | None = Field(default=None, max_length=200)


class ManifestConfirmRequest(BaseModel):
    problem_ids: list[int] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


class BatchRequest(BaseModel):
    problem_ids: list[int] = Field(min_length=1, max_length=MAX_BATCH_SIZE)
    skip_exported: bool = True


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
    examples: list[dict[str, str]] | None = Field(default=None, max_length=200)
    figures: list[dict[str, Any]] | None = Field(default=None, max_length=30)
    time_ms: int | None = Field(default=None, ge=1, le=60_000)
    memory_mb: int | None = Field(default=None, ge=1, le=4096)
    case_count: int | None = Field(default=None, ge=MIN_CASE_COUNT, le=500)
    max_input_bytes: int | None = Field(default=None, ge=1, le=64 * 1024 * 1024)
    stress_rounds: int | None = Field(default=None, ge=1, le=MAX_STRESS_ROUNDS)


class ParseRequest(BaseModel):
    raw_text: str = Field(min_length=1, max_length=_MAX_RAW_TEXT)


class SolutionRequest(BaseModel):
    author_source: str | None = None


class SettingsRequest(BaseModel):
    provider: Literal["claude", "codex"] | None = None
    model: str | None = Field(default=None, max_length=200)
    effort: str | None = Field(default=None, max_length=20)
    drive_folder: str | None = Field(default=None, max_length=1000)


def _load_settings() -> dict:
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    provider = saved.get("provider", "claude")
    provider = provider if provider in {"claude", "codex"} else "claude"
    models = saved.get("models") if isinstance(saved.get("models"), dict) else {}
    models.setdefault(provider, saved.get("model"))
    efforts = saved.get("efforts") if isinstance(saved.get("efforts"), dict) else {}
    efforts = {key: value for key, value in efforts.items()
               if key in {"claude", "codex"} and value in pricing.allowed_efforts(key, models.get(key))}
    return {"provider": provider, "models": models, "model": models.get(provider),
            "efforts": efforts, "effort": efforts.get(provider),
            "drive_folder": os.environ.get("TCGEN_DRIVE_FOLDER") or saved.get("drive_folder", "")}


_settings: dict[str, Any] = _load_settings()


def create_app() -> FastAPI:
    app = FastAPI(title="TC_generator", version=__version__)
    app.add_middleware(LocalOnlyGuard)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
    def get_index() -> HTMLResponse:
        return HTMLResponse(_initial_html(), headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------- 환경·목록

    @app.get("/api/env")
    def get_env() -> dict[str, Any]:
        report = probe_environment()
        payload = report.to_dict()
        settings = _llm_settings()
        payload.update(settings)
        payload["models"] = _settings.get("models", {settings["provider"]: settings["model"]})
        payload["model_options"] = pricing.model_options()
        if settings["provider"] == "codex":
            payload["llm_available"] = report.tool("codex").available
        else:
            payload["llm_available"] = report.tool("claude").available
        payload["drive_folder"] = _settings["drive_folder"]
        payload["drive_credentials_available"] = drive.credentials_available()
        return payload

    @app.get("/api/llm-status")
    def get_llm_status() -> dict:
        provider = _llm_settings()["provider"]
        if provider == "codex":
            return {"provider": provider, **codex_auth_status()}
        # 설치 여부만 확인한다. 구독·조직 허용 여부는 실제 요청에서 판정한다.
        import shutil
        available = shutil.which("claude") is not None
        return {"provider": provider, "ready": available,
                "message": "Claude CLI 설치됨 · `claude auth status`로 로그인 상태를 확인하세요. 구독과 조직 사용 허용이 필요합니다."
                if available else "Claude CLI가 없습니다. 설치 후 `claude auth login`으로 로그인해 주세요."}

    @app.post("/api/settings")
    def update_settings(request: SettingsRequest) -> dict[str, Any]:
        with _settings_lock:
            updated = dict(_settings)
            old_provider = updated.get("provider", "claude")
            provider = request.provider or old_provider
            models = dict(updated.get("models", {}))
            models.setdefault(old_provider, updated.get("model"))
            if "model" in request.model_fields_set:
                models[provider] = request.model.strip() or None if request.model else None
            efforts = dict(updated.get("efforts", {}))
            allowed = pricing.allowed_efforts(provider, models.get(provider))
            if "effort" in request.model_fields_set:
                if request.effort and request.effort not in allowed:
                    raise HTTPException(status_code=422, detail=f"이 모델이 지원하지 않는 effort입니다: {request.effort}")
                efforts[provider] = request.effort or None
            elif efforts.get(provider) not in (None, *allowed):
                # 모델을 바꿔 기존 effort를 쓸 수 없게 되면 기본값으로 돌린다.
                efforts[provider] = None
            updated.update(provider=provider, models=models, model=models.get(provider),
                           efforts=efforts, effort=efforts.get(provider))
            if request.drive_folder is not None:
                try:
                    updated["drive_folder"] = drive.folder_id(request.drive_folder) if request.drive_folder else ""
                except drive.DriveError as error:
                    raise HTTPException(status_code=400, detail=str(error)) from error
            staging = SETTINGS_PATH.with_suffix(".part")
            staging.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            staging.replace(SETTINGS_PATH)
            _settings.update(updated)
            return dict(updated)

    @app.get("/api/boj")
    def get_boj_list(q: str = Query(default="", max_length=200),
                     offset: int = Query(default=0, ge=0),
                     limit: int = Query(default=10, ge=1, le=200),
                     status: Literal["all", "uploaded", "not_uploaded", "pending_upload"] = "all") -> dict:
        try:
            uploaded, pending = _upload_groups()
            # 업로드 완료·업로드 대기·미업로드는 서로 겹치지 않고 합하면 전체다.
            filters = {"all": {}, "uploaded": {"include": uploaded}, "pending_upload": {"include": pending},
                       "not_uploaded": {"exclude": uploaded | pending}}
            page = _annotate_catalog(boj.list_problems(q, offset=offset, limit=limit, **filters[status]))
            # 필터 단추에 보여 줄 개수. 검색어를 같이 적용한다.
            counts = {key: boj.list_problems(q, offset=0, limit=0, **value)["total"] for key, value in filters.items()}
            return {**page, "status": status, "counts": counts}
        except boj.BojUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.put("/api/boj/{problem_id}/upload")
    def put_upload(problem_id: int, request: UploadRequest) -> dict:
        try:
            item = boj.catalog_entries([problem_id])[0]
        except boj.BojNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except boj.BojError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return history.set_uploaded(workspace_module.WORKSPACES_DIR, problem_id,
                                    request.uploaded, title=item["title"])

    # ------------------------------------------------------- 업로드 확인

    @app.post("/api/upload-manifests")
    def post_upload_manifest(request: ManifestRequest) -> dict:
        """내려줄 zip 묶음을 기록하고 내려받을 주소를 돌려준다. 업로드는 이 기록으로 확인받는다."""
        if bool(request.batch_id) == bool(request.slug):
            raise HTTPException(status_code=422, detail="batch_id와 slug 중 하나만 지정해 주세요.")
        if request.batch_id:
            batch = _batch_manager().snapshot()
            if batch.get("id") != request.batch_id:
                raise HTTPException(status_code=404, detail="일괄 실행 기록을 찾지 못했습니다.")
            entries = _batch_entries(batch)
            kind, url = "batch", f"/api/batches/{request.batch_id}/download"
        else:
            workspace = _workspace(request.slug)
            path = _zip_path(workspace)
            if not path.is_file():
                raise HTTPException(status_code=404, detail="아직 내보내지 않았습니다.")
            problem = workspace.load_problem()
            entries = ([{"problem_id": problem.boj_id, "slug": workspace.slug, "title": problem.title, "path": path}]
                       if problem.boj_id is not None else [])
            kind, url = "single", f"/api/problems/{workspace.slug}/download"
        if not entries and kind == "batch":
            raise HTTPException(status_code=404, detail="내보내기를 완료한 문제 zip이 없습니다.")
        # 백준 번호가 없는 문제는 업로드 기록 대상이 아니다. 내려받기만 한다.
        manifest = history.create_manifest(workspace_module.WORKSPACES_DIR, entries, kind=kind) if entries else None
        return {"manifest": manifest, "download_url": url}

    @app.get("/api/upload-manifests")
    def get_upload_manifests(limit: int = Query(default=10, ge=1, le=50)) -> dict:
        return {"manifests": history.recent_manifests(workspace_module.WORKSPACES_DIR, limit=limit)}

    @app.post("/api/upload-manifests/{manifest_id}/confirm")
    def confirm_upload_manifest(manifest_id: str, request: ManifestConfirmRequest) -> dict:
        try:
            return history.confirm_manifest(workspace_module.WORKSPACES_DIR, manifest_id, request.problem_ids)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="내려받기 기록을 찾지 못했습니다.") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/upload-manifests/{manifest_id}/undo")
    def undo_upload_manifest(manifest_id: str) -> dict:
        try:
            return history.undo_manifest(workspace_module.WORKSPACES_DIR, manifest_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="내려받기 기록을 찾지 못했습니다.") from error

    @app.get("/api/boj/{problem_id}")
    def get_boj_preview(problem_id: int) -> dict:
        try:
            imported = boj.load_boj_problem(problem_id)
        except boj.BojNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except boj.BojUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except boj.BojError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            preview = figure_preview.prepare(imported, _settings["drive_folder"])
        except (ValueError, OSError) as error:
            preview = {"status": "failed", "error": str(error), "images": {}, "key": ""}
        notes = _figure_notes(imported.notes, hide_local_warning=bool(_settings["drive_folder"]) or preview["status"] == "ready")
        return {"problem": imported.problem.to_dict(), "notes": notes,
                "source_url": boj.source_url(problem_id), "figure_preview": preview}

    @app.get("/api/boj/{problem_id}/figure-preview/{key}")
    def get_boj_figure_state(problem_id: int, key: str) -> dict:
        try:
            return figure_preview.state(problem_id, key)
        except (ValueError, OSError, WorkspaceError) as error:
            raise HTTPException(status_code=404, detail="그림 미리보기를 찾지 못했습니다.") from error

    @app.get("/api/boj/{problem_id}/figure-preview/{key}/{number}")
    def get_boj_figure(problem_id: int, key: str, number: int) -> FileResponse:
        try:
            cached = figure_preview.workspace(problem_id, key)
            path = images.find(cached, number) if 1 <= number <= 50 else None
        except (ValueError, OSError, WorkspaceError) as error:
            raise HTTPException(status_code=404, detail="그림을 찾지 못했습니다.") from error
        if path is None:
            raise HTTPException(status_code=404, detail="그림을 아직 받지 못했습니다.")
        return FileResponse(path, headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; sandbox"})

    @app.get("/api/problems")
    def get_problems() -> list[dict[str, Any]]:
        return [_summary(workspace) for workspace in list_workspaces()]

    @app.post("/api/problems")
    def post_problem(request: ProblemCreate) -> dict[str, Any]:
        problem = Problem(slug=slugify(request.title), title=request.title)
        workspace = create_workspace(problem)
        history.record(workspace)
        return _summary(workspace)

    @app.post("/api/problems/from-boj")
    def post_problem_from_boj(request: BojImportRequest) -> dict[str, Any]:
        """백준 번호 하나로 작업공간을 만든다.

        수집본은 지문을 섹션 단위로 갖고 있으므로 이 경로에는 LLM 파싱이
        없다. 대신 자동으로 채우지 못한 것들을 ``notes``로 돌려준다. 빈
        칸을 알리지 않으면 사람은 다 채워진 줄 안다.
        """
        try:
            workspace, notes = boj.import_into_workspace(request.problem_id)
        except boj.BojNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except boj.BojUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except boj.BojError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except OSError as error:
            raise HTTPException(
                status_code=500, detail=f"작업공간을 만들지 못했습니다: {error}"
            ) from error
        history.record(workspace)
        copied = figure_preview.copy_cached(workspace.load_problem(), _settings["drive_folder"], workspace)
        expected = len(workspace.load_problem().figures)
        ready = all(images.find(workspace, n) for n in range(1, expected + 1))
        queued = bool(_settings["drive_folder"] and expected and not ready)
        if queued:
            _launch_drive_images(workspace.slug)
        return {
            **_summary(workspace),
            "notes": _figure_notes(notes, hide_local_warning=bool(copied or queued)),
            "source_url": boj.source_url(request.problem_id),
            "image_download": _image_download_state(workspace, _session(workspace.slug).job.to_dict()),
        }

    @app.get("/api/problems/{slug}")
    def get_problem(slug: str) -> dict[str, Any]:
        workspace = _workspace(slug)
        session = _session(slug)
        return {
            "problem": workspace.load_problem().to_dict(),
            "cases": [record.to_dict() for record in workspace.load_cases_index()],
            "job": session.job.to_dict(),
            "image_download": _image_download_state(workspace, session.job.to_dict()),
            "report": _report_text(workspace),
            "has_zip": _zip_path(workspace).exists(),
            "results": _run_results(workspace),
            "images": {str(number): f"/api/problems/{slug}/figures/{number}"
                       for number in range(1, len(workspace.load_problem().figures) + 1)
                       if images.find(workspace, number)},
        }

    @app.put("/api/problems/{slug}")
    def put_problem(slug: str, request: ProblemUpdate) -> dict[str, Any]:
        _ensure_idle(slug)
        workspace = _workspace(slug)
        before = workspace.load_problem()
        updated = _apply_update(before, request)
        workspace.save_problem(updated)
        if _solution_inputs_changed(before, updated, request.model_dump(exclude_none=True)):
            _invalidate_results(workspace, problem_changed=True)
        elif before.case_plan != updated.case_plan or before.title != updated.title:
            _invalidate_results(workspace)
        history.record(workspace)
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
        _ensure_idle(slug)
        workspace = _workspace(slug)
        workspace.assets_dir.mkdir(parents=True, exist_ok=True)
        await _store_pdf(file, workspace.source_pdf_path)
        problem = workspace.load_problem().with_changes(
            source_pdf=workspace.source_pdf_path.name
        )
        workspace.save_problem(problem)
        return {"source_pdf": problem.source_pdf, "bytes": workspace.source_pdf_path.stat().st_size}

    @app.post("/api/problems/{slug}/figures/{number}")
    async def post_figure(slug: str, number: int, file: UploadFile) -> dict:
        _ensure_idle(slug)
        workspace = _workspace(slug)
        problem = workspace.load_problem()
        if not 1 <= number <= min(30, len(problem.figures) + 1):
            raise HTTPException(status_code=400, detail="기존 그림 번호 또는 바로 다음 번호를 선택하세요.")
        data = await file.read(boj.MAX_FIGURE_BYTES + 1)
        try:
            images.store(workspace, number, data)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        figures = list(problem.figures)
        if number > len(figures):
            figures.append(Figure(ref=boj.figure_ref(number), role="spec"))
        else:
            figures[number - 1] = Figure(ref=figures[number - 1].ref, role=figures[number - 1].role)
        workspace.save_problem(problem.with_changes(figures=tuple(figures), solution_confirmed=False))
        _invalidate_results(workspace, problem_changed=True)
        return {"number": number, "url": f"/api/problems/{slug}/figures/{number}"}

    @app.get("/api/problems/{slug}/figures/{number}")
    def get_figure(slug: str, number: int) -> FileResponse:
        if not 1 <= number <= 50:
            raise HTTPException(status_code=404, detail="그림이 없습니다.")
        path = images.find(_workspace(slug), number)
        if path is None:
            raise HTTPException(status_code=404, detail="그림이 없습니다.")
        return FileResponse(path, headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; sandbox"})

    @app.post("/api/problems/{slug}/describe-figures")
    def post_describe_figures(slug: str) -> dict:
        return _launch(slug, "describe-figures", lambda pipeline: pipeline.describe_figures())

    @app.post("/api/problems/{slug}/drive-images")
    def post_drive_images(slug: str) -> dict:
        return _launch_drive_images(slug)

    @app.post("/api/problems/{slug}/parse-pdf")
    def post_parse_pdf(slug: str) -> dict[str, str]:
        workspace = _workspace(slug)
        if not workspace.source_pdf_path.is_file():
            raise HTTPException(status_code=409, detail="PDF 지문을 먼저 올려 주세요.")
        return _launch(slug, "parse-pdf", lambda pipeline: pipeline.parse_pdf())

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

    @app.post("/api/problems/{slug}/final")
    def post_final(slug: str) -> dict[str, str]:
        return _launch(slug, "final", lambda pipeline: pipeline.verify_final())

    @app.post("/api/problems/{slug}/export-metadata")
    def post_export_metadata(slug: str) -> dict:
        return _launch(slug, "export-metadata", lambda pipeline: pipeline.prepare_export_metadata())

    @app.post("/api/problems/{slug}/export")
    def post_export(slug: str) -> dict[str, str]:
        def work(pipeline: Pipeline) -> dict[str, Any]:
            result = pipeline.export()
            pipeline.write_report()
            if result.has_errors:
                raise PipelineError("zip 검증에 실패했습니다. 리포트를 확인해 주세요.")
            return result.to_dict()

        return _launch(slug, "export", work)

    @app.post("/api/problems/{slug}/run-all")
    def post_run_all(slug: str, request: SolutionRequest) -> dict[str, str]:
        return _launch(slug, "run-all", lambda pipeline: pipeline.run_all(request.author_source))

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

    @app.get("/api/history")
    def get_history() -> dict:
        root = workspace_module.WORKSPACES_DIR
        entries = history.snapshot(root, synchronize=True)
        return {"entries": entries, "by_problem": history.by_problem(root)}

    @app.get("/api/batches/current")
    def get_batch() -> dict:
        result = _batch_manager().snapshot()
        for item in result["items"]:
            if item["status"] == "running" and item.get("slug"):
                events = _session(item["slug"]).bus.history()
                if events:
                    item["stage"] = events[-1].stage
                    item["message"] = events[-1].message
        archives = _batch_archives(result)
        result["bundle_count"] = len(archives)
        return result

    @app.get("/api/batches/{batch_id}/download")
    def get_batch_download(batch_id: str) -> FileResponse:
        batch = _batch_manager().snapshot()
        if batch.get("id") != batch_id:
            raise HTTPException(status_code=404, detail="일괄 실행 기록을 찾지 못했습니다.")
        archives = _batch_archives(batch)
        if not archives:
            raise HTTPException(status_code=404, detail="내보내기를 완료한 문제 zip이 없습니다.")
        target = workspace_module.WORKSPACES_DIR / f".tcgen-batch-{batch_id}.zip"
        try:
            with _bundle_lock:
                problem_export.bundle(archives, target)
        except (OSError, exporter.ExportError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return FileResponse(target, filename="problem-export.zip", media_type="application/zip")

    @app.post("/api/batches")
    def post_batch(request: BatchRequest) -> dict:
        if len(set(request.problem_ids)) != len(request.problem_ids):
            raise HTTPException(status_code=422, detail="같은 문제를 중복 선택할 수 없습니다.")
        if any(not BOJ_MIN_PROBLEM_ID <= number <= BOJ_MAX_PROBLEM_ID for number in request.problem_ids):
            raise HTTPException(status_code=422, detail="백준 문제 번호 범위를 확인해 주세요.")
        try:
            problems = boj.catalog_entries(request.problem_ids)
            history.snapshot(workspace_module.WORKSPACES_DIR, synchronize=True)
            settings = _llm_settings()
            def worker(number, cancel, progress, skip):
                return _execute_batch_item(number, cancel, progress, skip, llm_settings=settings)
            return _batch_manager().start(problems, worker, skip_exported=request.skip_exported, llm=settings)
        except JobConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except boj.BojNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except boj.BojError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/batches/{batch_id}/cancel")
    def cancel_batch(batch_id: str) -> dict:
        try:
            return _batch_manager().cancel(batch_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    # 진짜 API 라우트를 모두 등록한 뒤, 정적 마운트보다 먼저 걸어 둔다.
    # Starlette는 등록 순서대로 매칭하므로 위의 라우트들이 항상 이긴다.
    @app.api_route(
        "/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False
    )
    def unknown_api_path(rest: str) -> JSONResponse:
        """없는 API 경로를 알아볼 수 있는 404로 돌려준다.

        이것이 없으면 ``/``의 StaticFiles 마운트가 요청을 받아 'Method Not
        Allowed'를 돌려준다. 정적 파일은 요청마다 디스크에서 읽히므로 낡은
        서버 프로세스가 **새 화면을 띄워 주면서 새 라우트는 모르는** 상태가
        되는데, 405만 보고는 그 사실을 알 길이 없다.
        """
        raise HTTPException(
            status_code=404,
            detail=(
                f"알 수 없는 API 경로입니다: /api/{rest} · "
                "코드를 고친 뒤라면 서버를 재시작하세요."
            ),
        )

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


# ----------------------------------------------------------------- 내부 도구


_SPJ_LABEL = ('<span class="problem-label spj" title="스페셜 저지 문제: 정답이 여러 개일 수 있어 '
              '출력 글자 비교로는 맞는 답을 틀렸다고 채점할 수 있습니다.">SPJ</span>')
_UPLOAD_PILLS = {"uploaded": ("green", "업로드 완료"), "stale": ("orange", "재업로드 필요"), "ready": ("blue", "업로드 대기")}


def _initial_status_badges(record: dict | None) -> str:
    """앱이 뜨기 전 첫 화면용 상태 표시. 실제 조작은 앱이 그리는 버튼으로 한다."""
    if not record:
        return '<span class="status-pill tone-gray"><span class="dot"></span>미생성</span>'
    upload = (record.get("upload") or {}).get("status")
    key = upload if upload in _UPLOAD_PILLS else ("ready" if record.get("exported") else None)
    if key is None:
        return '<span class="status-pill tone-gray"><span class="dot"></span>미생성</span>'
    tone, label = _UPLOAD_PILLS[key]
    return f'<span class="status-pill tone-{tone}"><span class="dot"></span>{label}</span>'


def _initial_html() -> str:
    """첫 10개를 HTML에 포함해 별도 fetch·앱 초기화 전에도 목록을 보여 준다."""
    template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    try:
        page = _annotate_catalog(boj.initial_catalog_page())
        items = "".join(
            f'<li class="catalog-row"><input type="checkbox" data-select-id="{item["problem_id"]}" '
            f'aria-label="{item["problem_id"]} 선택">'
            f'<button type="button" data-boj-id="{item["problem_id"]}">'
            f'<span class="problem-number">{item["problem_id"]}</span>'
            f'<span class="problem-name">{escape(item["title"] or "제목 없음")}'
            f'{_SPJ_LABEL if item.get("special_judge") else ""}</span></button>'
            f'{_initial_status_badges(item.get("history"))}</li>'
            for item in page["items"]
        )
        status = f'1–{len(page["items"])}개' if page["items"] else "수집본에 문제가 없습니다."
        template = template.replace(
            '<ul id="catalog-list" class="problem-list catalog-list" data-initial-page=""></ul>',
            '<ul id="catalog-list" class="problem-list catalog-list" '
            f'data-initial-page="{escape(json.dumps(page, ensure_ascii=False), quote=True)}">{items}</ul>',
        )
        if page["has_more"]:
            template = template.replace('id="catalog-next" class="secondary" disabled',
                                        'id="catalog-next" class="secondary"')
    except boj.BojError as error:
        status = escape(str(error))
    template = template.replace("문제 목록을 불러오는 중입니다…", status)
    provider = _llm_settings()["provider"]
    template = template.replace(f'<option value="{provider}">', f'<option value="{provider}" selected>')
    # HTML과 예전 JS가 섞여 초기화가 실패하지 않도록 파일 변경 시 자산 URL도 바꾼다.
    for asset in ("app.js", "style.css"):
        version = (STATIC_DIR / asset).stat().st_mtime_ns
        template = template.replace(f'"{asset}"', f'"{asset}?v={version}"')
    return template


def _upload_groups() -> tuple[set[int], set[int]]:
    """(업로드 완료, 업로드 대기). 업로드 대기는 zip이 있고 업로드하지 않았거나 재업로드가 필요한 문제다."""
    uploaded, pending = set(), set()
    for key, record in history.by_problem(workspace_module.WORKSPACES_DIR).items():
        status = record["upload"]["status"]
        if status == history.UPLOAD_DONE:
            uploaded.add(int(key))
        elif status == history.UPLOAD_STALE or record["exported"]:
            pending.add(int(key))
    return uploaded, pending


def _annotate_catalog(page: dict) -> dict:
    records = history.by_problem(workspace_module.WORKSPACES_DIR,
                                 problem_ids=[item["problem_id"] for item in page["items"]])
    return {**page, "items": [{**item, "history": records.get(str(item["problem_id"]))} for item in page["items"]]}


def _batch_entries(batch: dict) -> list[dict]:
    """묶음 zip에 들어갈 문제들. 묶음 내려받기와 업로드 확인이 같은 목록을 쓴다."""
    entries, seen = [], set()
    for item in batch.get("items", []):
        if item["status"] in {"exported", "skipped"} and item.get("slug"):
            try:
                path = problem_export.destination(open_workspace(item["slug"]))
            except WorkspaceError:
                continue
            if path.is_file() and path not in seen:
                seen.add(path)
                entries.append({"problem_id": item["problem_id"], "slug": item["slug"],
                                "title": item.get("title", ""), "path": path})
    return entries


def _batch_archives(batch: dict) -> list[Path]:
    return [entry["path"] for entry in _batch_entries(batch)]


def _batch_manager() -> BatchManager:
    root = workspace_module.WORKSPACES_DIR
    with _batch_lock:
        if root not in _batch_managers:
            _batch_managers[root] = BatchManager(root)
        return _batch_managers[root]


def _execute_batch_item(number: int, cancel: threading.Event, progress: Callable, skip_exported: bool,
                        *, llm_settings: dict | None = None) -> dict:
    root = workspace_module.WORKSPACES_DIR
    entries = [entry for entry in history.snapshot(root) if entry.get("boj_id") == number]
    complete = history.by_problem(root, problem_ids=[number]).get(str(number))
    if skip_exported and complete and complete["completed"]:
        slug = complete.get("slug", "")
        has_zip = bool(slug and _zip_path(open_workspace(slug)).is_file())
        return {"status": "skipped", "slug": slug, "has_zip": has_zip,
                "message": "업로드 완료된 문제" if complete["upload"]["status"] != history.UPLOAD_NONE else "이미 생성된 문제"}
    previous = next((entry for entry in entries if not entry["exported"] and (root / entry["slug"] / "problem.json").is_file()), None)
    if previous:
        workspace = open_workspace(previous["slug"])
    else:
        workspace, _ = boj.import_into_workspace(number)
        figure_preview.copy_cached(workspace.load_problem(), _settings["drive_folder"], workspace)
        history.record(workspace)
    progress(slug=workspace.slug, stage="prepare")
    session = _session(workspace.slug)
    clients: list = []

    def work(local_cancel):
        history.record(workspace, stage="batch", status="running")
        try:
            if cancel.is_set():
                raise Cancelled()
            problem = workspace.load_problem()
            if not all((problem.statement.strip(), problem.input_spec.strip(), problem.output_spec.strip())):
                raise PipelineError("수집본에 문제·입력·출력 설명이 비어 있습니다. 작업을 열어 직접 보완해 주세요.")
            missing = any(not images.find(workspace, n) for n in range(1, len(problem.figures) + 1))
            if missing and _settings["drive_folder"]:
                progress(stage="images", message="Drive에서 그림을 가져오는 중입니다.")
                _fetch_drive(workspace)
            if cancel.is_set() or local_cancel.is_set():
                raise Cancelled()
            pipeline = (_build_pipeline(workspace, session, local_cancel, settings=llm_settings)
                        if llm_settings is not None else _build_pipeline(workspace, session, local_cancel))
            if hasattr(getattr(pipeline, "cli", None), "usage"):
                clients.append(pipeline.cli)
            problem = workspace.load_problem()
            author = (workspace.solution_path.read_text(encoding="utf-8")
                      if problem.solution_source == "author" and workspace.solution_path.exists() else None)
            result = pipeline.run_all(author_source=author, reuse_solution=True)
            entry = history.record(workspace, stage="export")
            if not entry["exported"]:
                raise PipelineError("검증된 내보내기 파일이 없습니다. 리포트를 확인해 주세요.")
            return result
        except Cancelled:
            history.record(workspace, stage="batch", status="cancelled")
            raise
        except Exception as error:
            history.record(workspace, stage="batch", status="failed", error=str(error))
            raise
        finally:
            # 실패·중단으로 끝나도 그때까지 쓴 토큰을 남긴다.
            if clients:
                progress(usage=clients[0].usage.snapshot())

    job = session.start("batch", work)
    cancellation_sent = False
    while not job.done_event.wait(0.2):
        if cancel.is_set() and not cancellation_sent:
            session.cancel()
            cancellation_sent = True
    if job.status == "cancelled":
        raise Cancelled("사용자 요청으로 중단했습니다.")
    if job.status != "done":
        raise RuntimeError(job.error)
    return {"status": "exported", "slug": workspace.slug, "has_zip": True, "message": "최종 검증·문제 내보내기 완료"}


def _ensure_idle(slug: str) -> None:
    if _session(slug).job.to_dict()["running"]:
        raise HTTPException(status_code=409, detail="작업이 실행 중입니다. 끝난 뒤 수정해 주세요.")


def _remove_zip(workspace: Workspace) -> None:
    problem_export.remove_archives(workspace)


def _invalidate_results(workspace: Workspace, *, problem_changed: bool = False) -> None:
    _remove_zip(workspace)
    (workspace.reports_dir / "report.md").unlink(missing_ok=True)
    path = workspace.reports_dir / "run.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    keys = ("cases", "strength", "final", "export", "export_metadata")
    if problem_changed:
        keys += ("stress", "examples")
        workspace.clear_cases()
    for key in keys:
        payload.pop(key, None)
    payload["problem"] = workspace.load_problem().to_dict()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_results(workspace: Workspace) -> dict:
    path = workspace.reports_dir / "run.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {key: payload.get(key) for key in ("stress", "cases", "strength", "final", "export", "export_metadata")}


def _fetch_drive(workspace: Workspace) -> dict:
    result = drive.fetch_figures(workspace, _settings["drive_folder"])
    if result["downloaded"]:
        _invalidate_results(workspace, problem_changed=True)
    return result


def _figure_notes(notes, *, hide_local_warning: bool) -> list[str]:
    # 이 문구는 로컬 수집본 검사 결과다. Drive 요청의 성공/실패와 혼동시키지 않는다.
    return [note for note in notes if not (hide_local_warning and note.startswith("지문의 그림은 "))]


def _image_download_state(workspace: Workspace, job: dict) -> dict:
    expected = len(workspace.load_problem().figures)
    available = sum(bool(images.find(workspace, n)) for n in range(1, expected + 1))
    status = "none" if not expected else "ready" if available == expected else (
        "downloading" if job["name"] == "drive-images" and job["running"] else
        "failed" if job["name"] == "drive-images" and job["status"] == "failed" else "missing")
    return {"status": status, "available": available, "expected": expected,
            "error": job.get("error", "") if status == "failed" else ""}


def _launch_drive_images(slug: str) -> dict:
    workspace, session = _workspace(slug), _session(slug)

    def work(cancel):
        session.bus.emit("drive-images", "Drive에서 이 문제의 그림을 가져오는 중입니다.")
        result = _fetch_drive(workspace)
        session.bus.emit("drive-images", f"그림 다운로드 완료: {result['downloaded']}개, 기존 그림 {result['cached']}개")
        return result

    try:
        session.start("drive-images", work)
    except JobConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"status": "started", "stage": "drive-images"}


async def _store_pdf(file: UploadFile, destination: Path) -> None:
    """PDF를 조각으로 읽어 저장한다. 통째로 메모리에 올리지 않는다.

    받는 동안에는 임시 파일에만 쓰고, 검사를 모두 통과했을 때만 제자리로
    옮긴다. 대상 파일을 먼저 열면 거절된 업로드 하나가 이미 올려 둔 멀쩡한
    지문을 지워 버린다.
    """
    staging = destination.with_name(destination.name + ".part")
    total = 0
    try:
        with staging.open("wb") as sink:
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
        staging.replace(destination)
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"PDF를 저장하지 못했습니다: {error}") from error
    finally:
        staging.unlink(missing_ok=True)


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
    settings = _llm_settings()

    def job(cancel_event: threading.Event) -> Any:
        history.record(workspace, stage=name, status="running")
        try:
            pipeline = _build_pipeline(workspace, session, cancel_event, settings=settings)
            result = work(pipeline)
            history.record(workspace, stage=name, status="done" if name not in {"export", "run-all"} else None)
            return result
        except Cancelled:
            history.record(workspace, stage=name, status="cancelled")
            raise
        except Exception as error:
            history.record(workspace, stage=name, status="failed", error=str(error))
            raise

    try:
        session.start(name, job)
    except JobConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"status": "started", "stage": name}


def _llm_settings() -> dict:
    with _settings_lock:
        return {"provider": _settings.get("provider", "claude"), "model": _settings.get("model"),
                "effort": _settings.get("effort")}


def _build_pipeline(
    workspace: Workspace, session: WorkspaceSession, cancel_event: threading.Event,
    *, settings: dict | None = None,
) -> Pipeline:
    settings = settings if settings is not None else _llm_settings()
    cli = make_cli(settings["provider"], model=settings.get("model"), effort=settings.get("effort"),
                   log_dir=workspace.logs_dir)
    _provider_labels = {
        "codex": "GPT (ChatGPT 구독)",
        "claude": "Claude 구독",
    }
    session.bus.emit("llm", f"사용 AI: {_provider_labels.get(settings['provider'], settings['provider'])} · 모델 {settings.get('model') or '기본값'} · effort {settings.get('effort') or '기본값'}")
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
        "boj_id": problem.boj_id,
        "history": history.get(workspace),
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
    "hints",
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
        or before.hints != after.hints
        or before.examples != after.examples
        or before.figures != after.figures
        or before.limits != after.limits
    )


def _zip_path(workspace: Workspace) -> Path:
    return problem_export.current_archive(workspace)


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
