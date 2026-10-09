"""엔진 통합 테스트.

가짜 LLM을 쓰지만 실행·비교·패키징은 전부 진짜로 돌린다.
PRD 수용 기준 AC-2~AC-7을 덮는다.
"""

import zipfile

import pytest

from fixtures import (
    FakeCLI,
    STRESS_ONLY_WRONG_SOLUTION,
    WRONG_SOLUTION,
    default_responses,
    generators_payload,
    make_problem,
    mutants_payload,
    slow_mutants_payload,
)
from tcgen import exporter
from tcgen.engine.pipeline import Pipeline, PipelineError
from tcgen.workspace import create_workspace

pytestmark = pytest.mark.integration


def build_pipeline(tmp_path, responses=None, **problem_overrides) -> Pipeline:
    workspace = create_workspace(make_problem(**problem_overrides), base_dir=tmp_path)
    return Pipeline(workspace, FakeCLI(responses or default_responses()))


class TestSolutionConfirmation:
    def test_correct_solution_is_confirmed(self, tmp_path):
        pipeline = build_pipeline(tmp_path)
        outcome = pipeline.build_solution()

        assert outcome.confirmed
        assert outcome.examples.passed
        assert outcome.stress.passed
        assert pipeline.problem.solution_confirmed

    def test_example_gate_catches_obvious_error(self, tmp_path):
        # 예제조차 통과하지 못하는 코드는 가장 싼 관문에서 걸린다.
        responses = default_responses(solution=WRONG_SOLUTION, fix=WRONG_SOLUTION)
        pipeline = build_pipeline(tmp_path, responses)
        outcome = pipeline.build_solution(max_attempts=2)

        assert not outcome.confirmed
        assert not outcome.examples.passed
        assert outcome.examples.counterexample is not None

    def test_stress_catches_error_that_examples_miss(self, tmp_path):
        # AC-3: 예제는 통과하지만 틀린 코드는 브루트포스 대조가 잡아낸다.
        responses = default_responses(
            solution=STRESS_ONLY_WRONG_SOLUTION, fix=STRESS_ONLY_WRONG_SOLUTION
        )
        pipeline = build_pipeline(tmp_path, responses)
        outcome = pipeline.build_solution(max_attempts=2)

        assert outcome.examples.passed
        assert not outcome.confirmed
        assert outcome.stress.counterexample is not None
        assert not pipeline.problem.solution_confirmed

    def test_counterexample_feedback_fixes_the_solution(self, tmp_path):
        from fixtures import SOLUTION

        responses = default_responses(solution=STRESS_ONLY_WRONG_SOLUTION, fix=SOLUTION)
        pipeline = build_pipeline(tmp_path, responses)
        outcome = pipeline.build_solution()

        assert outcome.confirmed
        assert outcome.attempts >= 2  # 처음엔 실패하고 수정 후 통과

    def test_author_provided_solution_is_recorded(self, tmp_path):
        from fixtures import SOLUTION

        pipeline = build_pipeline(tmp_path)
        pipeline.build_solution(author_source=SOLUTION)
        assert pipeline.problem.solution_source == "author"


class TestExportGate:
    def test_export_blocked_without_confirmation(self, tmp_path):
        # AC-2: 확정되지 않은 정답 코드로는 내보낼 수 없다.
        pipeline = build_pipeline(tmp_path)
        with pytest.raises(PipelineError, match="확정되지 않아"):
            pipeline.export()

    def test_cases_blocked_without_confirmation(self, tmp_path):
        pipeline = build_pipeline(tmp_path)
        with pytest.raises(PipelineError, match="확정되지 않아"):
            pipeline.build_cases()


class TestCaseGeneration:
    def test_cases_are_generated_and_indexed(self, tmp_path):
        pipeline = build_pipeline(tmp_path)
        pipeline.build_solution()
        outcome = pipeline.build_cases()

        assert len(outcome.records) >= 5
        numbers = [record.index for record in outcome.records]
        assert numbers == list(range(1, len(numbers) + 1))
        assert pipeline.workspace.load_cases_index() == outcome.records

    def test_example_is_first_case(self, tmp_path):
        pipeline = build_pipeline(tmp_path)
        pipeline.build_solution()
        pipeline.build_cases()

        assert pipeline.workspace.input_path(1).read_bytes() == b"1 2\n"
        assert pipeline.workspace.output_path(1).read_bytes() == b"3"

    def test_outputs_are_lf_only(self, tmp_path):
        # Windows에서 찍힌 CRLF가 그대로 저장되면 채점 해시가 어긋난다.
        pipeline = build_pipeline(tmp_path)
        pipeline.build_solution()
        outcome = pipeline.build_cases()

        for record in outcome.records:
            assert b"\r\n" not in pipeline.workspace.output_path(record.index).read_bytes()

    def test_constraint_violating_input_is_rejected(self, tmp_path):
        # AC-4: validator를 통과하지 못한 입력은 채택되지 않는다.
        responses = default_responses(generators=generators_payload(invalid=True))
        pipeline = build_pipeline(tmp_path, responses)
        pipeline.build_solution()
        outcome = pipeline.build_cases()

        assert any("제약 위반" in rejection.reason for rejection in outcome.rejections)

    def test_duplicate_inputs_are_rejected(self, tmp_path):
        pipeline = build_pipeline(tmp_path)
        pipeline.build_solution()
        outcome = pipeline.build_cases()

        for record in outcome.records:
            assert pipeline.workspace.input_path(record.index).exists()


class TestStrength:
    def test_logic_mutants_are_killed(self, tmp_path):
        # AC-5: 오답 뮤턴트가 잡히고, 잡은 케이스 번호가 남는다.
        pipeline = build_pipeline(tmp_path)
        pipeline.build_solution()
        pipeline.build_cases()
        report = pipeline.verify_strength()

        assert report.all_killed
        assert report.kill_rate == 1.0
        assert all(result.killed_by is not None for result in report.results)

    @pytest.mark.slow
    def test_tle_and_mle_mutants_are_killed(self, tmp_path):
        responses = default_responses(mutants=slow_mutants_payload())
        pipeline = build_pipeline(tmp_path, responses)
        pipeline.build_solution()
        pipeline.build_cases()
        report = pipeline.verify_strength()

        verdicts = {result.name: result.verdict for result in report.results}
        assert verdicts["slow"] == "TLE"
        assert verdicts["hungry"] == "MLE"

    def test_surviving_mutant_triggers_reinforcement(self, tmp_path):
        # AC-6: 살아남은 오답이 있으면 추가 케이스를 만든다.
        responses = default_responses(
            mutants=mutants_payload(sneaky=True),
            edge={"cases": [{"label": "작은 값", "input": "1 1\n"}]},
            generators={"generators": []},
        )
        pipeline = build_pipeline(tmp_path, responses)
        pipeline.build_solution()
        before = len(pipeline.build_cases().records)
        report = pipeline.verify_strength(reinforce_rounds=1)

        assert len(pipeline.workspace.load_cases_index()) > before
        assert report.all_killed

    def test_report_records_measurement_reliability(self, tmp_path):
        pipeline = build_pipeline(tmp_path)
        pipeline.build_solution()
        pipeline.build_cases()
        report = pipeline.verify_strength()

        assert "격리 없이" in report.to_dict()["reliability_note"]


class TestExport:
    def test_full_run_produces_valid_zip(self, tmp_path):
        # AC-7: zip 구조와 자체 검증.
        pipeline = build_pipeline(tmp_path)
        pipeline.build_solution()
        pipeline.build_cases()
        result = pipeline.export()

        assert not result.has_errors
        assert exporter.verify_zip(result.zip_path) == ()

        with zipfile.ZipFile(result.zip_path) as archive:
            names = archive.namelist()
        assert "info" in names
        assert "info.json" not in names
        assert all("/" not in name for name in names)
        assert sorted(name for name in names if name.endswith(".in")) == sorted(
            f"{index}.in" for index in range(1, result.case_count + 1)
        )

    def test_report_is_written(self, tmp_path):
        pipeline = build_pipeline(tmp_path)
        pipeline.build_solution()
        pipeline.build_cases()
        pipeline.verify_strength()
        pipeline.export()
        path = pipeline.write_report()

        content = path.read_text(encoding="utf-8")
        assert "kill rate" in content
        assert "실행 환경과 측정 신뢰도" in content
        assert (pipeline.workspace.reports_dir / "run.json").exists()


class TestRunAll:
    def test_full_automatic_run(self, tmp_path):
        pipeline = build_pipeline(tmp_path)
        payload = pipeline.run_all()

        assert payload["problem"]["solution_confirmed"] is True
        assert payload["export"]["has_errors"] is False
        assert payload["strength"]["kill_rate"] == 1.0

    def test_run_all_stops_when_solution_unconfirmed(self, tmp_path):
        responses = default_responses(solution=WRONG_SOLUTION, fix=WRONG_SOLUTION)
        pipeline = build_pipeline(tmp_path, responses)
        with pytest.raises(PipelineError):
            pipeline.run_all()
        assert (pipeline.workspace.reports_dir / "report.md").exists()


class TestEvents:
    def test_progress_events_are_emitted(self, tmp_path):
        pipeline = build_pipeline(tmp_path)
        pipeline.build_solution()

        stages = {event.stage for event in pipeline.bus.history()}
        assert "solution" in stages
        assert any("대조" in event.message for event in pipeline.bus.history())
