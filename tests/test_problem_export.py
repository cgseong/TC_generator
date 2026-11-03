"""문제 묶음 스키마, 그림 위치, 평가 기준, 예제와 완료 표시 회귀 검사."""
import copy
import io
import json
import threading
import zipfile

import pytest
from PIL import Image

from fixtures import FakeCLI, default_responses, make_problem
from test_initial_catalog import collection
from test_server import client
from tcgen import exporter, history, images, problem_export, server
from tcgen.engine import metadata
from tcgen.models import Figure
from tcgen.runner.base import RunResult, Verdict
from tcgen.workspace import create_workspace


def assessment(scores=(0, 0, 0, 0), **changes):
    payload = copy.deepcopy(default_responses()["export-metadata"])
    for key, score in zip(metadata.AXES, scores):
        payload["assessment"][key]["score"] = score
    payload.update(changes)
    return payload


def ready_workspace(root, number=1000, **changes):
    ws = create_workspace(make_problem(boj_id=number, solution_confirmed=True, **changes), base_dir=root)
    records = [exporter.write_case(ws.cases_dir, i, f"{i} 2\n".encode(), f"{i+2}\n".encode(), label="example" if i == 1 else "edge") for i in range(1, 4)]
    ws.save_cases_index(records)
    return ws


def export_metadata(ws):
    result = metadata.normalize_classification(assessment())
    return {**result, **metadata.select_samples(ws, object())}


def successful_export(ws):
    result = problem_export.export_problem(ws, export_metadata(ws))
    (ws.reports_dir / "run.json").write_text(json.dumps({"final": {"passed": True}, "export": result.to_dict()}))
    history.record(ws, exported=True)
    return result.zip_path


@pytest.mark.parametrize("scores, expected", [
    ((0, 0, 0, 0), "VeryLow"), ((1, 1, 0, 0), "VeryLow"),
    ((1, 1, 1, 0), "Low"), ((2, 1, 1, 1), "Low"),
    ((2, 2, 1, 1), "Mid"), ((3, 2, 2, 2), "Mid"),
    ((3, 3, 2, 2), "High"), ((3, 3, 3, 3), "High"),
    ((4, 3, 3, 3), "VeryHigh"), ((4, 4, 4, 4), "VeryHigh"),
])
def test_difficulty_is_calculated_by_fixed_boundaries(scores, expected):
    result = metadata.normalize_classification(assessment(scores, difficulty="untrusted name"))
    assert result["difficulty"] == expected
    assert result["difficulty_score"] == sum(scores)
    assert result["rubric_version"] == "tcgen-difficulty-v2"


@pytest.mark.parametrize("value", [True, -1, 5, 1.5, "2"])
def test_invalid_axis_score_is_rejected(value):
    payload = assessment()
    payload["assessment"]["knowledge"]["score"] = value
    with pytest.raises(metadata.MetadataError):
        metadata.normalize_classification(payload)


def test_tags_are_canonical_deduplicated_and_bounded():
    result = metadata.normalize_classification(assessment(tags=["DFS", "깊이 우선 탐색", "DP"]))
    assert result["tags"] == ["깊이 우선 탐색", "동적 계획법"]
    for tags in (["new arbitrary tag"], [], ["구현", "정렬", "수학", "해시"]):
        with pytest.raises(metadata.MetadataError):
            metadata.normalize_classification(assessment(tags=tags))
    assert metadata.normalize_classification(assessment(field=99))["field"] == 0


def test_same_content_uses_cached_assessment_even_if_case_plan_changes():
    problem = make_problem()
    result = metadata.classify(problem, "print(1)", FakeCLI({"export-metadata": assessment()}))
    changed = problem.with_changes(slug="different-folder", case_plan=problem.case_plan.__class__(count=40))
    assert metadata.classify(changed, "print(1)", FakeCLI({}), cached=result) == result
    assert metadata.classification_key(problem, "print(2)") != result["classification_key"]
    assert metadata.classification_key(problem.with_changes(statement="changed"), "print(1)") != result["classification_key"]


def test_malformed_metadata_retries_with_validation_feedback():
    cli = FakeCLI({"export-metadata": [assessment(tags=["unknown"]), assessment(tags=["수학"])]})
    assert metadata.classify(make_problem(), "print(1)", cli)["tags"] == ["수학"]


def test_problem_archive_matches_attached_structure_and_constants(tmp_path):
    ws = ready_workspace(tmp_path)
    result = problem_export.export_problem(ws, export_metadata(ws))
    assert result.zip_path.name.endswith("_problem.zip")
    assert problem_export.verify_problem_zip(result.zip_path) == ()
    with zipfile.ZipFile(result.zip_path) as z:
        payload = json.loads(z.read("1/problem.json"))
        assert set(payload) == problem_export.REQUIRED
        assert set(z.namelist()) == {"1/problem.json"} | {f"1/testcase/{i}.{suffix}" for i in range(1, 4) for suffix in ("in", "out")}
    assert payload["display_id"] == "1000"
    assert payload["source"] == "Baekjoon Online Judge"
    assert payload["template"] == {} and payload["answers"] == []
    assert payload["spj"] is None and payload["rule_type"] == "ACM"
    assert payload["time_limit"] == ws.load_problem().limits.time_ms
    assert payload["memory_limit"] == ws.load_problem().limits.memory_mb
    assert len(payload["samples"]) == 3
    for sample in payload["samples"]:
        assert int(sample["output"]) == sum(map(int, sample["input"].split()))


def test_figures_are_embedded_at_placeholder_and_statement_is_escaped(tmp_path):
    ws = ready_workspace(tmp_path, statement='앞 <script>alert(1)</script>\n[그림 1]\n뒤', figures=(Figure(ref="[그림 1]", description="그림"),))
    sink = io.BytesIO(); Image.new("RGB", (3, 3), "blue").save(sink, format="PNG")
    images.store(ws, 1, sink.getvalue())
    payload, figures = problem_export.build_payload(ws, export_metadata(ws))
    html = payload["description"]["value"]
    assert html.index("앞") < html.index('<img src="images/1.png"') < html.index("뒤")
    assert set(figures) == {"images/1.png"}
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "[그림 1]" not in html
    with zipfile.ZipFile(problem_export.export_problem(ws, export_metadata(ws)).zip_path) as z:
        assert '<img src="images/1.png"' in json.loads(z.read("1/problem.json"))["description"]["value"]
        assert z.read("1/images/1.png").startswith(b"\x89PNG")


def test_missing_image_blocks_problem_export(tmp_path):
    ws = ready_workspace(tmp_path, statement="[그림 1]", figures=(Figure(ref="[그림 1]", description="전사"),))
    with pytest.raises(exporter.ExportError, match="파일이 없습니다"):
        problem_export.export_problem(ws, export_metadata(ws))


def test_large_cases_are_excluded_from_user_facing_samples(tmp_path):
    ws = ready_workspace(tmp_path)
    large = exporter.write_case(ws.cases_dir, 4, b"1 " * 10000, b"10000", label="scale")
    ws.save_cases_index((*ws.load_cases_index(), large))
    selected = metadata.select_samples(ws, object())
    assert 2 <= len(selected["samples"]) <= 4
    assert all(len(sample["input"].encode()) <= 4096 for sample in selected["samples"])
    assert all(source["case_index"] != 4 for source in selected["sample_sources"])


def test_generated_sample_mismatch_blocks_export(tmp_path):
    ws = create_workspace(make_problem(solution_confirmed=True), base_dir=tmp_path)
    (ws.gens_dir / "stress_gen.py").write_text("# fake")
    class Executor:
        def run_script(self, *args, **kwargs):
            return RunResult(verdict=Verdict.OK, stdout=b"3 4\n")
        def run_on_bytes(self, path, *args, **kwargs):
            return RunResult(verdict=Verdict.OK, stdout=b"8" if path.name == "brute.py" else b"7")
    with pytest.raises(metadata.MetadataError, match="불일치"):
        metadata.select_samples(ws, Executor())


def test_bundled_problems_have_contiguous_folders_and_no_metadata_loss(tmp_path):
    first, second = ready_workspace(tmp_path, 1000), ready_workspace(tmp_path, 1001)
    paths = [successful_export(first), successful_export(second)]
    target = tmp_path / "problem-export.zip"
    problem_export.bundle(paths, target)
    assert problem_export.verify_problem_zip(target) == ()
    with zipfile.ZipFile(target) as z:
        assert json.loads(z.read("1/problem.json"))["display_id"] == "1000"
        assert json.loads(z.read("2/problem.json"))["display_id"] == "1001"


def test_incomplete_problem_zip_is_rejected(tmp_path):
    ws = ready_workspace(tmp_path)
    path = successful_export(ws)
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("unexpected.txt", "not part of a problem")
    assert problem_export.verify_problem_zip(path)
    with pytest.raises(exporter.ExportError):
        problem_export.bundle([path], tmp_path / "invalid.zip")


def test_manual_uncheck_persists_but_does_not_delete_archive(tmp_path):
    ws = ready_workspace(tmp_path)
    path = successful_export(ws)
    history.set_completion(tmp_path, 1000, False)
    item = history.snapshot(tmp_path, synchronize=True)[0]
    assert not item["completed"] and item["exported"] and path.is_file()
    assert history.by_problem(tmp_path)["1000"]["completion_source"] == "manual"
    assert history.set_completion(tmp_path, 1000, True)["completed"]
    history.set_completion(tmp_path, 1000, False)
    history.record(ws, exported=True)
    assert history.get(ws)["completed"] and history.get(ws)["completion_source"] == "automatic"


def test_manual_completion_without_export_is_not_faked_as_verified(tmp_path):
    item = history.set_completion(tmp_path, 1000, True, title="수동 완료")
    assert item["completed"] and not item["exported"] and item["slug"] == ""
    assert history.by_problem(tmp_path)["1000"]["completed"]


def test_completion_api_and_batch_skip_use_user_decision(client, collection, tmp_path):
    assert client.put("/api/boj/1000/completion", json={"completed": True}).json()["completed"]
    result = server._execute_batch_item(1000, threading.Event(), lambda **kw: None, True)
    assert result["status"] == "skipped" and result["has_zip"] is False
    assert client.put("/api/boj/1000/completion", json={"completed": False}).json()["completed"] is False
    assert client.get("/api/boj").json()["items"][0]["history"]["completed"] is False
    assert client.put("/api/boj/9999/completion", json={"completed": True}).status_code == 404


def test_batch_download_exports_attached_multi_problem_layout(client, tmp_path):
    first, second = ready_workspace(tmp_path, 1000), ready_workspace(tmp_path, 1001)
    successful_export(first); successful_export(second)
    manager = server._batch_manager()
    manager._state = {"id": "completed", "status": "done", "items": [
        {"problem_id": 1000, "status": "exported", "slug": first.slug},
        {"problem_id": 1001, "status": "exported", "slug": second.slug},
    ]}
    response = client.get("/api/batches/completed/download")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        assert json.loads(z.read("2/problem.json"))["display_id"] == "1001"
    assert client.get("/api/batches/current").json()["bundle_count"] == 2


def test_metadata_preview_preserves_valid_zip_and_reuses_classification(tmp_path):
    from tcgen.engine.pipeline import Pipeline
    ws = ready_workspace(tmp_path)
    ws.solution_path.write_text("print(sum(map(int, input().split())))")
    path = successful_export(ws)
    before = path.read_bytes()
    class Executor:
        def run_on_bytes(self, *args, **kwargs):
            return RunResult(verdict=Verdict.OK)
        def run_script(self, path, stdin, *args, **kwargs):
            return RunResult(verdict=Verdict.OK, stdout=str(sum(map(int, stdin.read_bytes().split()))).encode())
    pipeline = Pipeline(ws, FakeCLI(default_responses()))
    pipeline.executor = Executor()
    first = pipeline.prepare_export_metadata()
    assert path.read_bytes() == before and history.get(ws)["exported"]
    restored = Pipeline(ws, FakeCLI({}))
    restored.executor = Executor()
    assert restored.prepare_export_metadata()["difficulty"] == first["difficulty"]
    assert path.read_bytes() == before


def test_title_change_invalidates_problem_zip_without_discarding_solution(client, tmp_path):
    ws = ready_workspace(tmp_path)
    path = successful_export(ws)
    response = client.put(f"/api/problems/{ws.slug}", json={"title": "새 제목"})
    assert response.status_code == 200 and response.json()["solution_confirmed"]
    assert not path.exists() and len(ws.load_cases_index()) == 3
    assert not history.get(ws)["exported"]


def test_assessment_cache_survives_other_workspaces_and_source_formatting(tmp_path):
    problem = make_problem()
    first = metadata.classify(problem, "print(1)", FakeCLI({"export-metadata": assessment()}), cache_dir=tmp_path)
    second = metadata.classify(problem.with_changes(slug="another"), "# comment\nprint ( 1 )\n", FakeCLI({}), cache_dir=tmp_path)
    assert first == second


def test_unindexed_case_cannot_be_exported_without_final_verification(tmp_path):
    ws = ready_workspace(tmp_path)
    info = export_metadata(ws)
    exporter.write_case(ws.cases_dir, 4, b"4 2\n", b"6", label="random")
    with pytest.raises(exporter.ExportError, match="검증 목록"):
        problem_export.export_problem(ws, info)


@pytest.mark.parametrize("scores, level", [
    ((0, 0, 0, 0), "VeryLow"), ((1, 1, 1, 1), "Low"), ((2, 2, 2, 1), "Mid"),
    ((3, 3, 2, 2), "High"), ((4, 4, 4, 4), "VeryHigh"),
])
def test_problem_zip_supports_all_code_place_difficulty_levels(tmp_path, scores, level):
    ws = ready_workspace(tmp_path)
    info = {**export_metadata(ws), **metadata.normalize_classification(assessment(scores))}
    result = problem_export.export_problem(ws, info)
    assert not result.has_errors
    with zipfile.ZipFile(result.zip_path) as archive:
        assert json.loads(archive.read("1/problem.json"))["difficulty"] == level


def test_old_three_level_cache_is_not_reused_for_five_level_rubric(tmp_path, monkeypatch):
    problem, source = make_problem(), "print(1)"
    with monkeypatch.context() as previous:
        previous.setattr(metadata, "RUBRIC_VERSION", "tcgen-difficulty-v1")
        old_key = metadata.classification_key(problem, source)
    old = {"classification_key": old_key, "rubric_version": "tcgen-difficulty-v1", "difficulty": "Low"}
    (tmp_path / f"{old_key}.json").write_text(json.dumps(old))
    result = metadata.classify(problem, source, FakeCLI({"export-metadata": assessment()}),
                               cached=old, cache_dir=tmp_path)
    assert result["classification_key"] != old_key
    assert result["rubric_version"] == "tcgen-difficulty-v2"
    assert result["difficulty"] == "VeryLow"
    assert [band["name"] for band in result["difficulty_scale"]] == ["VeryLow", "Low", "Mid", "High", "VeryHigh"]


@pytest.mark.parametrize("name", ["Very Low", "Very High", "Unknown"])
def test_problem_zip_rejects_noncanonical_difficulty_names(tmp_path, name):
    ws = ready_workspace(tmp_path)
    info = {**export_metadata(ws), "difficulty": name}
    with pytest.raises(exporter.ExportError, match="난이도"):
        problem_export.export_problem(ws, info)
