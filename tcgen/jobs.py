"""백그라운드 작업 관리.

5~15분 걸리는 단계를 HTTP 요청 안에서 돌리지 않는다. 작업은 스레드로 띄우고
UI는 SSE로 진행 상황을 본다 (PRD 7.1, 8.2).

문제 하나당 동시에 한 작업만 허용한다. 같은 작업폴더를 두 작업이 동시에
건드리면 케이스 파일이 섞인다.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from tcgen.engine.events import LEVEL_ERROR, LEVEL_SUCCESS, LEVEL_WARN, EventBus

logger = logging.getLogger(__name__)

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_IDLE = "idle"


class JobConflict(Exception):
    """이미 실행 중인 작업이 있다."""


class Cancelled(Exception):
    """사용자가 중단을 요청했다."""


@dataclass
class JobState:
    """작업 하나의 상태. 서버 안에서만 쓰는 가변 상태다."""

    name: str = ""
    status: str = STATUS_IDLE
    error: str = ""
    result: Any = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "error": self.error,
            "running": self.status == STATUS_RUNNING,
        }


class WorkspaceSession:
    """한 작업폴더의 이벤트 버스와 작업 상태."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.bus = EventBus()
        self.job = JobState()
        self._lock = threading.Lock()

    def start(self, name: str, work: Callable[[threading.Event], Any]) -> None:
        with self._lock:
            if self.job.status == STATUS_RUNNING:
                raise JobConflict(f"'{self.job.name}' 작업이 아직 실행 중입니다.")
            job = JobState(name=name, status=STATUS_RUNNING)
            self.job = job

        def finish(status: str, *, result: Any = None, error: str = "") -> None:
            # 자기 자신의 JobState에만 쓴다. 그 사이 새 잡이 시작됐더라도
            # 끝난 잡의 결과가 새 잡의 상태를 덮어쓰지 않는다.
            with self._lock:
                job.status = status
                job.result = result
                job.error = error

        def runner() -> None:
            try:
                result = work(job.cancel_event)
            except Cancelled:
                finish(STATUS_CANCELLED)
                self.bus.emit(name, "사용자 요청으로 중단했습니다.", LEVEL_WARN)
            except BaseException as error:  # noqa: BLE001 - 어떤 예외든 잠금을 풀어야 한다
                logger.exception("작업 실패: %s", name)
                finish(STATUS_FAILED, error=str(error) or type(error).__name__)
                self.bus.emit(name, f"실패: {error}", LEVEL_ERROR)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
            else:
                finish(STATUS_DONE, result=result)
                self.bus.emit(name, f"'{name}' 단계를 마쳤습니다.", LEVEL_SUCCESS)

        threading.Thread(target=runner, name=f"job-{self.slug}-{name}", daemon=True).start()

    def cancel(self) -> bool:
        with self._lock:
            if self.job.status != STATUS_RUNNING:
                return False
            self.job.cancel_event.set()
        self.bus.emit(self.job.name, "중단을 요청했습니다. 현재 단계가 끝나면 멈춥니다.", LEVEL_WARN)
        return True


class SessionRegistry:
    """작업폴더별 세션 보관소."""

    def __init__(self) -> None:
        self._sessions: dict[str, WorkspaceSession] = {}
        self._lock = threading.Lock()

    def get(self, slug: str) -> WorkspaceSession:
        with self._lock:
            session = self._sessions.get(slug)
            if session is None:
                session = WorkspaceSession(slug)
                self._sessions[slug] = session
            return session
