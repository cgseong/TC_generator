"""정답 코드 확정 (PRD F2-B).

브루트포스 대조를 통과하기 전까지 정답 코드는 '후보'다. 이 모듈이 그 판정을
내린다. 같은 LLM이 쓴 두 코드가 같은 오해를 공유할 수 있으므로, 대조 통과는
'지문 해석이 옳다'가 아니라 '두 구현이 일치한다'는 뜻임에 유의한다. 해석
자체는 사람이 확인한다(F1-4).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tcgen import compare
from tcgen.config import MIN_STRESS_ROUNDS
from tcgen.engine.execution import Executor, failure_note
from tcgen.models import Limits, Problem
from tcgen.runner.base import Verdict

#: 브루트포스와 생성기는 작은 입력만 다루므로 넉넉한 제한을 준다.
HELPER_LIMITS = Limits(time_ms=10_000, memory_mb=1024)

ProgressHook = Callable[[int, int], None]


@dataclass(frozen=True)
class Counterexample:
    """정답 후보와 브루트포스가 갈린 입력."""

    input_text: str
    solution_output: str
    brute_output: str
    seed: int | None = None
    reason: str = "출력 불일치"

    def to_dict(self) -> dict[str, object]:
        return {
            "input": self.input_text,
            "solution_output": self.solution_output,
            "brute_output": self.brute_output,
            "seed": self.seed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StressOutcome:
    """스트레스 대조 결과."""

    passed: bool
    rounds_run: int
    counterexample: Counterexample | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "rounds_run": self.rounds_run,
            "counterexample": self.counterexample.to_dict() if self.counterexample else None,
            "note": self.note,
        }


@dataclass(frozen=True)
class ExampleCheck:
    """지문 예제 검증 결과."""

    passed: bool
    failures: tuple[str, ...] = ()
    counterexample: Counterexample | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
            "counterexample": self.counterexample.to_dict() if self.counterexample else None,
        }


def check_examples(
    executor: Executor, solution_path: Path, problem: Problem
) -> ExampleCheck:
    """지문 예제를 통과하는지 본다. 가장 싼 1차 관문이다 (PRD F1-6)."""
    failures: list[str] = []
    first_counterexample: Counterexample | None = None
    for index, example in enumerate(problem.examples, start=1):
        result = executor.run_on_bytes(
            solution_path, example.input.encode("utf-8"), problem.limits
        )
        actual = result.stdout.decode("utf-8", errors="replace").strip()
        if result.verdict is not Verdict.OK:
            failures.append(f"예제 {index}: {failure_note(result)}")
            first_counterexample = first_counterexample or Counterexample(
                input_text=example.input,
                solution_output=result.stderr.strip() or actual,
                brute_output=example.output,
                reason=f"예제 {index} 실행 실패: {failure_note(result)}",
            )
            continue
        if not compare.outputs_match(example.output.encode("utf-8"), result.stdout):
            failures.append(f"예제 {index}: 기대 '{example.output.strip()}' / 실제 '{actual}'")
            first_counterexample = first_counterexample or Counterexample(
                input_text=example.input,
                solution_output=actual,
                brute_output=example.output,
                reason=f"예제 {index} 출력 불일치",
            )
    return ExampleCheck(
        passed=not failures,
        failures=tuple(failures),
        counterexample=first_counterexample,
    )


def run_stress(
    executor: Executor,
    solution_path: Path,
    brute_path: Path,
    generator_path: Path,
    *,
    rounds: int,
    limits: Limits,
    validator_path: Path | None = None,
    on_progress: ProgressHook | None = None,
) -> StressOutcome:
    """작은 랜덤 입력으로 정답 후보와 브루트포스를 대조한다 (PRD F2-B4)."""
    if rounds < MIN_STRESS_ROUNDS:
        # 0회 대조를 '통과'로 돌려주면 정답 확정 게이트가 통째로 무력화된다.
        return StressOutcome(
            passed=False,
            rounds_run=0,
            note=f"대조 횟수가 {rounds}회로 설정되어 검증할 수 없습니다. 1회 이상이어야 합니다.",
        )
    for seed in range(1, rounds + 1):
        if on_progress is not None:
            on_progress(seed, rounds)

        generated = executor.run_script(
            generator_path, None, HELPER_LIMITS, args=(str(seed),)
        )
        if generated.verdict is not Verdict.OK:
            return StressOutcome(
                passed=False,
                rounds_run=seed,
                note=f"생성기 실행 실패(seed={seed}): {failure_note(generated)}",
            )

        data = generated.stdout.replace(compare.CRLF, compare.LF)
        if validator_path is not None:
            valid, message = validate_input(executor, validator_path, data)
            if not valid:
                return StressOutcome(
                    passed=False,
                    rounds_run=seed,
                    note=f"생성기가 제약을 위반한 입력을 만들었습니다(seed={seed}): {message}",
                )

        outcome = _compare_once(executor, solution_path, brute_path, data, limits, seed)
        if outcome is not None:
            return StressOutcome(passed=False, rounds_run=seed, counterexample=outcome)

    return StressOutcome(passed=True, rounds_run=rounds, note=f"{rounds}회 대조 통과")


def _compare_once(
    executor: Executor,
    solution_path: Path,
    brute_path: Path,
    data: bytes,
    limits: Limits,
    seed: int,
) -> Counterexample | None:
    solution_result = executor.run_on_bytes(solution_path, data, limits)
    brute_result = executor.run_on_bytes(brute_path, data, HELPER_LIMITS)
    input_text = data.decode("utf-8", errors="replace")

    if brute_result.verdict is not Verdict.OK:
        return Counterexample(
            input_text=input_text,
            solution_output=solution_result.stdout.decode("utf-8", errors="replace"),
            brute_output=brute_result.stderr.strip(),
            seed=seed,
            reason=f"브루트포스 실행 실패: {failure_note(brute_result)}",
        )
    if solution_result.verdict is not Verdict.OK:
        return Counterexample(
            input_text=input_text,
            solution_output=solution_result.stderr.strip(),
            brute_output=brute_result.stdout.decode("utf-8", errors="replace"),
            seed=seed,
            reason=f"정답 후보 실행 실패: {failure_note(solution_result)}",
        )
    if not compare.outputs_match(brute_result.stdout, solution_result.stdout):
        return Counterexample(
            input_text=input_text,
            solution_output=solution_result.stdout.decode("utf-8", errors="replace"),
            brute_output=brute_result.stdout.decode("utf-8", errors="replace"),
            seed=seed,
        )
    return None


def validate_input(
    executor: Executor, validator_path: Path, data: bytes
) -> tuple[bool, str]:
    """입력이 제약을 지키는지 검사한다 (PRD F2-C4)."""
    result = executor.run_on_bytes(validator_path, data, HELPER_LIMITS)
    if result.verdict is Verdict.OK:
        return True, ""
    message = result.stderr.strip().splitlines()
    return False, message[-1] if message else failure_note(result)


def check_determinism(
    executor: Executor, solution_path: Path, data: bytes, limits: Limits
) -> bool:
    """같은 입력을 두 번 돌려 출력이 같은지 본다 (PRD F2-B7)."""
    first = executor.run_on_bytes(solution_path, data, limits)
    second = executor.run_on_bytes(solution_path, data, limits)
    if first.verdict is not Verdict.OK or second.verdict is not Verdict.OK:
        return False
    return compare.outputs_match(first.stdout, second.stdout)
