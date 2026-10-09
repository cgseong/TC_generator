"""로컬 서버 API 테스트."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from fixtures import make_problem
from tcgen import server, workspace as workspace_module
from tcgen.workspace import create_workspace


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_module, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(server, "_registry", server.SessionRegistry())
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


@pytest.fixture
def slug(tmp_path, client):
    return create_workspace(make_problem(), base_dir=tmp_path).slug


class TestEnvironment:
    def test_env_reports_tools_and_banner(self, client):
        payload = client.get("/api/env").json()
        assert "tools" in payload
        assert payload["isolated"] is False
        assert "격리" in payload["banner"]

    def test_env_lists_skipped_languages(self, client):
        payload = client.get("/api/env").json()
        assert isinstance(payload["skipped_languages"], list)

    def test_model_setting_round_trips(self, client):
        client.post("/api/settings", json={"model": "sonnet"})
        assert client.get("/api/env").json()["model"] == "sonnet"
        client.post("/api/settings", json={"model": None})


class TestProblemCrud:
    def test_create_and_list(self, client):
        created = client.post("/api/problems", json={"title": "두 수의 합"}).json()
        assert created["slug"] == "두-수의-합"

        listed = client.get("/api/problems").json()
        assert [item["slug"] for item in listed] == ["두-수의-합"]

    def test_get_unknown_problem_is_404(self, client):
        assert client.get("/api/problems/없는문제").status_code == 404

    def test_update_fields(self, client, slug):
        response = client.put(
            f"/api/problems/{slug}",
            json={"title": "새 제목", "time_ms": 500, "case_count": 7},
        )
        payload = response.json()
        assert payload["title"] == "새 제목"
        assert payload["limits"]["time_ms"] == 500
        assert payload["case_plan"]["count"] == 7

    def test_update_examples(self, client, slug):
        payload = client.put(
            f"/api/problems/{slug}",
            json={"examples": [{"input": "1 2\n", "output": "3\n"}]},
        ).json()
        assert payload["examples"] == [{"input": "1 2\n", "output": "3\n"}]

    def test_setting_limits_clears_assumed_flag(self, client, slug):
        payload = client.put(f"/api/problems/{slug}", json={"time_ms": 1000}).json()
        assert payload["limits"]["assumed"] is False

    def test_detail_includes_job_and_cases(self, client, slug):
        payload = client.get(f"/api/problems/{slug}").json()
        assert payload["cases"] == []
        assert payload["job"]["running"] is False
        assert payload["has_zip"] is False

    def test_confirm_interpretation(self, client, slug):
        payload = client.post(f"/api/problems/{slug}/confirm-interpretation").json()
        assert payload["interpretation_confirmed"] is True


class TestJobs:
    def test_cancel_when_idle_returns_false(self, client, slug):
        assert client.post(f"/api/problems/{slug}/cancel").json() == {"cancelled": False}

    def test_second_job_is_rejected(self, client, slug):
        release = threading.Event()
        server._registry.get(slug).start("busy", lambda cancel: release.wait(timeout=5))
        try:
            response = client.post(f"/api/problems/{slug}/cases")
            assert response.status_code == 409
            assert "실행 중" in response.json()["detail"]
        finally:
            release.set()

    def test_cancel_running_job(self, client, slug):
        release = threading.Event()

        def work(cancel_event):
            while not cancel_event.is_set():
                time.sleep(0.01)
            return "stopped"

        server._registry.get(slug).start("busy", work)
        try:
            assert client.post(f"/api/problems/{slug}/cancel").json() == {"cancelled": True}
        finally:
            release.set()


class TestArtifacts:
    def test_download_before_export_is_404(self, client, slug):
        assert client.get(f"/api/problems/{slug}/download").status_code == 404

    def test_report_placeholder(self, client, slug):
        assert "아직" in client.get(f"/api/problems/{slug}/report").text

    def test_missing_case_is_404(self, client, slug):
        assert client.get(f"/api/problems/{slug}/case/1").status_code == 404


class TestStaticSite:
    def test_index_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "TC_generator" in response.text

    def test_assets_are_local_only(self, client):
        # 폐쇄망에서도 동작해야 하므로 외부 CDN 참조가 없어야 한다.
        html = client.get("/").text
        assert "http://" not in html
        assert "https://" not in html
