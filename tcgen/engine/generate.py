"""테스트케이스 생성 (PRD F2-C).

하이브리드 전략이다.

- 경계·특수 케이스는 LLM이 **입력 데이터를 직접** 준다.
- 랜덤·대규모·저격 케이스는 LLM이 쓴 **생성기 스크립트를 앱이 시드와 함께 실행**한다.

어느 쪽이든 validator를 통과하지 못한 입력은 채택하지 않는다. 기대 출력은
확정된 정답 코드를 **실행해서** 얻는다. 추측하지 않는다 (F2-C5).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from tcgen import compare, exporter
from tcgen.engine.execution import Executor, failure_note
from tcgen.engine.solution import HELPER_LIMITS, validate_input
from tcgen.models import CaseRecord, Limits
from tcgen.runner.base import Verdict
from tcgen.workspace import Workspace

ProgressHook = Callable[[str], None]

#: 기준 풀이 실행에 주는 여유 배수
REFERENCE_TIME_FACTOR = 5
REFERENCE_MEMORY_FACTOR = 2


@dataclass(frozen=True)
class Candidate:
    """채택 심사를 기다리는 입력 하나."""

    data: bytes
    label: str
    origin: str = ""
    seed: int | None = None


@dataclass(frozen=True)
class Rejection:
    """채택되지 않은 입력과 그 이유."""

    origin: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"origin": self.origin, "reason": self.reason}


@dataclass(frozen=True)
class GenerationOutcome:
    """생성 결과."""

    records: tuple[CaseRecord, ...]
    rejections: tuple[Rejection, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "records": [record.to_dict() for record in self.records],
            "rejections": [rejection.to_dict() for rejection in self.rejections],
            "case_count": len(self.records),
        }


def candidates_from_examples(examples: Iterable) -> tuple[Candidate, ...]:
    """지문 예제를 1번부터 넣는다. 가장 신뢰도가 높은 케이스다."""
    return tuple(
        Candidate(
            data=example.input.encode("utf-8"),
            label="example",
            origin=f"example#{index}",
        )
        for index, example in enumerate(examples, start=1)
    )


def candidates_from_payload(
    payload: dict, *, label: str, origin_prefix: str
) -> tuple[Candidate, ...]:
    """LLM이 직접 준 입력 목록을 후보로 바꾼다."""
    raw_cases = payload.get("cases", []) or []
    candidates: list[Candidate] = []
    for index, item in enumerate(raw_cases, start=1):
        if not isinstance(item, dict):
            continue
        text = item.get("input")
        if not isinstance(text, str) or not text.strip():
            continue
        note = str(item.get("label", "")).strip()
        candidates.append(
            Candidate(
                data=text.encode("utf-8"),
                label=label,
                origin=f"{origin_prefix}#{index}" + (f" {note}" if note else ""),
            )
        )
    return tuple(candidates)


def candidates_from_generator(
    executor: Executor,
    generator_path: Path,
    *,
    label: str,
    seeds: Sequence[int],
    max_bytes: int,
    on_progress: ProgressHook | None = None,
) -> tuple[tuple[Candidate, ...], tuple[Rejection, ...]]:
    """생성기 스크립트를 시드별로 돌려 입력을 만든다."""
    candidates: list[Candidate] = []
    rejections: list[Rejection] = []
    for seed in seeds:
        origin = f"{generator_path.stem}(seed={seed})"
        if on_progress is not None:
            on_progress(origin)
        result = executor.run_script(
            generator_path, None, HELPER_LIMITS, args=(str(seed),), output_limit=max_bytes * 2
        )
        if result.verdict is not Verdict.OK:
            rejections.append(Rejection(origin, f"생성기 실행 실패: {failure_note(result)}"))
            continue
        data = result.stdout.replace(compare.CRLF, compare.LF)
        if len(data) > max_bytes:
            rejections.append(
                Rejection(origin, f"입력이 상한({max_bytes}바이트)을 넘었습니다({len(data)}바이트).")
            )
            continue
        candidates.append(Candidate(data=data, label=label, origin=origin, seed=seed))
    return tuple(candidates), tuple(rejections)


def materialize(
    executor: Executor,
    workspace: Workspace,
    candidates: Sequence[Candidate],
    solution_path: Path,
    limits: Limits,
    *,
    validator_path: Path | None = None,
    start_index: int = 1,
    existing_digests: Sequence[str] = (),
    on_progress: ProgressHook | None = None,
) -> GenerationOutcome:
    """후보를 심사해 ``N.in``/``N.out``으로 확정한다.

    심사 순서: 중복 제거 → validator → 정답 코드 실행. 셋 중 하나라도 걸리면
    채택하지 않고 이유를 남긴다.
    """
    seen = set(existing_digests)
    records: list[CaseRecord] = []
    rejections: list[Rejection] = []
    next_index = start_index

    for candidate in candidates:
        if on_progress is not None:
            on_progress(candidate.origin or candidate.label)

        data = candidate.data.replace(compare.CRLF, compare.LF)
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            rejections.append(Rejection(candidate.origin, "같은 입력이 이미 있습니다."))
            continue

        if validator_path is not None:
            valid, message = validate_input(executor, validator_path, data)
            if not valid:
                rejections.append(Rejection(candidate.origin, f"제약 위반: {message}"))
                continue

        # 기준 풀이는 제한에 여유를 준다. 최대 크기 케이스에서 정답 코드가
        # 제한에 근접하면, 가장 가치 있는 케이스부터 탈락해 버린다.
        result = executor.run_on_bytes(solution_path, data, _reference_limits(limits))
        if result.verdict is not Verdict.OK:
            rejections.append(
                Rejection(candidate.origin, f"정답 코드가 처리하지 못했습니다: {failure_note(result)}")
            )
            continue

        record = exporter.write_case(
            workspace.cases_dir,
            next_index,
            data,
            result.stdout,
            label=candidate.label,
            origin=candidate.origin,
            seed=candidate.seed,
        )
        records.append(record)
        seen.add(digest)
        next_index += 1

    return GenerationOutcome(records=tuple(records), rejections=tuple(rejections))


def _reference_limits(limits: Limits) -> Limits:
    """기대 출력을 얻을 때 쓰는 완화된 제한."""
    return Limits(
        time_ms=limits.time_ms * REFERENCE_TIME_FACTOR,
        memory_mb=limits.memory_mb * REFERENCE_MEMORY_FACTOR,
        assumed=limits.assumed,
    )


def digests_of(workspace: Workspace, records: Sequence[CaseRecord]) -> tuple[str, ...]:
    """이미 채택된 입력들의 해시. 추가 생성 때 중복을 막는다."""
    digests: list[str] = []
    for record in records:
        path = workspace.input_path(record.index)
        if path.exists():
            digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return tuple(digests)
