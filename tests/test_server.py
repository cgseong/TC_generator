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


class TestPdfUpload:
    PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\ntrailer\n"

    def upload(self, client, slug, content, filename="지문.pdf"):
        return client.post(
            f"/api/problems/{slug}/source-pdf",
            files={"file": (filename, content, "application/pdf")},
        )

    def test_stores_pdf_under_a_fixed_name(self, client, slug, tmp_path):
        response = self.upload(client, slug, self.PDF)

        assert response.status_code == 200
        stored = tmp_path / slug / "assets" / "statement.pdf"
        assert stored.read_bytes() == self.PDF
        assert response.json()["source_pdf"] == "statement.pdf"

    def test_client_filename_cannot_escape_the_workspace(self, client, slug, tmp_path):
        # 업로드된 이름을 경로로 쓰면 작업공간 밖에 파일을 심을 수 있다.
        self.upload(client, slug, self.PDF, filename="../../evil.pdf")

        assert (tmp_path / slug / "assets" / "statement.pdf").exists()
        assert not (tmp_path / "evil.pdf").exists()

    def test_rejects_a_file_that_is_not_a_pdf(self, client, slug, tmp_path):
        response = self.upload(client, slug, b"PK\x03\x04 not a pdf", filename="x.pdf")

        assert response.status_code == 400
        assert not (tmp_path / slug / "assets" / "statement.pdf").exists()

    def test_rejects_an_oversized_file(self, client, slug, tmp_path):
        oversized = self.PDF + b"0" * server.MAX_PDF_BYTES
        response = self.upload(client, slug, oversized)

        assert response.status_code == 413
        assert not (tmp_path / slug / "assets" / "statement.pdf").exists()

    def test_parse_pdf_requires_an_uploaded_file(self, client, slug):
        assert client.post(f"/api/problems/{slug}/parse-pdf").status_code == 409

    def test_figures_can_be_corrected_by_hand(self, client, slug):
        payload = client.put(
            f"/api/problems/{slug}",
            json={"figures": [{"ref": "[그림1]", "role": "spec", "description": "좌상단 (1,1)"}]},
        ).json()

        assert payload["figures"][0]["description"] == "좌상단 (1,1)"
        # 그림 전사는 풀이의 전제다. 고치면 기존 확정은 무효가 되어야 한다.
        assert payload["solution_confirmed"] is False
