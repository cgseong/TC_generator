"""실수 허용 오차가 예제·스트레스·강도·최종 대조에 일관되게 적용되는지 확인한다."""
from pathlib import Path

from fixtures import make_problem
from tcgen import compare, exporter
from tcgen.engine.finalize import verify_cases
from tcgen.engine.solution import check_examples, run_stress
from tcgen.engine.strength import MutantSpec, evaluate_mutant
from tcgen.engine.metadata import original_samples
from tcgen.models import Example
from tcgen.runner.base import RunResult, Verdict
from tcgen.workspace import create_workspace

EPS = "절대/상대 오차는 10^-6까지 허용한다."
LONG = b"13.341664064126334"
SHORT = b"13.341664064126"


class FloatExecutor:
    def run_on_bytes(self, path, *args, **kwargs):
        return RunResult(verdict=Verdict.OK, stdout=SHORT if path.name == "sol.py" else LONG)

    def run_script(self, path, *args, **kwargs):
        if path.stem == "stress_gen":
            return RunResult(verdict=Verdict.OK, stdout=(str(kwargs.get("args", (1,))[0]) + "\n").encode())
        return RunResult(verdict=Verdict.OK, stdout=SHORT if path.name == "sol.py" else LONG)


def float_problem():
    return make_problem(output_spec=EPS, examples=(Example("1\n", LONG.decode()),), solution_confirmed=True)


def test_example_precision_difference_passes_without_changing_expected_text():
    problem = float_problem()
    result = check_examples(FloatExecutor(), Path("sol.py"), problem)
    assert result.passed and result.counterexample is None
    assert problem.examples[0].output == LONG.decode()
    assert not check_examples(FloatExecutor(), Path("sol.py"), problem.with_changes(output_spec="정답을 출력한다.")).passed


def test_stress_comparison_obeys_problem_policy():
    problem = float_problem()
    args = (FloatExecutor(), Path("sol.py"), Path("brute.py"), Path("stress_gen.py"))
    assert run_stress(*args, rounds=2, limits=problem.limits,
                      comparison=compare.comparison_for(problem)).passed
    assert not run_stress(*args, rounds=2, limits=problem.limits).passed


def test_strength_does_not_kill_a_program_for_valid_decimal_formatting(tmp_path):
    ws = create_workspace(float_problem(), base_dir=tmp_path)
    exporter.write_case(ws.cases_dir, 1, b"1\n", SHORT, label="edge")
    spec = MutantSpec(name="same_value", kind="wa", source="# same value")
    result = evaluate_mutant(FloatExecutor(), ws, Path("mutant.py"), spec, [1], ws.load_problem().limits)
    assert not result.killed


def test_final_brute_comparison_allows_precision_but_saved_output_remains_exact(tmp_path):
    ws = create_workspace(float_problem(), base_dir=tmp_path)
    record = exporter.write_case(ws.cases_dir, 1, b"1\n", SHORT, label="edge")
    ws.save_cases_index([record])
    result = verify_cases(FloatExecutor(), ws)
    assert result["fully_verified"] and result["output_comparison"]["mode"] == "float"
    assert ws.output_path(1).read_bytes() == SHORT
    ws.output_path(1).write_bytes(LONG)
    result = verify_cases(FloatExecutor(), ws)
    assert not result["passed"] and "재실행 출력 불일치" in result["results"][0]["note"]


def test_public_samples_preserve_original_float_output_without_reformatting(tmp_path):
    ws = create_workspace(float_problem(), base_dir=tmp_path)
    result = original_samples(ws.load_problem())
    assert result["samples"] == [{"input": "1\n", "output": LONG.decode()}]
