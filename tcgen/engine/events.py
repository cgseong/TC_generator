"""파이프라인 진행 이벤트 버스.

브라우저 UI가 SSE로 받아 실시간 로그를 그린다 (PRD 8.2). 5~15분 걸리는 작업이
침묵하지 않게 하는 것이 목적이다.
"""

from __future__ import annotations

import collections
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"
LEVEL_SUCCESS = "success"

_QUEUE_MAX = 1000
#: 하루 종일 띄워 두는 도구이므로 기록은 상한을 둔다.
_HISTORY_MAX = 2000


@dataclass(frozen=True)
class Event:
    """진행 상황 한 건."""

    stage: str
    message: str
    level: str = LEVEL_INFO
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "message": self.message,
            "level": self.level,
            "data": self.data,
            "timestamp": self.timestamp,
        }


class EventBus:
    """구독자마다 큐를 하나씩 주는 단순한 발행/구독."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[Event | None]] = []
        self._history: collections.deque[Event] = collections.deque(maxlen=_HISTORY_MAX)

    def publish(self, event: Event) -> None:
        with self._lock:
            self._history.append(event)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:  # 느린 구독자 때문에 파이프라인을 멈추지 않는다
                continue

    def emit(
        self,
        stage: str,
        message: str,
        level: str = LEVEL_INFO,
        **data: Any,
    ) -> Event:
        event = Event(stage=stage, message=message, level=level, data=data)
        self.publish(event)
        return event

    def close(self) -> None:
        """구독자들에게 종료를 알린다."""
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(None)
            except queue.Full:
                continue

    def history(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._history)

    def attach(self) -> tuple[tuple[Event, ...], "queue.Queue[Event | None]"]:
        """구독 큐를 등록하고 지금까지의 기록과 함께 돌려준다.

        블로킹 제너레이터(:meth:`subscribe`)는 서버에서 쓰면 스레드를 영구히
        붙잡으므로, 비동기 스트림은 이 저수준 API로 직접 폴링한다.
        """
        subscriber: queue.Queue[Event | None] = queue.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            backlog = tuple(self._history)
            self._subscribers.append(subscriber)
        return backlog, subscriber

    def detach(self, subscriber: "queue.Queue[Event | None]") -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def subscribe(self) -> Iterator[Event]:
        """현재까지의 기록을 먼저 주고, 이후 이벤트를 이어서 준다."""
        backlog, subscriber = self.attach()
        try:
            yield from backlog
            while True:
                event = subscriber.get()
                if event is None:
                    return
                yield event
        finally:
            self.detach(subscriber)
