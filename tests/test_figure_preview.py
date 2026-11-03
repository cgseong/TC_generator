"""Drive 그림 미리보기, 상태 표시와 작업공간 재사용 회귀 검사."""

import io
import sqlite3
import threading
import time

import pytest
from PIL import Image

from test_boj import SCHEMA, _insert_problem
from test_server import client
from tcgen import boj, drive, figure_preview, images, server


def png():
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(data, format="PNG")
    return data.getvalue()


@pytest.fixture
def collection(client, tmp_path, monkeypatch):
    db = tmp_path / "boj.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(SCHEMA)
        _insert_problem(connection, 1010, title="다리 놓기", sections=(
            ("description", 1, "문제", '<p>설명</p><img src="test.avif">'),
            ("input", 2, "입력", "정수를 입력한다."),
            ("output", 3, "출력", "답을 출력한다."),
        ))
    monkeypatch.setattr(boj, "BOJ_DB_PATH", db)
    monkeypatch.setattr(boj, "BOJ_IMAGES_DIR", tmp_path / "missing-local-images")
    monkeypatch.setitem(server._settings, "drive_folder", "rootfolder123")
    return db


def wait_state(client, key):
    for _ in range(200):
        state = client.get(f"/api/boj/1010/figure-preview/{key}").json()
        if state["status"] != "downloading":
            return state
        time.sleep(.01)
    raise AssertionError("그림 다운로드 작업이 끝나지 않았습니다.")


def test_preview_downloads_and_import_reuses_image(client, collection, monkeypatch):
    calls = []

    def fetch(workspace, folder):
        calls.append(folder)
        images.store(workspace, 1, png())
        return {"downloaded": 1, "cached": 0}

    monkeypatch.setattr(drive, "fetch_figures", fetch)
    payload = client.get("/api/boj/1010").json()
    assert not any("내려받아 둔 파일" in note for note in payload["notes"])
    state = wait_state(client, payload["figure_preview"]["key"])
    assert state["status"] == "ready"
    assert client.get(state["images"]["1"]).content.startswith(b"\x89PNG")
    assert client.get("/api/problems").json() == []
    created = client.post("/api/problems/from-boj", json={"problem_id": 1010}).json()
    assert created["image_download"]["status"] == "ready"
    assert not any("내려받아 둔 파일" in note for note in created["notes"])
    detail = client.get(f"/api/problems/{created['slug']}").json()
    assert detail["images"]["1"]
    assert not detail["job"]["running"]
    assert calls == ["rootfolder123"]


def test_pending_preview_keeps_statement_available_and_does_not_duplicate_job(client, collection, monkeypatch):
    release = threading.Event()
    calls = []

    def fetch(workspace, folder):
        calls.append(folder)
        release.wait(3)
        images.store(workspace, 1, png())
        return {"downloaded": 1, "cached": 0}

    monkeypatch.setattr(drive, "fetch_figures", fetch)
    try:
        first = client.get("/api/boj/1010").json()
        second = client.get("/api/boj/1010").json()
        assert first["problem"]["statement"]
        assert first["figure_preview"]["status"] == "downloading"
        assert second["figure_preview"]["key"] == first["figure_preview"]["key"]
        assert len(calls) == 1
    finally:
        release.set()
    assert wait_state(client, first["figure_preview"]["key"])["status"] == "ready"


def test_drive_failure_is_reported_as_failure_not_local_missing_warning(client, collection, monkeypatch):
    def fetch(*args, **kwargs):
        raise drive.DriveError("공유 권한을 확인하세요")

    monkeypatch.setattr(drive, "fetch_figures", fetch)
    payload = client.get("/api/boj/1010").json()
    state = wait_state(client, payload["figure_preview"]["key"])
    assert state["status"] == "failed"
    assert "공유 권한" in state["error"]
    assert not any("내려받아 둔 파일" in note for note in payload["notes"])


def test_import_download_exposes_progress_without_environment_probe(client, collection, monkeypatch):
    release = threading.Event()

    def fetch(workspace, folder):
        release.wait(3)
        images.store(workspace, 1, png())
        return {"downloaded": 1, "cached": 0}

    def blocked():
        raise AssertionError("그림 다운로드가 LLM 실행 환경 점검을 기다렸습니다.")

    monkeypatch.setattr(drive, "fetch_figures", fetch)
    monkeypatch.setattr(server, "probe_environment", blocked)
    try:
        created = client.post("/api/problems/from-boj", json={"problem_id": 1010}).json()
        assert created["image_download"]["status"] == "downloading"
    finally:
        release.set()
    for _ in range(200):
        payload = client.get(f"/api/problems/{created['slug']}").json()
        if not payload["job"]["running"]:
            break
        time.sleep(.01)
    assert payload["image_download"]["status"] == "ready"
    assert payload["job"]["status"] == "done"


def test_no_drive_keeps_missing_image_note(client, collection, monkeypatch):
    monkeypatch.setitem(server._settings, "drive_folder", "")
    payload = client.get("/api/boj/1010").json()
    assert payload["figure_preview"]["status"] == "missing"
    assert any("내려받아 둔 파일" in note for note in payload["notes"])


def test_preview_image_cannot_be_requested_for_another_problem(client, collection, monkeypatch):
    def fetch(workspace, folder):
        images.store(workspace, 1, png())
        return {"downloaded": 1, "cached": 0}

    monkeypatch.setattr(drive, "fetch_figures", fetch)
    payload = client.get("/api/boj/1010").json()
    state = wait_state(client, payload["figure_preview"]["key"])
    key = state["key"]
    assert client.get(f"/api/boj/1011/figure-preview/{key}/1").status_code == 404
    assert client.get(f"/api/boj/1010/figure-preview/{key}/0").status_code == 404
