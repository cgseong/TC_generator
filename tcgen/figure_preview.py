"""작업공간을 만들기 전에 선택한 문제의 그림을 비동기로 받아 미리 보여 준다."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from tcgen import boj, drive, images
from tcgen.config import PROJECT_ROOT
from tcgen.jobs import JobConflict, SessionRegistry
from tcgen.models import Problem
from tcgen.workspace import Workspace

CACHE_DIR = PROJECT_ROOT / ".tcgen-cache" / "figure-previews"
registry = SessionRegistry()
_lock = threading.Lock()


def _key(problem: Problem, folder: str) -> str:
    data = problem.to_dict()
    data["slug"] = f"boj-{problem.boj_id}"
    return hashlib.sha256((folder + json.dumps(data, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()[:40]


def workspace(problem_id: int, key: str) -> Workspace:
    if not re.fullmatch(r"[a-f0-9]{40}", key):
        raise ValueError("그림 캐시 키가 올바르지 않습니다.")
    root = CACHE_DIR.resolve()
    path = (root / key).resolve()
    if path.parent != root:
        raise ValueError("그림 캐시 경로가 올바르지 않습니다.")
    cached = Workspace(path)
    if not cached.problem_path.is_file():
        raise ValueError("그림 캐시가 없습니다.")
    if cached.load_problem().boj_id != problem_id:
        raise ValueError("문제 번호와 그림 캐시가 다릅니다.")
    return cached


def state(problem_id: int, key: str) -> dict:
    cached = workspace(problem_id, key)
    count = len(cached.load_problem().figures)
    found = {str(n): f"/api/boj/{problem_id}/figure-preview/{key}/{n}"
             for n in range(1, count + 1) if images.find(cached, n)}
    job = registry.get(key).job.to_dict()
    status = "ready" if len(found) == count else (
        "downloading" if job["running"] else "failed" if job["status"] == "failed" else "missing")
    return {"key": key, "status": status, "error": job.get("error", ""), "images": found}


def prepare(imported: boj.BojImport, folder: str) -> dict:
    problem = imported.problem
    if not problem.figures:
        return {"key": "", "status": "none", "error": "", "images": {}}
    folder = drive.folder_id(folder) if folder else ""
    key = _key(problem, folder)
    with _lock:
        cached = Workspace(CACHE_DIR / key)
        if cached.root.resolve().parent != CACHE_DIR.resolve() or not cached.contains(cached.figures_dir):
            raise ValueError("그림 캐시 경로가 올바르지 않습니다.")
        if not cached.problem_path.exists():
            cached.save_problem(problem)
        if imported.images and not all(images.find(cached, n) for n in range(1, len(problem.figures) + 1)):
            boj._copy_figures(cached, imported.images)
        result = state(problem.boj_id, key)
        if folder and result["status"] not in {"ready", "downloading"}:
            try:
                registry.get(key).start("drive-images", lambda cancel: drive.fetch_figures(cached, folder))
            except JobConflict:
                pass
        return state(problem.boj_id, key)


def copy_cached(problem: Problem, folder: str, target: Workspace) -> int:
    """미리보기를 마친 그림을 새 작업공간에 재사용한다."""
    if not problem.boj_id or not problem.figures:
        return 0
    folder = drive.folder_id(folder) if folder else ""
    key = _key(problem, folder)
    try:
        cached = workspace(problem.boj_id, key)
    except (OSError, ValueError):
        return 0
    paths = [images.find(cached, n) for n in range(1, len(problem.figures) + 1)]
    if any(path is None for path in paths):
        return 0
    missing = sum(not images.find(target, n) for n in range(1, len(paths) + 1))
    if missing:
        failures = boj._copy_figures(target, paths)
        if failures:
            raise OSError("; ".join(failures))
    return missing
