"""테스트 강도 검증 (PRD F2-D).

일부러 틀린 코드(뮤턴트)를 테스트케이스에 통과시켜 본다. 하나라도 살아남으면
그 테스트는 약한 것이고, 그 뮤턴트를 잡는 케이스를 추가로 만들어야 한다.

``wa``는 출력 불일치로, ``tle``·``mle``는 제한 초과로 잡힌다. Docker 격리가
없는 Windows에서 메모리는 폴링 측정이라 ``mle`` 판정은 참고치다 (F2-D6).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from tcgen import compare
from tcgen.engine.execution import Executor
from tcgen.models import Limits
from tcgen.runner.base import Verdict
from tcgen.workspace import Workspace

KIND_WA = "wa"
KIND_TLE = "tle"
KIND_MLE = "mle"

ProgressHook = Callable[[str], None]


@dataclass(frozen=True)
class MutantSpec:
    """LLM이 준 오답 코드 한 건."""

    name: str
    kind: str
    source: str
    flaw: str = ""


@dataclass(frozen=True)
class MutantResult:
    """오답 코드 한 건의 검증 결과."""

    name: str
    kind: str
    flaw: str
    killed: bool
    killed_by: int | None = None
    verdict: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "flaw": self.flaw,
            "killed": self.killed,
            "killed_by": self.killed_by,
            "verdict": self.verdict,
            "note": self.note,
        }


@dataclass(frozen=True)
class StrengthReport:
    """강도 검증 전체 결과."""

    results: tuple[MutantResult, ...]
    measurement: str = ""
    isolated: bool = False

    @property
    def survivors(self) -> tuple[MutantResult, ...]:
        return tuple(result for result in self.results if not result.killed)

    @property
    def kill_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for result in self.results if result.killed) / len(self.results)

    @property
    def all_killed(self) -> bool:
        return bool(self.results) and not self.survivors

    def to_dict(self) -> dict[str, object]:
        return {
            "results": [result.to_dict() for result in self.results],
            "kill_rate": round(self.kill_rate, 4),
            "survivor_count": len(self.survivors),
            "measurement": self.measurement,
            "isolated": self.isolated,
            "reliability_note": reliability_note(self.measurement, self.isolated),
        }


def reliability_note(measurement: str, isolated: bool) -> str:
    """측정 신뢰도를 리포트에 그대로 적는다 (PRD F2-D6, NF-S6)."""
    parts = []
    if not isolated:
        parts.append("격리 없이 로컬에서 실행했습니다")
    if measurement != "":
        parts.append(f"측정 방식: {measurement}")
    if measurement == "polling-lowres":
        parts.append("메모리·시간은 폴링 측정이라 MLE 판정은 참고치입니다")
    return ". ".join(parts)


def evaluate_mutant(
    executor: Executor,
    workspace: Workspace,
    mutant_path: Path,
    spec: MutantSpec,
    case_numbers: Sequence[int],
    limits: Limits,
) -> MutantResult:
    """케이스를 순서대로 먹여 뮤턴트가 잡히는 지점을 찾는다."""
    evaluated = 0
    for number in case_numbers:
        input_path = workspace.input_path(number)
        expected_path = workspace.output_path(number)
        if not input_path.exists() or not expected_path.exists():
            continue
        evaluated += 1

        result = executor.run_script(mutant_path, input_path, limits)
        if result.verdict is not Verdict.OK:
            return MutantResult(
                name=spec.name,
                kind=spec.kind,
                flaw=spec.flaw,
                killed=True,
                killed_by=number,
                verdict=result.verdict.value,
                note=result.note,
            )
        if not compare.outputs_match(expected_path.read_bytes(), result.stdout):
            return MutantResult(
                name=spec.name,
                kind=spec.kind,
                flaw=spec.flaw,
                killed=True,
                killed_by=number,
                verdict=Verdict.WA.value,
            )

    if evaluated == 0:
        # 한 건도 실제로 돌려보지 못한 것을 '생존'으로 보고하면 kill rate가
        # 거짓이 된다. 측정 실패는 측정 실패로 적는다.
        return MutantResult(
            name=spec.name,
            kind=spec.kind,
            flaw=spec.flaw,
            killed=False,
            verdict=Verdict.SKIPPED.value,
            note="테스트케이스 파일이 없어 검증하지 못했습니다.",
        )
    return MutantResult(
        name=spec.name,
        kind=spec.kind,
        flaw=spec.flaw,
        killed=False,
        verdict=Verdict.OK.value,
        note="모든 테스트케이스를 통과했습니다. 테스트가 약합니다.",
    )


def evaluate_all(
    executor: Executor,
    workspace: Workspace,
    specs: Sequence[MutantSpec],
    paths: Sequence[Path],
    case_numbers: Sequence[int],
    limits: Limits,
    *,
    measurement: str = "",
    isolated: bool = False,
    on_progress: ProgressHook | None = None,
) -> StrengthReport:
    """모든 뮤턴트를 검증해 kill rate를 낸다."""
    results: list[MutantResult] = []
    for spec, path in zip(specs, paths):
        if on_progress is not None:
            on_progress(spec.name)
        results.append(
            evaluate_mutant(executor, workspace, path, spec, case_numbers, limits)
        )
    return StrengthReport(
        results=tuple(results), measurement=measurement, isolated=isolated
    )


def specs_from_payload(payload: dict) -> tuple[MutantSpec, ...]:
    """LLM 응답에서 뮤턴트 명세를 꺼낸다."""
    raw = payload.get("mutants", []) or []
    specs: list[MutantSpec] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        if not isinstance(source, str) or not source.strip():
            continue
        kind = str(item.get("kind", KIND_WA)).lower()
        name = str(item.get("name") or f"mutant{index}")
        specs.append(
            MutantSpec(
                name=_safe_name(name, index),
                kind=kind if kind in {KIND_WA, KIND_TLE, KIND_MLE} else KIND_WA,
                source=source,
                flaw=str(item.get("flaw", "")),
            )
        )
    return tuple(specs)


def _safe_name(name: str, index: int) -> str:
    """파일명으로 쓸 수 있게 다듬는다."""
    cleaned = "".join(char if char.isalnum() or char in "_-" else "_" for char in name)
    return cleaned.strip("_") or f"mutant{index}"
