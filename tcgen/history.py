"""불러온 문제, 검증된 내보내기, 사용자가 조정한 완료 표시를 보관한다."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from tcgen.problem_export import current_archive
from tcgen.workspace import Workspace, WorkspaceError, ensure_safe_slug, list_workspaces

_lock = threading.RLock()
_FILENAME = ".tcgen-history.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp(path: Path):
    try:
        info = path.stat()
        return [info.st_size, info.st_mtime_ns, info.st_ino]
    except OSError:
        return None


def _document(root: Path) -> dict:
    try:
        value = json.loads((root / _FILENAME).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _read(root: Path) -> dict:
    entries = _document(root).get("entries", {})
    return {key: entry for key, entry in entries.items() if isinstance(entry, dict)} if isinstance(entries, dict) else {}


def _overrides(root: Path) -> dict:
    values = _document(root).get("completion_overrides", {})
    return {key: entry for key, entry in values.items() if isinstance(entry, dict) and type(entry.get("completed")) is bool} if isinstance(values, dict) else {}


def _write(root: Path, entries: dict, overrides: dict | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / _FILENAME
    staging = path.with_suffix(".part")
    payload = {"version": 2, "entries": entries,
               "completion_overrides": _overrides(root) if overrides is None else overrides}
    staging.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(path)


def _capture(workspace: Workspace, previous: dict) -> dict:
    problem = workspace.load_problem()
    report = workspace.reports_dir / "run.json"
    archive = current_archive(workspace)
    stamp = [_stamp(workspace.problem_path), _stamp(report), archive.name, _stamp(archive)]
    entry = {**previous, "slug": workspace.slug, "boj_id": problem.boj_id, "title": problem.title,
             "created_at": previous.get("created_at") or now(),
             "status": previous.get("status", "imported"), "stage": previous.get("stage", "import"),
             "error": previous.get("error", ""), "updated_at": previous.get("updated_at") or now()}
    if stamp != previous.get("_scan"):
        entry["updated_at"] = now()
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        export = payload.get("export") or {}
        export = export if isinstance(export, dict) else {}
        final = payload.get("final")
        final = final if isinstance(final, dict) else None
        if (problem.solution_confirmed and stamp[3] and export.get("has_errors") is False
                and export.get("case_count", 0) > 0 and (final is None or final.get("passed"))):
            entry["_export_stamp"] = stamp[3]
            entry["_export_name"] = archive.name
            entry["_problem_stamp"] = stamp[0]
            entry["exported_at"] = previous.get("exported_at") or datetime.fromtimestamp(archive.stat().st_mtime, timezone.utc).isoformat()
            entry["case_count"] = export["case_count"]
            entry["status"] = "exported"
        else:
            entry["_export_stamp"] = None
        entry["_scan"] = stamp
    return entry


def _completion(root: Path, result: dict, overrides: dict | None = None) -> dict:
    values = _overrides(root) if overrides is None else overrides
    override = values.get(str(result.get("boj_id")))
    result["auto_exported"] = result["exported"]
    result["completed"] = override["completed"] if override else result["exported"]
    result["completion_source"] = "manual" if override else "automatic"
    result["completed_at"] = override.get("updated_at") if override else result.get("exported_at")
    return result


def _public(root: Path, entry: dict) -> dict:
    result = {key: value for key, value in entry.items() if not key.startswith("_")}
    try:
        ws = Workspace(root / ensure_safe_slug(entry["slug"]))
        archive = ws.export_dir / entry.get("_export_name", f"{ws.slug}_testcases.zip")
        current = (bool(entry.get("_export_stamp")) and ws.contains(archive)
                   and _stamp(archive) == entry["_export_stamp"]
                   and _stamp(ws.problem_path) == entry.get("_problem_stamp"))
    except (WorkspaceError, KeyError):
        current = False
    result["exported"] = current
    if result.get("status") == "exported" and not current:
        result["status"] = "needs_review"
    return _completion(root, result)


def record(workspace: Workspace, *, stage: str | None = None, status: str | None = None,
           error: str = "", exported: bool = False) -> dict:
    root = workspace.root.parent
    with _lock:
        entries = _read(root)
        entry = _capture(workspace, entries.get(workspace.slug, {}))
        if stage is not None:
            entry["stage"] = stage
        if status is not None:
            entry["status"] = status
        entry["error"] = error
        entry["updated_at"] = now()
        overrides = _overrides(root)
        if exported:
            entry["exported_at"] = now()
            # 새 내보내기가 성공하면 자동으로 다시 완료 처리한다.
            overrides.pop(str(entry.get("boj_id")), None)
        entries[workspace.slug] = entry
        _write(root, entries, overrides)
        return _public(root, entry)


def set_completion(root: Path, problem_id: int, completed: bool, *, title: str = "") -> dict:
    with _lock:
        overrides = _overrides(root)
        overrides[str(problem_id)] = {"completed": completed, "updated_at": now(), "title": title}
        _write(root, _read(root), overrides)
        return by_problem(root, problem_ids=[problem_id])[str(problem_id)]


def snapshot(root: Path, *, synchronize: bool = False) -> list[dict]:
    with _lock:
        entries = _read(root)
        if synchronize:
            for ws in list_workspaces(base_dir=root):
                try:
                    entries[ws.slug] = _capture(ws, entries.get(ws.slug, {}))
                except (OSError, ValueError, WorkspaceError):
                    continue
            _write(root, entries)
        return sorted((_public(root, entry) for entry in entries.values()),
                      key=lambda entry: entry.get("updated_at", ""), reverse=True)


def get(workspace: Workspace) -> dict:
    with _lock:
        entry = _read(workspace.root.parent).get(workspace.slug)
        return _public(workspace.root.parent, entry) if entry else {}


def by_problem(root: Path, *, problem_ids=None) -> dict:
    # 첫 화면은 표시 중인 번호의 저장된 기록만 확인한다. 작업폴더를 재탐색하지 않는다.
    wanted = set(problem_ids) if problem_ids is not None else None
    with _lock:
        entries = [entry for entry in _read(root).values()
                   if entry.get("boj_id") is not None and (wanted is None or entry.get("boj_id") in wanted)]
        entries.sort(key=lambda entry: entry.get("updated_at", ""), reverse=True)
        result = {}
        for raw in entries:
            entry = _public(root, raw)
            key = str(entry["boj_id"])
            if key not in result or (entry["exported"] and not result[key]["exported"]):
                result[key] = entry
        for key, override in _overrides(root).items():
            if key not in result and key.isdigit() and (wanted is None or int(key) in wanted):
                result[key] = _completion(root, {"boj_id": int(key), "slug": "", "title": override.get("title", ""),
                                                "exported": False, "updated_at": override["updated_at"]})
        return result
