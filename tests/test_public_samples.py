"""공개 예제는 문제의 원문 입출력만 사용하고 순서·개수·표기를 보존한다."""
import json
import zipfile

import pytest

from fixtures import FakeCLI, default_responses, make_problem
from test_server import client
from tcgen import exporter, problem_export
from tcgen.engine import metadata
from tcgen.engine.pipeline import Pipeline
from tcgen.models import Example
from tcgen.workspace import create_workspace


def workspace(root, *, examples=None):
    changes = {} if examples is None else {"examples": examples}
    ws = create_workspace(make_problem(solution_confirmed=True, **changes), base_dir=root)
    rows = [("example", b"1 2\n", b"3"), ("edge", b"0 0\n", b"0"),
            ("anti", b"0 1\n", b"1"), ("scale", b"1000 1000\n", b"2000"),
            ("random", b"9 3\n", b"12")]
    ws.save_cases_index([exporter.write_case(ws.cases_dir, i, data, out, label=label)
                        for i, (label, data, out) in enumerate(rows, 1)])
    return ws


def test_only_original_samples_are_used_without_modifying_testcases(tmp_path):
    ws = workspace(tmp_path, examples=(Example("5 7\n", "12\n"), Example("1 2\n", "3\n")))
    before = {path.name: path.read_bytes() for path in ws.cases_dir.iterdir()}
    result = metadata.original_samples(ws.load_problem())
    assert result["samples"] == [{"input": "5 7\n", "output": "12\n"},
                                 {"input": "1 2\n", "output": "3\n"}]
    assert {path.name: path.read_bytes() for path in ws.cases_dir.iterdir()} == before
    assert all(source["purpose"] == "original" for source in result["sample_sources"])


def test_original_samples_keep_whitespace_duplicate_entries_and_order():
    examples = (Example(" 1  2\n\n", "3.000000\n\n"), Example("1 2\n", "3\n"),
                Example("1 2\n", "3\n"))
    problem = make_problem(examples=examples)
    assert metadata.original_samples(problem)["samples"] == [example.to_dict() for example in examples]


def test_long_original_inputs_and_outputs_are_not_shortened():
    example = Example("123 " * 300 + "\n" * 20, "정답 " * 200 + "\n" * 20)
    assert metadata.original_samples(make_problem(examples=(example,)))["samples"] == [example.to_dict()]


def test_only_line_endings_are_normalized():
    result = metadata.original_samples(make_problem(examples=(Example(" 1\r\n2\r", "3\r\n\r\n"),)))
    assert result["samples"] == [{"input": " 1\n2\n", "output": "3\n\n"}]


def test_all_original_examples_are_preserved_in_zip_even_when_over_four(tmp_path):
    examples = tuple(Example(f"{i} 2\n", f"{i + 2}\n") for i in range(1, 8))
    ws = workspace(tmp_path, examples=examples)
    info = {**metadata.normalize_classification(default_responses()["export-metadata"]),
            **metadata.original_samples(ws.load_problem())}
    result = problem_export.export_problem(ws, info)
    assert not result.has_errors
    assert problem_export.verify_problem_zip(result.zip_path) == ()
    with zipfile.ZipFile(result.zip_path) as archive:
        payload = json.loads(archive.read("1/problem.json"))
    assert payload["samples"] == [example.to_dict() for example in examples]


@pytest.mark.parametrize("examples", [(), (Example("", "3"),), (Example("1 2", ""),)])
def test_missing_original_data_blocks_export_instead_of_filling_from_tests(tmp_path, examples):
    ws = workspace(tmp_path, examples=examples)
    with pytest.raises(metadata.MetadataError, match="원문 예제"):
        metadata.original_samples(ws.load_problem())


def test_old_generated_sample_cache_is_ignored(tmp_path):
    ws = workspace(tmp_path)
    ws.solution_path.write_text("print(sum(map(int,input().split())))")
    problem = ws.load_problem()
    cached = metadata.classify(problem, ws.solution_path.read_text(), FakeCLI(default_responses()))
    cached.update(samples=[{"input": "1000 1000\n", "output": "2000"}],
                  sample_sources=[{"purpose": "general"}], sample_note="old generated sample")
    cli = FakeCLI({})
    pipeline = Pipeline(ws, cli)
    pipeline._state["export_metadata"] = cached
    result = pipeline.prepare_export_metadata(final_checked=True)
    assert result["samples"] == [{"input": "1 2\n", "output": "3\n"}]
    assert result["sample_sources"] == [{"origin": "problem.examples#1", "example_index": 1, "purpose": "original"}]
    assert "export-metadata" not in cli.calls


def test_saving_a_problem_preserves_large_original_example_sets(client, tmp_path):
    ws = workspace(tmp_path)
    examples = [{"input": f"{i} 2\n", "output": f"{i+2}\n"} for i in range(63)]
    response = client.put(f"/api/problems/{ws.slug}", json={"examples": examples})
    assert response.status_code == 200
    assert [example.to_dict() for example in ws.load_problem().examples] == examples
