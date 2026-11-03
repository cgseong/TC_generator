"""최대 30개 문제를 순서대로 실행하며 결과를 파일에 남기는 일괄 작업 큐."""
from __future__ import annotations

import copy
import json
import threading
import uuid
from pathlib import Path
from typing import Callable

from tcgen.history import now
from tcgen.jobs import Cancelled, JobConflict

MAX_BATCH_SIZE = 30


class BatchManager:
    def __init__(self, root: Path):
        self.path = root / ".tcgen-batch.json"
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        try:
            self._state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._state = {"status": "idle", "items": []}
        if not isinstance(self._state, dict) or not isinstance(self._state.get("items"), list):
            self._state = {"status": "idle", "items": []}
        if self._state.get("status") in {"running", "cancelling"}:
            self._state["status"] = "interrupted"
            self._state["finished_at"] = now()
            for item in self._state["items"]:
                if item["status"] in {"pending", "running"}:
                    item.update(status="interrupted", error="서버 종료로 중단되었습니다. 다시 선택해 실행해 주세요.")
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".part")
        staging.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.replace(self.path)

    def snapshot(self) -> dict:
        with self._lock:
            state = copy.deepcopy(self._state)
        items = state.get("items", [])
        state["running"] = state["status"] in {"running", "cancelling"}
        state["counts"] = {status: sum(item["status"] == status for item in items)
                           for status in ("pending", "running", "exported", "failed", "skipped", "cancelled", "interrupted")}
        return state

    def update(self, problem_id: int, **changes):
        with self._lock:
            for item in self._state["items"]:
                if item["problem_id"] == problem_id:
                    item.update(changes)
                    self._save()
                    return

    def start(self, problems: list[dict], worker: Callable, *, skip_exported: bool = True, llm: dict | None = None) -> dict:
        if not 1 <= len(problems) <= MAX_BATCH_SIZE or len({p["problem_id"] for p in problems}) != len(problems):
            raise ValueError("서로 다른 문제를 1~30개 선택해 주세요.")
        with self._lock:
            if self._state.get("status") in {"running", "cancelling"}:
                raise JobConflict("일괄 실행이 이미 진행 중입니다.")
            self._cancel = threading.Event()
            self._state = {"id": uuid.uuid4().hex, "status": "running", "created_at": now(),
                           "skip_exported": skip_exported, "llm": dict(llm or {}),
                           "items": [{**p, "status": "pending", "slug": "", "error": ""} for p in problems]}
            self._save()
            initial = self.snapshot()

        def run():
            try:
                for problem in problems:
                    number = problem["problem_id"]
                    if self._cancel.is_set():
                        break
                    self.update(number, status="running", started_at=now())
                    try:
                        result = worker(number, self._cancel, lambda **data: self.update(number, **data), skip_exported)
                        self.update(number, **result, finished_at=now())
                    except Cancelled as error:
                        self.update(number, status="cancelled", error=str(error), finished_at=now())
                        self._cancel.set()
                    except Exception as error:
                        self.update(number, status="failed", error=str(error) or type(error).__name__, finished_at=now())
            finally:
                with self._lock:
                    for item in self._state["items"]:
                        if item["status"] == "pending":
                            item.update(status="cancelled", error="실행 전에 중단되었습니다.")
                    self._state["status"] = "cancelled" if self._cancel.is_set() else "done"
                    self._state["finished_at"] = now()
                    self._save()

        threading.Thread(target=run, name="tcgen-batch", daemon=True).start()
        return initial

    def cancel(self, batch_id: str) -> dict:
        with self._lock:
            if self._state.get("id") != batch_id:
                raise ValueError("일괄 실행 기록을 찾지 못했습니다.")
            if self._state.get("status") in {"running", "cancelling"}:
                self._cancel.set()
                self._state["status"] = "cancelling"
                self._save()
            return self.snapshot()
