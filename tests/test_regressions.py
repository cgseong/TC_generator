"""리뷰에서 발견된 결함에 대한 회귀 테스트.

각 테스트는 실제로 재현된 결함 하나에 대응한다.
"""

import json

import pytest
from fastapi.testclient import TestClient

from fixtures import SOLUTION, FakeCLI, default_responses, make_problem
from tcgen import server, workspace as workspace_module
from tcgen.engine.pipeline import Pipeline
from tcgen.engine.solution import run_stress
from tcgen.models import CasePlan, Limits
from tcgen.runner import LocalRunner, Verdict, python_command
from tcgen.workspace import WorkspaceError, create_workspace, normalize_slug, open_workspace


class TestPathTraversal:
    """URL의 slug로 workspaces 밖을 열 수 있었다 (보안 CRITICAL)."""

    @pytest.mark.parametrize(
        "evil",
        [r"C:\Windows\Temp", r"..\..\outside", r"\Windows", "..", ".", "a/b", "", "x" * 200],
    )
    def test_dangerous_slug_is_rejected(self, evil, tmp_path):
        with pytest.raises(WorkspaceError):
            open_workspace(evil, base_dir=tmp_path)

    def test_normal_slug_still_opens(self, tmp_path):
        created = create_workspace(make_problem(), base_dir=tmp_path)
        assert open_workspace(created.slug, base_dir=tmp_path).root == created.root

    def test_case_differences_resolve_to_one_name(self):
        # 같은 폴더가 서로 다른 잠금 키를 갖던 문제 (동시 실행 경합)
        assert normalize_slug("DEMO") == normalize_slug("demo")


class TestStressRoundsGate:
    """stress_rounds=0이면 대조 0회로 '통과' 처리되어 확정 게이트가 뚫렸다."""

    @pytest.mark.parametrize("rounds", [0, -1])
    def test_zero_or_negative_rounds_never_passes(self, rounds):
        def exploding_executor(*args, **kwargs):  # 실행되면 안 된다
            raise AssertionError("대조가 실행되어서는 안 됩니다")

        outcome = run_stress(
            exploding_executor,
            None,
            None,
            None,
            rounds=rounds,
            limits=Limits(),
        )
        assert not outcome.passed
        assert outcome.rounds_run == 0

    def test_case_plan_clamps_lower_bound(self):
        plan = CasePlan.from_dict({"count": -5, "stress_rounds": 0, "max_input_bytes": 0})
        assert plan.count >= 1
        assert plan.stress_rounds >= 1
        assert plan.max_input_bytes >= 1


class TestOutputEncoding:
    """-I 가 PYTHONIOENCODING 을 무시해 한글 출력이 cp949로 기록됐다."""

    def test_non_ascii_output_is_utf8(self, tmp_path):
        script = tmp_path / "ko.py"
        script.write_text("print('정답입니다')", encoding="utf-8")

        result = LocalRunner().run(python_command(script), None, Limits(time_ms=5000))

        assert result.verdict is Verdict.OK
        assert result.stdout.decode("utf-8").strip() == "정답입니다"

    def test_characters_outside_cp949_do_not_crash(self, tmp_path):
        script = tmp_path / "emoji.py"
        script.write_text("print('\\u2764')", encoding="utf-8")

        result = LocalRunner().run(python_command(script), None, Limits(time_ms=5000))

        assert result.verdict is Verdict.OK


class TestConfirmationInvalidation:
    """문제를 바꿔도 solution_confirmed 가 남아 다른 문제의 케이스를 내보낼 수 있었다."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setattr(workspace_module, "WORKSPACES_DIR", tmp_path)
        monkeypatch.setattr(server, "_registry", server.SessionRegistry())
        with TestClient(server.app, base_url="http://127.0.0.1:8765") as test_client:
            yield test_client

    @pytest.fixture
    def confirmed_slug(self, tmp_path, client):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)
        workspace.save_problem(workspace.load_problem().with_changes(solution_confirmed=True))
        return workspace.slug

    def test_changing_statement_clears_confirmation(self, client, confirmed_slug):
        payload = client.put(
            f"/api/problems/{confirmed_slug}", json={"statement": "완전히 다른 문제"}
        ).json()
        assert payload["solution_confirmed"] is False

    def test_changing_limits_clears_confirmation(self, client, confirmed_slug):
        payload = client.put(f"/api/problems/{confirmed_slug}", json={"time_ms": 500}).json()
        assert payload["solution_confirmed"] is False

    def test_cosmetic_change_keeps_confirmation(self, client, confirmed_slug):
        payload = client.put(f"/api/problems/{confirmed_slug}", json={"hints": "메모"}).json()
        assert payload["solution_confirmed"] is True


class TestRequestGuards:
    """로컬 서버가 교차 출처 요청과 Host 위조에 열려 있었다."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setattr(workspace_module, "WORKSPACES_DIR", tmp_path)
        monkeypatch.setattr(server, "_registry", server.SessionRegistry())
        with TestClient(server.app, base_url="http://127.0.0.1:8765") as test_client:
            yield test_client

    def test_cross_origin_post_is_rejected(self, client):
        response = client.post(
            "/api/problems", json={"title": "x"}, headers={"Origin": "http://evil.example"}
        )
        assert response.status_code == 403

    def test_cross_site_fetch_metadata_is_rejected(self, client):
        response = client.post(
            "/api/problems", json={"title": "x"}, headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert response.status_code == 403

    def test_foreign_host_header_is_rejected(self, client):
        assert client.get("/api/env", headers={"Host": "evil.example"}).status_code == 421

    def test_same_origin_request_passes(self, client):
        response = client.post(
            "/api/problems",
            json={"title": "같은 출처"},
            headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 200

    def test_non_browser_client_passes(self, client):
        # curl·테스트처럼 Origin을 보내지 않는 클라이언트는 그대로 동작해야 한다.
        assert client.post("/api/problems", json={"title": "도구"}).status_code == 200

    @pytest.mark.parametrize(
        "payload",
        [{"stress_rounds": 0}, {"time_ms": 0}, {"case_count": -5}, {"max_input_bytes": 10**10}],
    )
    def test_out_of_range_values_are_rejected(self, client, tmp_path, payload):
        slug = create_workspace(make_problem(), base_dir=tmp_path).slug
        assert client.put(f"/api/problems/{slug}", json=payload).status_code == 422


class TestWizardReport:
    """단계마다 Pipeline이 새로 만들어져 위저드 모드 리포트가 비어 있었다."""

    def _fresh_pipeline(self, workspace):
        # 서버가 매 요청마다 하는 일을 그대로 재현한다.
        return Pipeline(workspace, FakeCLI(default_responses()))

    def test_report_keeps_evidence_across_pipeline_instances(self, tmp_path):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)

        self._fresh_pipeline(workspace).build_solution(author_source=SOLUTION)
        self._fresh_pipeline(workspace).build_cases()
        self._fresh_pipeline(workspace).verify_strength()
        self._fresh_pipeline(workspace).export()
        path = self._fresh_pipeline(workspace).write_report()

        content = path.read_text(encoding="utf-8")
        assert "수행하지 않음" not in content
        assert "강도 검증을 수행하지 않았습니다" not in content
        assert "kill rate" in content
        assert "브루트포스 대조: 통과" in content

    def test_run_json_accumulates_each_stage(self, tmp_path):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)
        self._fresh_pipeline(workspace).build_solution(author_source=SOLUTION)
        self._fresh_pipeline(workspace).build_cases()

        stored = json.loads((workspace.reports_dir / "run.json").read_text(encoding="utf-8"))
        assert stored["stress"]["passed"] is True
        assert stored["cases"]["case_count"] > 0
