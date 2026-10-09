"""이벤트 버스 테스트. SSE 로그가 이 동작에 의존한다."""

import threading

from tcgen.engine.events import LEVEL_WARN, Event, EventBus


class TestPublishing:
    def test_emit_records_history(self):
        bus = EventBus()
        bus.emit("solution", "시작", data_key="값")

        history = bus.history()
        assert len(history) == 1
        assert history[0].stage == "solution"
        assert history[0].data == {"data_key": "값"}

    def test_event_serializes(self):
        event = Event(stage="cases", message="심사", level=LEVEL_WARN)
        payload = event.to_dict()
        assert payload["stage"] == "cases"
        assert payload["level"] == "warn"
        assert "timestamp" in payload


class TestSubscription:
    def test_subscriber_receives_backlog_then_live_events(self):
        bus = EventBus()
        bus.emit("solution", "이전 기록")

        received: list[str] = []
        ready = threading.Event()

        def consume() -> None:
            for event in bus.subscribe():
                received.append(event.message)
                ready.set()

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        ready.wait(timeout=2)

        bus.emit("solution", "새 이벤트")
        bus.close()
        thread.join(timeout=2)

        assert "이전 기록" in received
        assert "새 이벤트" in received

    def test_close_ends_iteration(self):
        bus = EventBus()
        finished = threading.Event()

        def consume() -> None:
            for _ in bus.subscribe():
                pass
            finished.set()

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        bus.close()
        thread.join(timeout=2)

        assert finished.is_set()

    def test_subscriber_is_removed_after_iteration(self):
        bus = EventBus()
        bus.emit("cases", "기록")
        iterator = bus.subscribe()
        assert next(iterator).message == "기록"  # 백로그는 블로킹 없이 나온다
        iterator.close()  # 구독 해제

        bus.emit("cases", "구독자 없이도 발행된다")
        assert len(bus.history()) == 2
