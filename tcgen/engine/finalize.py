"""입력·정답 출력을 재검사하고 브루트포스로 대조한다. 브루트포스 자원 초과는 미검증이다."""

from __future__ import annotations

import time
from typing import Callable

from tcgen import compare
from tcgen.engine.execution import Executor, failure_note
from tcgen.engine.generate import _reference_limits
from tcgen.engine.solution import validate_input
from tcgen.models import Limits
from tcgen.runner.base import Verdict
from tcgen.workspace import Workspace


#: 큰 입력에서 완전탐색이 재귀 깊이·메모리 한도에 걸린 경우. 시간 초과와 같은 자원 초과다.
_RESOURCE_ERRORS = ("RecursionError", "MemoryError", "StackOverflowError", "OutOfMemoryError")


def _out_of_resources(result) -> bool:
    if result.verdict in (Verdict.TLE, Verdict.MLE):
        return True
    return result.verdict is Verdict.RE and any(name in (result.stderr or "") for name in _RESOURCE_ERRORS)


def verify_cases(
    executor: Executor,
    workspace: Workspace,
    *,
    on_progress: Callable[[str], None] | None = None,
    budget_seconds: float = 30.0,
) -> dict:
    """전 케이스를 방문하되 대규모 완전탐색으로 작업 전체가 멈추지 않게 한다."""
    results = []
    problem = workspace.load_problem()
    reference_limits = _reference_limits(problem.limits)
    comparison = compare.comparison_for(problem)
    brute_seconds = 0.0
    for record in workspace.load_cases_index():
        if on_progress:
            on_progress(f"최종 대조: {record.index}번")
        item = {"index": record.index, "status": "failed", "note": "",
                "input_valid": False, "output_valid": False}
        results.append(item)
        input_path, output_path = workspace.input_path(record.index), workspace.output_path(record.index)
        if not input_path.is_file() or not output_path.is_file():
            item["note"] = "입력 또는 출력 파일이 없습니다."
            continue
        valid, note = validate_input(executor, workspace.validator_path, input_path.read_bytes())
        if not valid:
            item["note"] = f"입력 검증 실패: {note}"
            continue
        item["input_valid"] = True
        expected = output_path.read_bytes()
        reference = executor.run_script(workspace.solution_path, input_path, reference_limits)
        if reference.verdict is not Verdict.OK:
            item["note"] = f"정답 코드 재실행 실패: {failure_note(reference)}"
            continue
        if not compare.outputs_match(expected, reference.stdout):
            item["note"] = "저장된 기대 출력과 정답 코드 재실행 출력 불일치"
            continue
        item["output_valid"] = True

        # 입력과 정답 출력은 전부 검증한다. 시간 예산은 브루트포스에만 적용한다.
        remaining_seconds = budget_seconds - brute_seconds
        if remaining_seconds <= 0:
            item.update(status="skipped", note="최종 브루트포스 대조 시간 예산 소진")
            continue
        remaining_ms = max(1, int(remaining_seconds * 500))
        started = time.monotonic()
        result = executor.run_script(
            workspace.brute_path, input_path,
            Limits(time_ms=min(500, remaining_ms), memory_mb=512),
        )
        brute_seconds += time.monotonic() - started
        if _out_of_resources(result):
            item.update(status="skipped", note=f"브루트포스 자원 초과: {failure_note(result)}")
        elif result.verdict is not Verdict.OK:
            item["note"] = f"브루트포스 실행 실패: {failure_note(result)}"
        elif not compare.outputs_match(expected, result.stdout, comparison=comparison):
            item["note"] = "저장된 기대 출력과 브루트포스 출력 불일치"
        else:
            item["status"] = "matched"
    failed = sum(item["status"] == "failed" for item in results)
    matched = sum(item["status"] == "matched" for item in results)
    return {
        "passed": bool(results) and failed == 0,
        "fully_verified": bool(results) and matched == len(results),
        "matched": matched, "failed": failed, "skipped": len(results) - matched - failed,
        "total": len(results),
        "inputs_validated": sum(item["input_valid"] for item in results),
        "outputs_verified": sum(item["output_valid"] for item in results),
        "results": results, "output_comparison": comparison.to_dict(),
    }
