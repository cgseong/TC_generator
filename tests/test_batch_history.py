"""Durable export records and bounded sequential batches, without invoking an LLM."""
import json
import threading
import time

import pytest

from fixtures import make_problem
from test_initial_catalog import collection
from test_server import client
from tcgen import history, server
from tcgen.batch import BatchManager
from tcgen.jobs import Cancelled, JobConflict, WorkspaceSession
from tcgen.workspace import create_workspace


def exported_workspace(root, number=1000):
    ws = create_workspace(make_problem(boj_id=number, solution_confirmed=True), base_dir=root)
    mark_exported(ws)
    return ws


def mark_exported(ws):
    ws.save_problem(ws.load_problem().with_changes(solution_confirmed=True))
    (ws.export_dir / f"{ws.slug}_testcases.zip").write_bytes(b"test artifact")
    (ws.reports_dir / "run.json").write_text(json.dumps({
        "final": {"passed": True}, "export": {"has_errors": False, "case_count": 15},
    }))


def wait_batch(manager):
    deadline = time.monotonic() + 5
    while manager.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert not manager.snapshot()["running"]
    return manager.snapshot()


def test_history_backfills_old_exports_and_survives_new_manager(tmp_path):
    ws = exported_workspace(tmp_path)
    assert history.snapshot(tmp_path) == []
    entry = history.snapshot(tmp_path, synchronize=True)[0]
    assert entry["exported"] and entry["case_count"] == 15 and entry["exported_at"]
    assert history.by_problem(tmp_path)["1000"]["slug"] == ws.slug
    assert history.get(ws)["exported"]


def test_history_does_not_mark_failed_or_deleted_exports_complete(tmp_path):
    ws = exported_workspace(tmp_path)
    path = ws.reports_dir / "run.json"
    path.write_text(json.dumps({"export": {"has_errors": True, "case_count": 15}}))
    assert not history.record(ws)["exported"]
    mark_exported(ws)
    assert history.record(ws)["exported"]
    (ws.export_dir / f"{ws.slug}_testcases.zip").unlink()
    assert not history.get(ws)["exported"]
    assert history.get(ws)["exported_at"]


def test_history_critical_edit_removes_current_check_but_keeps_past_date(tmp_path):
    ws = exported_workspace(tmp_path)
    entry = history.record(ws)
    ws.save_problem(ws.load_problem().with_changes(solution_confirmed=False, statement="changed"))
    updated = history.record(ws)
    assert not updated["exported"]
    assert updated["exported_at"] == entry["exported_at"]


def test_previous_success_is_preserved_when_another_attempt_fails(tmp_path):
    successful = exported_workspace(tmp_path)
    history.record(successful)
    failed = create_workspace(make_problem(boj_id=1000), base_dir=tmp_path)
    history.record(failed, status="failed", error="failed later")
    assert history.by_problem(tmp_path)["1000"]["slug"] == successful.slug


def test_catalog_history_inspects_only_visible_problem_ids(tmp_path, monkeypatch):
    first = exported_workspace(tmp_path)
    second = exported_workspace(tmp_path, 1001)
    history.record(first); history.record(second)
    public = history._public
    inspected = []
    def spy(root, entry):
        inspected.append(entry["boj_id"])
        return public(root, entry)
    monkeypatch.setattr(history, "_public", spy)
    assert list(history.by_problem(tmp_path, problem_ids=[1000])) == ["1000"]
    assert inspected == [1000]


def test_queue_runs_serially_continues_on_error_and_persists(tmp_path):
    manager = BatchManager(tmp_path)
    order = []
    def worker(number, cancel, progress, skip, **kwargs):
        order.append(number)
        progress(slug=f"boj-{number}")
        if number == 1001:
            raise ValueError("intentional failure")
        return {"status": "exported"}
    manager.start([{"problem_id": n, "title": str(n)} for n in (1000, 1001, 1002)], worker)
    result = wait_batch(manager)
    assert order == [1000, 1001, 1002]
    assert [i["status"] for i in result["items"]] == ["exported", "failed", "exported"]
    assert result["items"][1]["error"] == "intentional failure"
    assert BatchManager(tmp_path).snapshot()["items"] == result["items"]


def test_queue_cancel_stops_current_and_leaves_pending_unexecuted(tmp_path):
    manager = BatchManager(tmp_path)
    started = threading.Event()
    def worker(number, cancel, progress, skip, **kwargs):
        started.set()
        assert cancel.wait(3)
        raise Cancelled("cancelled")
    batch = manager.start([{"problem_id": n, "title": str(n)} for n in (1000, 1001)], worker)
    assert started.wait(3)
    with pytest.raises(JobConflict):
        manager.start([{"problem_id": 1002}], worker)
    manager.cancel(batch["id"])
    result = wait_batch(manager)
    assert result["status"] == "cancelled"
    assert [i["status"] for i in result["items"]] == ["cancelled", "cancelled"]


@pytest.mark.parametrize("numbers", [[], [1000] * 2, list(range(1000, 1031))])
def test_queue_selection_limits(tmp_path, numbers):
    with pytest.raises(ValueError):
        BatchManager(tmp_path).start([{"problem_id": n} for n in numbers], lambda *args: {})


def test_restart_preserves_finished_and_marks_unfinished_interrupted(tmp_path):
    (tmp_path / ".tcgen-batch.json").write_text(json.dumps({"id": "old", "status": "running", "items": [
        {"problem_id": 1000, "status": "exported"}, {"problem_id": 1001, "status": "running"},
        {"problem_id": 1002, "status": "pending"},
    ]}))
    result = BatchManager(tmp_path).snapshot()
    assert result["status"] == "interrupted" and not result["running"]
    assert [i["status"] for i in result["items"]] == ["exported", "interrupted", "interrupted"]


def test_job_returns_an_independent_completion_signal():
    session = WorkspaceSession("example")
    job = session.start("test", lambda _: {"ok": True})
    assert job.done_event.wait(3)
    assert job.status == "done" and job.result == {"ok": True}


def test_api_limits_and_order(client, collection, monkeypatch):
    order = []
    def worker(number, cancel, progress, skip, **kwargs):
        order.append((number, skip))
        return {"status": "exported"}
    monkeypatch.setattr(server, "_execute_batch_item", worker)
    assert client.post("/api/batches", json={"problem_ids": []}).status_code == 422
    assert client.post("/api/batches", json={"problem_ids": [1000, 1000]}).status_code == 422
    assert client.post("/api/batches", json={"problem_ids": list(range(1000, 1031))}).status_code == 422
    assert client.post("/api/batches", json={"problem_ids": [9999]}).status_code == 404
    response = client.post("/api/batches", json={"problem_ids": [1002, 1000, 1001], "skip_exported": False})
    assert response.status_code == 200
    result = wait_batch(server._batch_manager())
    assert [i["problem_id"] for i in result["items"]] == [1002, 1000, 1001]
    assert order == [(1002, False), (1000, False), (1001, False)]
    assert client.get("/api/batches/current").json()["counts"]["exported"] == 3


def test_api_history_marks_catalog_without_delaying_root_for_workspace_scan(client, collection, tmp_path, monkeypatch):
    ws = exported_workspace(tmp_path)
    assert client.get("/api/history").json()["by_problem"]["1000"]["exported"]
    monkeypatch.setattr(history, "list_workspaces", lambda **kwargs: pytest.fail("root must not scan workspaces"))
    html = client.get("/").text
    assert '<span class="status-pill tone-blue"><span class="dot"></span>업로드 대기</span>' in html
    assert 'data-select-id="1000"' in html
    # 첫 화면에도 완료 체크박스를 두지 않는다. 실행 선택 체크박스만 있다.
    assert "data-complete-id" not in html
    item = client.get("/api/boj").json()["items"][0]
    assert item["history"]["slug"] == ws.slug


def test_batch_worker_skips_completed_and_reuses_unfinished_workspace(client, collection, tmp_path, monkeypatch):
    complete = exported_workspace(tmp_path)
    history.record(complete)
    result = server._execute_batch_item(1000, threading.Event(), lambda **data: None, True)
    assert result["status"] == "skipped" and result["slug"] == complete.slug
    unfinished = create_workspace(make_problem(boj_id=1001, solution_source="author"), base_dir=tmp_path)
    unfinished.solution_path.write_text("print('author')")
    history.record(unfinished)
    seen = []
    class Pipeline:
        def run_all(self, **kwargs):
            seen.append(kwargs)
            mark_exported(unfinished)
            return {}
    monkeypatch.setattr(server, "_build_pipeline", lambda *args: Pipeline())
    result = server._execute_batch_item(1001, threading.Event(), lambda **data: None, True)
    assert result["status"] == "exported" and result["slug"] == unfinished.slug
    assert seen == [{"author_source": "print('author')", "reuse_solution": True}]


@pytest.mark.integration
def test_two_problems_run_full_pipeline_serially_and_export_valid_zips(client, collection, tmp_path, monkeypatch):
    import zipfile
    from fixtures import FakeCLI, default_responses
    from tcgen.engine.pipeline import Pipeline

    workspaces = [create_workspace(make_problem(boj_id=number), base_dir=tmp_path) for number in (1000, 1001)]
    for ws in workspaces:
        history.record(ws)
    calls = []
    def pipeline(ws, session, cancel, **kwargs):
        calls.append(ws.load_problem().boj_id)
        return Pipeline(ws, FakeCLI(default_responses()), bus=session.bus, cancel_event=cancel)
    monkeypatch.setattr(server, "_build_pipeline", pipeline)
    response = client.post("/api/batches", json={"problem_ids": [1000, 1001]})
    assert response.status_code == 200
    manager = server._batch_manager()
    deadline = time.monotonic() + 60
    while manager.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(.02)
    result = manager.snapshot()
    assert not result["running"]
    assert calls == [1000, 1001]
    assert [i["status"] for i in result["items"]] == ["exported", "exported"], result
    for ws in workspaces:
        assert history.get(ws)["exported"]
        with zipfile.ZipFile(ws.export_dir / f"{ws.slug}_problem.zip") as archive:
            assert "1/problem.json" in archive.namelist()
            assert sum(name.endswith(".in") for name in archive.namelist()) >= 15
    # A second click skips completed problems without invoking the pipeline again.
    assert client.post("/api/batches", json={"problem_ids": [1000, 1001]}).status_code == 200
    assert wait_batch(manager)["counts"]["skipped"] == 2
    assert calls == [1000, 1001]


def test_api_cancel_reaches_active_workspace_and_rejects_duplicate_batch(client, collection, tmp_path, monkeypatch):
    for number in (1000, 1001):
        history.record(create_workspace(make_problem(boj_id=number), base_dir=tmp_path))
    started = threading.Event()
    calls = []
    def build(ws, session, cancel, **kwargs):
        class Pipeline:
            def run_all(self, **kwargs):
                calls.append(ws.load_problem().boj_id)
                started.set()
                assert cancel.wait(3)
                raise Cancelled()
        return Pipeline()
    monkeypatch.setattr(server, "_build_pipeline", build)
    batch = client.post("/api/batches", json={"problem_ids": [1000, 1001]}).json()
    assert started.wait(3)
    assert client.post("/api/batches", json={"problem_ids": [1000]}).status_code == 409
    assert client.post(f"/api/batches/{batch['id']}/cancel").status_code == 200
    result = wait_batch(server._batch_manager())
    assert result["status"] == "cancelled" and calls == [1000]
    assert result["counts"]["cancelled"] == 2
