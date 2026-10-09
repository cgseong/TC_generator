"""문제·케이스 데이터 모델.

모든 모델은 불변(frozen)이다. 값을 바꿀 때는 :func:`dataclasses.replace`로
새 객체를 만든다. 직렬화 형식은 ``problem.json``(PRD 6.3)이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from tcgen.config import (
    DEFAULT_CASE_COUNT,
    DEFAULT_MAX_INPUT_BYTES,
    DEFAULT_MEMORY_LIMIT_MB,
    DEFAULT_STRESS_ROUNDS,
    DEFAULT_TIME_LIMIT_MS,
    MIN_STRESS_ROUNDS,
)

SCHEMA_VERSION = 1

SOLUTION_SOURCE_AUTHOR = "author"
SOLUTION_SOURCE_LLM = "llm"

LABEL_EXAMPLE = "example"
LABEL_EDGE = "edge"
LABEL_RANDOM = "random"
LABEL_SCALE = "scale"
LABEL_ANTI = "anti"

#: 그림이 지문에서 맡는 역할. 삽화와 규칙 정의를 같이 취급하면 안 된다.
FIGURE_ROLE_DECORATIVE = "decorative"
FIGURE_ROLE_EXAMPLE = "example_visual"
FIGURE_ROLE_SPEC = "spec"
FIGURE_ROLE_RULE = "rule"
FIGURE_ROLES: frozenset[str] = frozenset(
    {FIGURE_ROLE_DECORATIVE, FIGURE_ROLE_EXAMPLE, FIGURE_ROLE_SPEC, FIGURE_ROLE_RULE}
)
#: 전사가 없으면 풀이가 성립하지 않는 역할들.
FIGURE_ROLES_CRITICAL: frozenset[str] = frozenset({FIGURE_ROLE_SPEC, FIGURE_ROLE_RULE})


@dataclass(frozen=True)
class Limits:
    """시간·메모리 제한. ``assumed``는 지문에 없어 가정한 값임을 뜻한다."""

    time_ms: int = DEFAULT_TIME_LIMIT_MS
    memory_mb: int = DEFAULT_MEMORY_LIMIT_MB
    assumed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_ms": self.time_ms,
            "memory_mb": self.memory_mb,
            "assumed": self.assumed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Limits":
        return cls(
            time_ms=int(data.get("time_ms", DEFAULT_TIME_LIMIT_MS)),
            memory_mb=int(data.get("memory_mb", DEFAULT_MEMORY_LIMIT_MB)),
            assumed=bool(data.get("assumed", False)),
        )


@dataclass(frozen=True)
class Example:
    """지문의 예제 입출력."""

    input: str
    output: str

    def to_dict(self) -> dict[str, Any]:
        return {"input": self.input, "output": self.output}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Example":
        return cls(input=str(data.get("input", "")), output=str(data.get("output", "")))


@dataclass(frozen=True)
class Figure:
    """지문 속 그림 하나와 그것을 글로 옮긴 내용 (PRD F1-8).

    ``description``이 비어 있으면 그림이 담은 정보는 **아무도 모르는 상태**다.
    프롬프트는 이 사실을 숨기지 않고 그대로 적어야 한다.
    """

    ref: str
    role: str = FIGURE_ROLE_DECORATIVE
    description: str = ""
    page: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "role": self.role,
            "description": self.description,
            "page": self.page,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Figure":
        role = str(data.get("role", FIGURE_ROLE_DECORATIVE))
        return cls(
            ref=str(data.get("ref", "")),
            # 모르는 role을 그대로 믿으면 critical 판정이 조용히 빗나간다.
            role=role if role in FIGURE_ROLES else FIGURE_ROLE_DECORATIVE,
            description=str(data.get("description", "")),
            page=int(data.get("page", 0) or 0),
        )

    @property
    def is_critical(self) -> bool:
        """풀이에 필요한 정보를 그림만 담고 있는 경우."""
        return self.role in FIGURE_ROLES_CRITICAL

    @property
    def is_unresolved(self) -> bool:
        """꼭 필요한데 전사가 없는 그림."""
        return self.is_critical and not self.description.strip()


@dataclass(frozen=True)
class CasePlan:
    """문제별 테스트케이스 생성 규모 (PRD F2-C6)."""

    count: int = DEFAULT_CASE_COUNT
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    stress_rounds: int = DEFAULT_STRESS_ROUNDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "max_input_bytes": self.max_input_bytes,
            "stress_rounds": self.stress_rounds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CasePlan":
        # 0이나 음수가 들어오면 생성·대조가 조용히 아무것도 하지 않게 된다.
        # 저장된 값이든 API로 들어온 값이든 여기서 하한을 건다.
        return cls(
            count=max(1, int(data.get("count", DEFAULT_CASE_COUNT))),
            max_input_bytes=max(1, int(data.get("max_input_bytes", DEFAULT_MAX_INPUT_BYTES))),
            stress_rounds=max(
                MIN_STRESS_ROUNDS, int(data.get("stress_rounds", DEFAULT_STRESS_ROUNDS))
            ),
        )


@dataclass(frozen=True)
class Problem:
    """문제 정보. 폼 입력과 지문 파싱이 모두 이 모델로 수렴한다."""

    slug: str
    title: str
    statement: str = ""
    input_spec: str = ""
    output_spec: str = ""
    constraints: str = ""
    hints: str = ""
    examples: tuple[Example, ...] = field(default_factory=tuple)
    figures: tuple[Figure, ...] = field(default_factory=tuple)
    #: 지문을 PDF로 올린 경우 작업공간 ``assets`` 안의 파일 이름.
    source_pdf: str = ""
    limits: Limits = field(default_factory=Limits)
    case_plan: CasePlan = field(default_factory=CasePlan)
    interpretation: str = ""
    interpretation_confirmed: bool = False
    solution_source: str = ""
    solution_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "slug": self.slug,
            "title": self.title,
            "statement": self.statement,
            "input_spec": self.input_spec,
            "output_spec": self.output_spec,
            "constraints": self.constraints,
            "hints": self.hints,
            "examples": [example.to_dict() for example in self.examples],
            "figures": [figure.to_dict() for figure in self.figures],
            "source_pdf": self.source_pdf,
            "limits": self.limits.to_dict(),
            "case_plan": self.case_plan.to_dict(),
            "interpretation": self.interpretation,
            "interpretation_confirmed": self.interpretation_confirmed,
            "solution_source": self.solution_source,
            "solution_confirmed": self.solution_confirmed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Problem":
        raw_examples: Sequence[Mapping[str, Any]] = data.get("examples", []) or []
        raw_figures: Sequence[Mapping[str, Any]] = data.get("figures", []) or []
        return cls(
            slug=str(data["slug"]),
            title=str(data.get("title", "")),
            statement=str(data.get("statement", "")),
            input_spec=str(data.get("input_spec", "")),
            output_spec=str(data.get("output_spec", "")),
            constraints=str(data.get("constraints", "")),
            hints=str(data.get("hints", "")),
            examples=tuple(Example.from_dict(item) for item in raw_examples),
            figures=tuple(Figure.from_dict(item) for item in raw_figures),
            source_pdf=str(data.get("source_pdf", "")),
            limits=Limits.from_dict(data.get("limits", {}) or {}),
            case_plan=CasePlan.from_dict(data.get("case_plan", {}) or {}),
            interpretation=str(data.get("interpretation", "")),
            interpretation_confirmed=bool(data.get("interpretation_confirmed", False)),
            solution_source=str(data.get("solution_source", "")),
            solution_confirmed=bool(data.get("solution_confirmed", False)),
        )

    def with_changes(self, **changes: Any) -> "Problem":
        """변경된 사본을 돌려준다. 원본은 건드리지 않는다."""
        return replace(self, **changes)

    @property
    def unresolved_figures(self) -> tuple[Figure, ...]:
        """풀이에 필요한데 전사가 비어 있는 그림들."""
        return tuple(figure for figure in self.figures if figure.is_unresolved)

    @property
    def is_exportable(self) -> bool:
        """정답 코드가 확정되지 않으면 내보낼 수 없다 (PRD F2-B6, F3-7)."""
        return self.solution_confirmed


@dataclass(frozen=True)
class CaseRecord:
    """생성된 테스트케이스 한 건의 메타데이터."""

    index: int
    label: str
    origin: str = ""
    seed: int | None = None
    input_bytes: int = 0
    output_bytes: int = 0
    crlf_normalized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "origin": self.origin,
            "seed": self.seed,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "crlf_normalized": self.crlf_normalized,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CaseRecord":
        raw_seed = data.get("seed")
        return cls(
            index=int(data["index"]),
            label=str(data.get("label", LABEL_RANDOM)),
            origin=str(data.get("origin", "")),
            seed=None if raw_seed is None else int(raw_seed),
            input_bytes=int(data.get("input_bytes", 0)),
            output_bytes=int(data.get("output_bytes", 0)),
            crlf_normalized=bool(data.get("crlf_normalized", False)),
        )
