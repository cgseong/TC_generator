"""불러온 문제, 검증된 내보내기, 문제 사이트 업로드 기록을 보관한다.

zip 준비(앱이 판정)와 업로드(사람이 확인)는 서로 다른 상태다. 업로드는 앱이
내려준 zip 묶음(내려받기 기록) 단위로 확인받고, 그 zip의 해시를 남겨 올린 뒤
zip을 다시 만들면 "재업로드 필요"로 알린다.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tcgen.problem_export import current_archive
from tcgen.workspace import Workspace, WorkspaceError, ensure_safe_slug, list_workspaces

_lock = threading.RLock()
_FILENAME = ".tcgen-history.json"
_MANIFEST_LIMIT = 100
UPLOAD_NONE, UPLOAD_DONE, UPLOAD_STALE = "none", "uploaded", "stale"


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


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _uploads(root: Path) -> dict:
    document = _document(root)
    values = document.get("uploads", {})
    uploads = ({key: entry for key, entry in values.items() if isinstance(entry, dict) and entry.get("uploaded_at")}
               if isinstance(values, dict) else {})
    # 예전 버전의 수동 "완료" 표시는 업로드했다는 뜻으로 쓰였다. 업로드 기록으로 옮긴다.
    legacy = document.get("completion_overrides", {})
    if isinstance(legacy, dict):
        for key, entry in legacy.items():
            if isinstance(entry, dict) and entry.get("completed") is True and key not in uploads:
                uploads[key] = {"uploaded_at": entry.get("updated_at") or now(), "sha256": None,
                                "manifest": None, "source": "legacy", "title": entry.get("title", "")}
    return uploads


def _manifests(root: Path) -> dict:
    values = _document(root).get("manifests", {})
    return {key: entry for key, entry in values.items() if isinstance(entry, dict)} if isinstance(values, dict) else {}


def _write(root: Path, entries: dict, uploads: dict | None = None, manifests: dict | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / _FILENAME
    staging = path.with_suffix(".part")
    payload = {"version": 3, "entries": entries,
               "uploads": _uploads(root) if uploads is None else uploads,
               "manifests": _manifests(root) if manifests is None else manifests}
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
            entry["_export_sha"] = _sha256(archive)
        else:
            entry["_export_stamp"] = None
            entry["_export_sha"] = None
        entry["_scan"] = stamp
    elif entry.get("_export_stamp") and not entry.get("_export_sha"):
        # 해시를 남기기 전 버전에서 기록된 내보내기.
        entry["_export_sha"] = _sha256(archive)
    return entry


def _status(root: Path, result: dict, uploads: dict | None = None, export_sha: str | None = None) -> dict:
    values = _uploads(root) if uploads is None else uploads
    upload = values.get(str(result.get("boj_id")))
    if upload is None:
        state = UPLOAD_NONE
    elif result["exported"] and upload.get("sha256") and export_sha and upload["sha256"] != export_sha:
        # 올린 뒤 zip을 다시 만들었다. 문제 사이트에는 예전 zip이 올라가 있다.
        state = UPLOAD_STALE
    else:
        state = UPLOAD_DONE
    result["upload"] = {"status": state, "uploaded_at": (upload or {}).get("uploaded_at"),
                        "manifest": (upload or {}).get("manifest"), "source": (upload or {}).get("source")}
    # 일괄 실행에서 건너뛸 문제: zip이 준비됐거나 이미 올렸다.
    result["completed"] = result["exported"] or state != UPLOAD_NONE
    result["completed_at"] = (upload or {}).get("uploaded_at") or result.get("exported_at")
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
    return _status(root, result, export_sha=entry.get("_export_sha") if current else None)


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
        if exported:
            entry["exported_at"] = now()
        entries[workspace.slug] = entry
        _write(root, entries)
        return _public(root, entry)


def _current_export_sha(root: Path, problem_id: int) -> str | None:
    for entry in _read(root).values():
        if entry.get("boj_id") == problem_id and _public(root, entry)["exported"]:
            return entry.get("_export_sha")
    return None


def set_uploaded(root: Path, problem_id: int, uploaded: bool, *, title: str = "") -> dict:
    """한 문제의 업로드 기록을 직접 남기거나 지운다. 지금 zip을 올린 것으로 본다."""
    with _lock:
        uploads = _uploads(root)
        if uploaded:
            uploads[str(problem_id)] = {"uploaded_at": now(), "sha256": _current_export_sha(root, problem_id),
                                        "manifest": None, "source": "manual", "title": title}
        else:
            uploads.pop(str(problem_id), None)
        _write(root, _read(root), uploads)
        current = by_problem(root, problem_ids=[problem_id]).get(str(problem_id))
        # 작업 폴더 없이 기록만 있던 문제는 지우면 기록이 남지 않는다.
        return current or _status(root, {"boj_id": problem_id, "slug": "", "title": title,
                                         "exported": False, "updated_at": now()})


def create_manifest(root: Path, items: list[dict], *, kind: str) -> dict:
    """내려준 zip 묶음을 기록한다. items: problem_id, slug, title, path."""
    with _lock:
        manifests = _manifests(root)
        manifest = {"id": uuid.uuid4().hex, "kind": kind, "created_at": now(), "confirmed": [], "confirmed_at": None,
                    "items": [{"problem_id": item["problem_id"], "slug": item["slug"], "title": item.get("title", ""),
                               "sha256": _sha256(item["path"])} for item in items]}
        manifests[manifest["id"]] = manifest
        for key in sorted(manifests, key=lambda key: manifests[key].get("created_at", ""))[:-_MANIFEST_LIMIT]:
            manifests.pop(key)
        _write(root, _read(root), manifests=manifests)
        return manifest


def confirm_manifest(root: Path, manifest_id: str, problem_ids: list[int]) -> dict:
    """내려받은 묶음 중 실제로 올린 문제를 기록한다. 기록하는 해시는 내려받을 때의 zip이다."""
    with _lock:
        manifests, uploads = _manifests(root), _uploads(root)
        manifest = manifests.get(manifest_id)
        if manifest is None:
            raise KeyError(manifest_id)
        items = {item["problem_id"]: item for item in manifest["items"]}
        unknown = set(problem_ids) - set(items)
        if unknown:
            raise ValueError(f"이 내려받기에 없는 문제입니다: {', '.join(map(str, sorted(unknown)))}")
        stamp = now()
        for number in problem_ids:
            item = items[number]
            uploads[str(number)] = {"uploaded_at": stamp, "sha256": item["sha256"], "manifest": manifest_id,
                                    "source": "download", "title": item["title"]}
        manifest["confirmed"] = sorted(set(manifest["confirmed"]) | set(problem_ids))
        manifest["confirmed_at"] = stamp
        _write(root, _read(root), uploads, manifests)
        return manifest


def undo_manifest(root: Path, manifest_id: str) -> dict:
    """이 묶음으로 남긴 업로드 기록을 지운다. 그 뒤 다른 묶음으로 다시 기록한 문제는 건드리지 않는다."""
    with _lock:
        manifests, uploads = _manifests(root), _uploads(root)
        manifest = manifests.get(manifest_id)
        if manifest is None:
            raise KeyError(manifest_id)
        for key in [key for key, entry in uploads.items() if entry.get("manifest") == manifest_id]:
            uploads.pop(key)
        manifest["confirmed"], manifest["confirmed_at"] = [], None
        _write(root, _read(root), uploads, manifests)
        return manifest


def recent_manifests(root: Path, *, limit: int = 10) -> list[dict]:
    with _lock:
        manifests = sorted(_manifests(root).values(), key=lambda entry: entry.get("created_at", ""), reverse=True)
        return manifests[:limit]


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
        for key, upload in _uploads(root).items():
            if key not in result and key.isdigit() and (wanted is None or int(key) in wanted):
                result[key] = _status(root, {"boj_id": int(key), "slug": "", "title": upload.get("title", ""),
                                             "exported": False, "updated_at": upload["uploaded_at"]})
        return result
