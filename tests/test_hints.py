"""힌트는 원문에 남겨 두되, 새 문제 zip과 묶음 zip에서는 항상 제외한다."""
import io
import json
import sqlite3
import zipfile

import pytest
from PIL import Image

from fixtures import FakeCLI, default_responses
from test_initial_catalog import collection
from test_problem_export import export_metadata, ready_workspace
from test_server import client
from tcgen import images, problem_export
from tcgen.engine import metadata
from tcgen.engine.pipeline import Pipeline
from tcgen.models import Figure


@pytest.mark.parametrize("text", [
    "", "N은 짝수입니다.", "여기를 누르면 1000번 예제 소스를 볼 수 있습니다.",
    "증명은 https://example.org 를 참고하세요.", "[그림 99]를 참고하세요.",
    '<script>alert("hint")</script>',
])
def test_every_hint_is_omitted_without_changing_original_data(tmp_path, text):
    ws = ready_workspace(tmp_path, hints=text)
    before = ws.load_problem().to_dict()
    info = export_metadata(ws)
    info["hint_review"] = {"text": "stale cached hint"}
    result = problem_export.export_problem(ws, info)
    assert not result.has_errors
    assert problem_export.verify_problem_zip(result.zip_path) == ()
    with zipfile.ZipFile(result.zip_path) as archive:
        payload = json.loads(archive.read("1/problem.json"))
    assert payload["hint"] == {"format": "html", "value": ""}
    assert ws.load_problem().to_dict() == before
    assert not any("힌트" in issue.message for issue in result.issues)


@pytest.mark.parametrize("ref", ["[그림 1]", "[풀이 도해]"])
@pytest.mark.parametrize("has_file", [True, False])
def test_hint_only_images_are_not_appended_to_statement_or_archived(tmp_path, ref, has_file):
    ws = ready_workspace(tmp_path, hints=f"{ref}을 참고하세요.",
                         figures=(Figure(ref=ref, description="풀이 그림"),))
    if has_file:
        data = io.BytesIO(); Image.new("RGB", (2, 2), "white").save(data, format="PNG")
        images.store(ws, 1, data.getvalue())
    result = problem_export.export_problem(ws, export_metadata(ws))
    assert not result.has_errors
    with zipfile.ZipFile(result.zip_path) as archive:
        payload = json.loads(archive.read("1/problem.json"))
        assert payload["hint"]["value"] == ""
        assert "<img" not in payload["description"]["value"]
        assert not any("/images/" in name for name in archive.namelist())


def test_images_shared_with_the_statement_are_kept(tmp_path):
    ws = ready_workspace(tmp_path, statement="문제 그림 [그림 1]", hints="[그림 1]로 풀이를 설명한다.",
                         figures=(Figure(ref="[그림 1]", description="문제 그림"),))
    data = io.BytesIO(); Image.new("RGB", (2, 2), "white").save(data, format="PNG")
    images.store(ws, 1, data.getvalue())
    result = problem_export.export_problem(ws, export_metadata(ws))
    with zipfile.ZipFile(result.zip_path) as archive:
        payload = json.loads(archive.read("1/problem.json"))
        assert '<img src="images/1.png"' in payload["description"]["value"]
        assert "1/images/1.png" in archive.namelist()
        assert payload["hint"]["value"] == ""


def test_old_hint_review_cache_is_not_reused_or_written_to_report(tmp_path):
    ws = ready_workspace(tmp_path, hints="원본 힌트")
    ws.solution_path.write_text("print(3)")
    cached = metadata.classify(ws.load_problem(), ws.solution_path.read_text(), FakeCLI(default_responses()))
    cached["hint_review"] = {"text": "old cleaned hint", "needs_review": True}
    cli = FakeCLI({})
    pipeline = Pipeline(ws, cli)
    pipeline._state["export_metadata"] = cached
    result = pipeline.prepare_export_metadata(final_checked=True)
    assert "hint_review" not in result
    assert "export-metadata" not in cli.calls
    assert "힌트 정리" not in (ws.reports_dir / "report.md").read_text()
    assert "hint_review" not in json.loads((ws.reports_dir / "run.json").read_text())["export_metadata"]
    assert ws.load_problem().hints == "원본 힌트"


@pytest.mark.parametrize("shared_image", [False, True])
def test_bundle_removes_legacy_hints_and_only_unshared_hint_images(tmp_path, shared_image):
    ws = ready_workspace(tmp_path)
    path = problem_export.export_problem(ws, export_metadata(ws)).zip_path
    with zipfile.ZipFile(path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    payload = json.loads(files["1/problem.json"])
    payload["hint"] = {"format": "html", "value": '<p>old hint <img src="images/1.png" /></p>'}
    if shared_image:
        payload["description"]["value"] += '<p><img src="images/1.png" /></p>'
    files["1/problem.json"] = problem_export.encode_problem_json(payload).encode()
    png = io.BytesIO(); Image.new("RGB", (2, 2), "white").save(png, format="PNG")
    files["1/images/1.png"] = png.getvalue()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    assert problem_export.verify_problem_zip(path) == ()
    before = path.read_bytes()
    target = tmp_path / "bundle.zip"
    problem_export.bundle([path], target)
    assert problem_export.verify_problem_zip(target) == ()
    with zipfile.ZipFile(target) as archive:
        result = json.loads(archive.read("1/problem.json"))
        assert result["hint"] == {"format": "html", "value": ""}
        assert ("1/images/1.png" in archive.namelist()) == shared_image
        assert result["samples"] == payload["samples"]
        assert result["description"] == payload["description"]
    assert path.read_bytes() == before


def test_preview_api_preserves_source_without_hint_classification(client, collection):
    html = '<p>여기를 누르면 1000번 예제 소스를 볼 수 있습니다.</p>'
    with sqlite3.connect(collection) as connection:
        connection.execute("INSERT INTO problem_sections VALUES (?, ?, ?, ?, ?, ?)",
                           (1000, "hint", 99, "힌트", html, ""))
    payload = client.get("/api/boj/1000").json()
    assert "예제 소스" in payload["problem"]["hints"]
    assert "hint_review" not in payload
    with sqlite3.connect(collection) as connection:
        assert connection.execute("SELECT body_html FROM problem_sections WHERE problem_id=1000 AND section_id='hint'").fetchone()[0] == html
