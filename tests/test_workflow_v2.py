"""새 흐름의 완료 조건, 최종 대조와 선택적 Drive 다운로드 회귀 검사."""

import io
import json
import sqlite3

import httpx
import pytest
from PIL import Image

from fixtures import FakeCLI, SOLUTION, default_responses, make_problem
from test_boj import SCHEMA, DEFAULT_SECTIONS, _insert_problem
from test_server import client, slug
from tcgen import boj, drive, exporter, images, server
from tcgen.engine.finalize import verify_cases
from tcgen.engine.pipeline import Pipeline, PipelineError
from tcgen.models import Figure
from tcgen.runner.base import RunResult, Verdict
from tcgen.workspace import create_workspace, open_workspace


def picture(fmt="PNG"):
    sink = io.BytesIO()
    Image.new("RGB", (20, 10), "red").save(sink, format=fmt)
    return sink.getvalue()


def cases(workspace, count=1):
    records = [exporter.write_case(workspace.cases_dir, n, f"{n} 2\n".encode(),
                                   f"{n + 2}\n".encode(), label="random")
               for n in range(1, count + 1)]
    workspace.save_cases_index(records)
    return records


class FinalExecutor:
    def __init__(self, verdict=Verdict.OK, output=b"3\n", stderr=""):
        self.verdict, self.output, self.calls = verdict, output, []
        self.stderr = stderr
        self.reference_calls = []
        self.validator_verdict = Verdict.OK
        self.reference_verdict = Verdict.OK

    def run_on_bytes(self, *args):
        return RunResult(verdict=self.validator_verdict, stderr="잘못된 입력")

    def run_script(self, script, stdin, limits):
        if script.name == "sol.py":
            self.reference_calls.append((script, limits))
            output = f"{sum(map(int, stdin.read_text().split()))}\n".encode()
            return RunResult(verdict=self.reference_verdict, stdout=output)
        self.calls.append((script, limits))
        return RunResult(verdict=self.verdict, stdout=self.output, stderr=self.stderr)


@pytest.mark.parametrize("stderr,status", [
    ("RecursionError: maximum recursion depth exceeded in comparison", "skipped"),
    ("MemoryError", "skipped"),
    ('Exception in thread "main" java.lang.StackOverflowError', "skipped"),
    ("IndexError: list index out of range", "failed"),
])
def test_brute_runtime_errors_from_resource_limits_are_unverified(tmp_path, stderr, status):
    workspace = create_workspace(make_problem(), base_dir=tmp_path)
    cases(workspace)
    result = verify_cases(FinalExecutor(Verdict.RE, b"", stderr), workspace)
    assert result["results"][0]["status"] == status
    assert result["passed"] == (status != "failed")


@pytest.mark.parametrize("verdict,output,status", [
    (Verdict.OK, b"3\n", "matched"),
    (Verdict.OK, b"4\n", "failed"),
    (Verdict.TLE, b"", "skipped"),
    (Verdict.MLE, b"", "skipped"),
    (Verdict.RE, b"", "failed"),
])
def test_final_distinguishes_mismatch_and_resource_limit(tmp_path, verdict, output, status):
    workspace = create_workspace(make_problem(), base_dir=tmp_path)
    cases(workspace)
    result = verify_cases(FinalExecutor(verdict, output), workspace)
    assert result["results"][0]["status"] == status
    assert result["fully_verified"] == (status == "matched")
    assert result["passed"] == (status != "failed")
    assert result["inputs_validated"] == result["outputs_verified"] == 1


def test_final_budget_marks_unvisited_brute_cases_skipped(tmp_path):
    workspace = create_workspace(make_problem(), base_dir=tmp_path)
    cases(workspace, 2)
    executor = FinalExecutor()
    result = verify_cases(executor, workspace, budget_seconds=0)
    assert result["skipped"] == 2
    assert executor.calls == []
    assert len(executor.reference_calls) == 2
    assert result["outputs_verified"] == 2


def test_export_blocks_mismatch_and_removes_old_zip(tmp_path):
    workspace = create_workspace(make_problem(solution_confirmed=True), base_dir=tmp_path)
    cases(workspace, 15)
    stale = workspace.export_dir / f"{workspace.slug}_testcases.zip"
    stale.write_bytes(b"old zip")
    pipeline = Pipeline(workspace, FakeCLI(default_responses()))
    pipeline.executor = FinalExecutor(output=b"wrong\n")
    with pytest.raises(PipelineError, match="최종 대조"):
        pipeline.export()
    assert not stale.exists()
    payload = json.loads((workspace.reports_dir / "run.json").read_text())
    assert payload["final"]["failed"] == 15
    assert "실패 15건" in (workspace.reports_dir / "report.md").read_text()


def test_export_requires_15_accepted_cases(tmp_path):
    workspace = create_workspace(make_problem(solution_confirmed=True), base_dir=tmp_path)
    cases(workspace, 14)
    with pytest.raises(PipelineError, match="최소 15개"):
        Pipeline(workspace, FakeCLI(default_responses())).export()


def test_corrupted_output_is_detected_even_without_brute_budget(tmp_path):
    workspace = create_workspace(make_problem(), base_dir=tmp_path)
    cases(workspace)
    workspace.output_path(1).write_bytes(b"tampered\n")
    executor = FinalExecutor()
    result = verify_cases(executor, workspace, budget_seconds=0)
    assert not result["passed"]
    assert result["failed"] == 1
    assert result["outputs_verified"] == 0
    assert "정답 코드 재실행 출력 불일치" in result["results"][0]["note"]
    assert executor.calls == []


@pytest.mark.parametrize("stage", ["validator", "reference"])
def test_final_input_or_reference_failure_blocks_validation(tmp_path, stage):
    workspace = create_workspace(make_problem(), base_dir=tmp_path)
    cases(workspace)
    executor = FinalExecutor()
    if stage == "validator":
        executor.validator_verdict = Verdict.RE
    else:
        executor.reference_verdict = Verdict.TLE
    result = verify_cases(executor, workspace)
    assert result["failed"] == 1
    assert not result["passed"]
    assert result["skipped"] == 0
    assert executor.calls == []


def test_minimum_case_count_applies_to_defaults_model_and_api(client, slug):
    from tcgen.models import CasePlan

    assert CasePlan().count == 15
    assert CasePlan.from_dict({"count": 1}).count == 15
    assert client.put(f"/api/problems/{slug}", json={"case_count": 14}).status_code == 422
    response = client.put(f"/api/problems/{slug}", json={"case_count": 15})
    assert response.status_code == 200
    assert response.json()["case_plan"]["count"] == 15


def test_insufficient_distinct_mutants_cannot_claim_strength(tmp_path):
    workspace = create_workspace(make_problem(solution_confirmed=True), base_dir=tmp_path)
    cases(workspace)
    workspace.solution_path.write_text(SOLUTION)
    cli = FakeCLI(default_responses(mutants={"mutants": [
        {"name": f"duplicate{i}", "kind": "wa", "source": SOLUTION} for i in range(6)
    ]}))
    with pytest.raises(PipelineError, match="WA 4개 이상"):
        Pipeline(workspace, cli).verify_strength()
    assert "mutants-fill-2" in cli.calls


def test_catalog_lists_every_problem_without_importing(client, tmp_path, monkeypatch):
    path = tmp_path / "collection.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    for n, title in ((1000, "A+B"), (1001, "100% 문제"), (1002, "터렛")):
        _insert_problem(connection, n, title=title, sections=DEFAULT_SECTIONS)
    for n in range(1003, 1011):
        _insert_problem(connection, n, title=f"문제 {n}", sections=DEFAULT_SECTIONS)
    connection.close()
    monkeypatch.setattr(boj, "BOJ_DB_PATH", path)
    page = client.get("/api/boj?limit=2").json()
    assert page["total"] == 11
    assert [p["problem_id"] for p in page["items"]] == [1000, 1001]
    assert client.get("/api/boj?offset=2&limit=2").json()["items"][0]["problem_id"] == 1002
    assert len(client.get("/api/boj").json()["items"]) == 10
    assert [p["problem_id"] for p in client.get("/api/boj?offset=10").json()["items"]] == [1010]
    assert client.get("/api/boj", params={"q": "%"}).json()["total"] == 1
    preview = client.get("/api/boj/1002").json()
    assert preview["problem"]["title"] == "터렛"
    assert preview["problem"]["statement"]
    assert client.get("/api/problems").json() == []


def test_avif_upload_can_be_previewed_and_invalidates_solution(client, slug):
    workspace = open_workspace(slug)
    workspace.save_problem(workspace.load_problem().with_changes(solution_confirmed=True))
    response = client.post(f"/api/problems/{slug}/figures/1", files={"file": ("evil/1.avif", picture("AVIF"), "image/avif")})
    assert response.status_code == 200
    image = client.get(response.json()["url"])
    assert image.content.startswith(b"\x89PNG")
    payload = client.get(f"/api/problems/{slug}").json()
    assert not payload["problem"]["solution_confirmed"]
    assert payload["images"]["1"]
    before = image.content
    assert client.post(f"/api/problems/{slug}/figures/1", files={"file": ("bad.avif", b"not an image")}).status_code == 400
    assert client.get(response.json()["url"]).content == before


def test_figure_transcription_feeds_solution_prompts(tmp_path):
    workspace = create_workspace(make_problem(figures=(Figure(ref="[그림 1]", role="spec"),)), base_dir=tmp_path)
    images.store(workspace, 1, picture("AVIF"))
    pipeline = Pipeline(workspace, FakeCLI(default_responses(**{"figure-1": {"description": "좌상단 원점"}})))
    assert pipeline.describe_figures()["described"] == 1
    assert pipeline.problem.figures[0].description == "좌상단 원점"


def test_missing_critical_image_blocks_solution(tmp_path):
    workspace = create_workspace(make_problem(figures=(Figure(ref="[그림 1]", role="spec"),)), base_dir=tmp_path)
    cli = FakeCLI(default_responses())
    with pytest.raises(PipelineError, match="첨부"):
        Pipeline(workspace, cli).build_solution()
    assert cli.calls == []


def test_public_drive_downloads_only_selected_problem_and_caches(tmp_path, monkeypatch):
    monkeypatch.delenv("TCGEN_DRIVE_API_KEY", raising=False)
    monkeypatch.delenv("TCGEN_DRIVE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(drive, "_cache", {})
    workspace = create_workspace(make_problem(boj_id=1002, figures=(Figure(ref="[그림 1]", role="spec"),)), base_dir=tmp_path)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/embeddedfolderview":
            if request.url.params["id"] == "rootfolder123":
                return httpx.Response(200, text='<a href="https://drive.google.com/drive/folders/selected1002">1002</a><a href="https://drive.google.com/drive/folders/otherproblem">1004</a>')
            assert request.url.params["id"] == "selected1002"
            return httpx.Response(200, text='<a href="https://drive.google.com/file/d/file1002image/view">1.avif</a>')
        assert request.url.host == "drive.usercontent.google.com"
        assert request.url.params["id"] == "file1002image"
        return httpx.Response(200, content=picture("AVIF"))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert drive.fetch_figures(workspace, "rootfolder123", client=client) == {"downloaded": 1, "cached": 0}
        assert drive.fetch_figures(workspace, "rootfolder123", client=client) == {"downloaded": 0, "cached": 1}
    assert len(calls) == 3
    assert images.find(workspace, 1).read_bytes().startswith(b"\x89PNG")


def test_drive_rejects_ambiguous_numbering_before_download(tmp_path, monkeypatch):
    monkeypatch.delenv("TCGEN_DRIVE_API_KEY", raising=False)
    monkeypatch.delenv("TCGEN_DRIVE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(drive, "_cache", {})
    workspace = create_workspace(make_problem(boj_id=1002, figures=(Figure(ref="1", role="spec"),)), base_dir=tmp_path)

    def handle(request):
        assert request.url.path == "/embeddedfolderview"
        content = ('<a href="https://drive.google.com/drive/folders/selected1002">1002</a>'
                   if request.url.params["id"] == "rootfolder123" else
                   '<a href="https://drive.google.com/file/d/file1002image/view">2.avif</a>')
        return httpx.Response(200, text=content)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(drive.DriveError, match="그림 수·번호"):
            drive.fetch_figures(workspace, "rootfolder123", client=client)
    assert images.find(workspace, 1) is None


def test_private_drive_api_errors_never_expose_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGEN_DRIVE_API_KEY", "SECRET")
    workspace = create_workspace(make_problem(boj_id=1002, figures=(Figure(ref="1", role="spec"),)), base_dir=tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403))) as client:
        with pytest.raises(drive.DriveError) as error:
            drive.fetch_figures(workspace, "rootfolder123", client=client)
    assert "SECRET" not in str(error.value)


def test_drive_settings_persist_without_credentials(client, tmp_path, monkeypatch):
    monkeypatch.setenv("TCGEN_DRIVE_ACCESS_TOKEN", "PRIVATE_TOKEN")
    result = client.post("/api/settings", json={"drive_folder": "https://drive.google.com/drive/folders/rootfolder123"})
    assert result.status_code == 200
    saved = server.SETTINGS_PATH.read_text()
    assert "rootfolder123" in saved
    assert "PRIVATE_TOKEN" not in saved
    assert server._load_settings()["drive_folder"] == "rootfolder123"


def test_same_kind_generators_use_distinct_scripts(tmp_path):
    workspace = create_workspace(make_problem(), base_dir=tmp_path)
    cli = FakeCLI(default_responses(generators={"generators": [
        {"name": "negative", "kind": "anti", "source": "print(-1)"},
        {"name": "last_max", "kind": "anti", "source": "print(1000)"},
        {"name": "random", "kind": "random", "source": "print(0)"},
        {"name": "scale", "kind": "scale", "source": "print(999)"},
    ]}))
    specs = Pipeline(workspace, cli)._prepare_generators(workspace.load_problem())
    paths = [path for kind, path in specs if kind == "anti"]
    assert len(paths) == 2
    assert paths[0] != paths[1]
    assert {p.read_text() for p in paths} == {"print(-1)", "print(1000)"}


def test_problem_edit_invalidates_cases_and_download(client, slug):
    workspace = open_workspace(slug)
    workspace.save_problem(workspace.load_problem().with_changes(solution_confirmed=True))
    cases(workspace)
    zip_path = workspace.export_dir / f"{workspace.slug}_testcases.zip"
    zip_path.write_bytes(b"old")
    (workspace.reports_dir / "run.json").write_text(json.dumps({"stress": {"passed": True}, "final": {"passed": True}}))
    response = client.put(f"/api/problems/{slug}", json={"constraints": "새 제약"})
    assert response.status_code == 200
    payload = client.get(f"/api/problems/{slug}").json()
    assert not payload["has_zip"]
    assert payload["cases"] == []
    assert payload["results"]["stress"] is None
    assert client.get(f"/api/problems/{slug}/download").status_code == 404
