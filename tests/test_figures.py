"""그림이 포함된 지문의 PDF 파싱 경로.

그림은 텍스트 붙여넣기로는 사라진다. PDF를 그대로 넘겨 CLI가 ``Read``로 읽게
하고, 그림이 담고 있던 정보를 전사해 이후 모든 프롬프트에 싣는다.
"""

import json
import subprocess

import pytest
from fixtures import FakeCLI, default_responses, make_problem

from tcgen.engine import prompts
from tcgen.engine.pipeline import Pipeline
from tcgen.llm import ClaudeCLI, LLMError
from tcgen.models import (
    FIGURE_ROLE_DECORATIVE,
    FIGURE_ROLE_SPEC,
    Figure,
    Problem,
)
from tcgen.workspace import create_workspace

PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\ntrailer\n"


def write_pdf(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PDF_BYTES)
    return path


def pdf_payload(**overrides):
    payload = {
        "title": "창고 적재",
        "statement": "격자를 따라 이동하며 높이를 더한다. [그림1] 참조.",
        "input_spec": "첫 줄에 H와 W가 주어진다.",
        "output_spec": "최댓값을 출력한다.",
        "constraints": "1 <= H, W <= 1000",
        "hints": "",
        "time_ms": 1000,
        "memory_mb": 256,
        "examples": [{"input": "3 4\n3 1 4 1\n5 9 2 6\n5 3 5 8\n", "output": "33\n"}],
        "figures": [
            {
                "ref": "[그림1]",
                "role": "spec",
                "description": "좌상단이 (1,1)이고 r은 아래로, c는 오른쪽으로 증가한다.",
            }
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------- 모델


class TestFigureModel:
    def test_round_trips_through_problem_json(self):
        figure = Figure(ref="[그림1]", role=FIGURE_ROLE_SPEC, description="좌상단이 (1,1)")
        problem = make_problem(figures=(figure,), source_pdf="statement.pdf")

        restored = Problem.from_dict(json.loads(json.dumps(problem.to_dict())))

        assert restored.figures == (figure,)
        assert restored.source_pdf == "statement.pdf"

    def test_unknown_role_falls_back_to_decorative(self):
        # 모르는 role을 그대로 믿으면 critical 판정이 조용히 빗나간다.
        figure = Figure.from_dict({"ref": "[그림1]", "role": "엉뚱한값"})
        assert figure.role == FIGURE_ROLE_DECORATIVE

    def test_spec_figure_without_description_is_unresolved(self):
        assert Figure(ref="[그림1]", role=FIGURE_ROLE_SPEC).is_unresolved
        assert not Figure(ref="[그림1]", role=FIGURE_ROLE_SPEC, description="설명").is_unresolved

    def test_decorative_figure_is_never_unresolved(self):
        assert not Figure(ref="[그림1]", role=FIGURE_ROLE_DECORATIVE).is_unresolved

    def test_problem_reports_unresolved_figures(self):
        problem = make_problem(
            figures=(
                Figure(ref="[그림1]", role=FIGURE_ROLE_DECORATIVE),
                Figure(ref="[그림2]", role=FIGURE_ROLE_SPEC),
            )
        )
        assert [figure.ref for figure in problem.unresolved_figures] == ["[그림2]"]


# ------------------------------------------------------------------ 프롬프트


class TestFigurePrompts:
    def test_problem_block_carries_transcription(self):
        problem = make_problem(
            figures=(Figure(ref="[그림1]", role=FIGURE_ROLE_SPEC, description="좌상단이 (1,1)"),)
        )
        block = prompts.solution(problem)

        assert "[그림1]" in block
        assert "좌상단이 (1,1)" in block

    def test_missing_transcription_is_stated_not_hidden(self):
        # 그림의 존재를 숨기면 LLM이 완전한 지문으로 착각하고 빈칸을 상상한다.
        problem = make_problem(figures=(Figure(ref="[그림1]", role=FIGURE_ROLE_SPEC),))
        block = prompts.solution(problem)

        assert "[그림1]" in block
        assert "전사 없음" in block

    def test_problem_without_figures_says_so(self):
        assert "[그림 없음]" in prompts.solution(make_problem())

    def test_pdf_prompt_names_the_file_and_demands_figures(self, tmp_path):
        pdf = write_pdf(tmp_path / "assets" / "statement.pdf")
        text = prompts.parse_pdf(pdf)

        assert str(pdf) in text
        assert "figures" in text
        assert "Read" in text


# ----------------------------------------------------------------- CLI 인자


def fake_completed(stdout: str):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def cli_result(payload) -> str:
    return json.dumps({"type": "result", "is_error": False, "result": json.dumps(payload)})


class RecordingRunner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, prompt, timeout_s):
        self.calls.append({"argv": list(argv), "prompt": prompt, "timeout": timeout_s})
        return self.responses.pop(0)


class TestReadScopedToFile:
    def test_read_is_allowed_only_for_the_given_directory(self, tmp_path):
        pdf = write_pdf(tmp_path / "assets" / "statement.pdf")
        runner = RecordingRunner(fake_completed(cli_result({"title": "제목"})))

        ClaudeCLI(runner=runner).ask_json_about_files("프롬프트", [pdf], tag="parse-pdf")

        argv = runner.calls[0]["argv"]
        assert argv[argv.index("--allowed-tools") + 1] == "Read"
        assert argv[argv.index("--add-dir") + 1] == str(pdf.parent.resolve())
        disallowed = argv[argv.index("--disallowed-tools") + 1]
        assert "Read" not in disallowed
        # 읽기만 푼다. 실행·쓰기·네트워크는 그대로 막혀 있어야 한다.
        for tool in ("Write", "Edit", "WebFetch", "WebSearch", "Task"):
            assert tool in disallowed

    def test_ordinary_calls_still_block_read(self, tmp_path):
        runner = RecordingRunner(fake_completed(cli_result({"ok": 1})))
        ClaudeCLI(runner=runner).ask_json("프롬프트")

        argv = runner.calls[0]["argv"]
        assert "Read" in argv[argv.index("--disallowed-tools") + 1]
        assert "--allowed-tools" not in argv
        assert "--add-dir" not in argv

    def test_missing_file_is_refused_before_launching_cli(self, tmp_path):
        runner = RecordingRunner()
        with pytest.raises(LLMError, match="찾을 수 없"):
            ClaudeCLI(runner=runner).ask_json_about_files("프롬프트", [tmp_path / "없다.pdf"])
        assert runner.calls == []


# ------------------------------------------------------------------ 파이프라인


class TestParsePdf:
    def build(self, tmp_path, payload=None):
        workspace = create_workspace(make_problem(), base_dir=tmp_path)
        responses = default_responses(**{"parse-pdf": payload or pdf_payload()})
        return Pipeline(workspace, FakeCLI(responses))

    def test_fills_problem_and_figures(self, tmp_path):
        pipeline = self.build(tmp_path)
        write_pdf(pipeline.workspace.source_pdf_path)

        problem = pipeline.parse_pdf()

        assert problem.title == "창고 적재"
        assert problem.limits.time_ms == 1000
        assert problem.examples[0].output == "33\n"
        assert len(problem.figures) == 1
        assert problem.figures[0].role == FIGURE_ROLE_SPEC
        assert "좌상단" in problem.figures[0].description
        assert problem.source_pdf == pipeline.workspace.source_pdf_path.name

    def test_resets_earlier_confirmations(self, tmp_path):
        pipeline = self.build(tmp_path)
        write_pdf(pipeline.workspace.source_pdf_path)
        pipeline.workspace.save_problem(
            pipeline.problem.with_changes(
                solution_confirmed=True, interpretation_confirmed=True
            )
        )

        problem = pipeline.parse_pdf()

        assert not problem.solution_confirmed
        assert not problem.interpretation_confirmed

    def test_warns_when_a_critical_figure_has_no_transcription(self, tmp_path):
        payload = pdf_payload(figures=[{"ref": "[그림1]", "role": "spec", "description": ""}])
        pipeline = self.build(tmp_path, payload)
        write_pdf(pipeline.workspace.source_pdf_path)
        pipeline.parse_pdf()

        messages = [event.message for event in pipeline.bus.history()]
        assert any("전사" in message for message in messages)

    def test_requires_the_pdf_to_exist(self, tmp_path):
        pipeline = self.build(tmp_path)
        with pytest.raises(Exception, match="PDF"):
            pipeline.parse_pdf()
