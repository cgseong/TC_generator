"""폴더 기반 작업공간 (PRD 6.3).

DB를 쓰지 않는다. 문제 하나가 폴더 하나이고, 폴더를 복사하면 그대로 이어서
작업할 수 있다.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from tcgen.config import WORKSPACES_DIR
from tcgen.models import CaseRecord, Problem

PROBLEM_FILE = "problem.json"
CASES_INDEX_FILE = "index.json"
SOLUTION_FILE = "sol.py"
BRUTE_FILE = "brute.py"
VALIDATOR_FILE = "validator.py"

SUBDIRECTORIES: tuple[str, ...] = (
    "solutions",
    "gens",
    "cases",
    "mutants",
    "reports",
    "export",
    "logs",
)

_SLUG_INVALID = re.compile(r"[^\w+-]+", re.UNICODE)
_SLUG_DASHES = re.compile(r"-{2,}")
MAX_SLUG_LENGTH = 60
FALLBACK_SLUG = "problem"

#: 조회 경로에서 허용하는 slug 형태. 경로 구분자·드라이브 문자·점이 모두 빠진다.
_SAFE_SLUG = re.compile(rf"\A[\w+-]{{1,{MAX_SLUG_LENGTH}}}\Z", re.UNICODE)

#: Windows 예약 장치명. 폴더로 만들 수 없어 mkdir이 실패한다.
_WINDOWS_RESERVED: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


def slugify(title: str) -> str:
    """문제 제목을 폴더명으로 바꾼다. 한글은 그대로 둔다."""
    collapsed = _SLUG_INVALID.sub("-", title.strip())
    collapsed = _SLUG_DASHES.sub("-", collapsed).strip("-_")
    slug = collapsed[:MAX_SLUG_LENGTH].strip("-_").lower() if collapsed else ""
    if not slug:
        return FALLBACK_SLUG
    return f"{slug}-ws" if slug in _WINDOWS_RESERVED else slug


def ensure_safe_slug(slug: str) -> str:
    """작업공간 이름이 단일 경로 조각인지 확인한다.

    URL에서 온 slug를 그대로 ``root / slug``에 넣으면 Windows에서 경로를
    벗어난다. ``C:\\Windows``처럼 드라이브가 붙으면 base가 통째로 대체되고,
    ``..``는 상위로 올라간다. 조회 경로의 유일한 관문이므로 형태부터 막는다.
    """
    if not _SAFE_SLUG.match(slug or ""):
        raise WorkspaceError(f"작업공간 이름이 올바르지 않습니다: {slug!r}")
    return slug


class WorkspaceError(Exception):
    """작업공간 조작 실패."""


@dataclass(frozen=True)
class Workspace:
    """문제 하나의 작업 폴더."""

    root: Path

    @property
    def slug(self) -> str:
        return self.root.name

    @property
    def solutions_dir(self) -> Path:
        return self.root / "solutions"

    @property
    def gens_dir(self) -> Path:
        return self.root / "gens"

    @property
    def cases_dir(self) -> Path:
        return self.root / "cases"

    @property
    def mutants_dir(self) -> Path:
        return self.root / "mutants"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def export_dir(self) -> Path:
        return self.root / "export"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def problem_path(self) -> Path:
        return self.root / PROBLEM_FILE

    @property
    def solution_path(self) -> Path:
        return self.solutions_dir / SOLUTION_FILE

    @property
    def brute_path(self) -> Path:
        return self.solutions_dir / BRUTE_FILE

    @property
    def validator_path(self) -> Path:
        return self.root / VALIDATOR_FILE

    @property
    def cases_index_path(self) -> Path:
        return self.cases_dir / CASES_INDEX_FILE

    def input_path(self, index: int) -> Path:
        return self.cases_dir / f"{index}.in"

    def output_path(self, index: int) -> Path:
        return self.cases_dir / f"{index}.out"

    def ensure_directories(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRECTORIES:
            (self.root / name).mkdir(exist_ok=True)

    def save_problem(self, problem: Problem) -> None:
        self.ensure_directories()
        _write_json(self.problem_path, problem.to_dict())

    def load_problem(self) -> Problem:
        if not self.problem_path.exists():
            raise WorkspaceError(f"문제 정보가 없습니다: {self.problem_path}")
        return Problem.from_dict(_read_json(self.problem_path))

    def save_cases_index(self, cases: Sequence[CaseRecord]) -> None:
        self.cases_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.cases_index_path, [case.to_dict() for case in cases])

    def load_cases_index(self) -> tuple[CaseRecord, ...]:
        if not self.cases_index_path.exists():
            return ()
        return tuple(CaseRecord.from_dict(item) for item in _read_json(self.cases_index_path))

    def clear_cases(self) -> None:
        """케이스 파일을 모두 지운다. 재생성 전에 호출한다."""
        # rmtree 직전에 봉쇄를 재확인한다. 경로가 어떤 경위로든 작업공간을
        # 벗어났다면 지우지 않고 멈춘다.
        if not self.contains(self.cases_dir):
            raise WorkspaceError(f"작업공간 밖을 지우려 했습니다: {self.cases_dir}")
        if self.cases_dir.exists():
            shutil.rmtree(self.cases_dir)
        self.cases_dir.mkdir(parents=True, exist_ok=True)

    def contains(self, path: Path) -> bool:
        """경로가 이 작업공간 안에 있는지. 실행 경로 검증에 쓴다."""
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return False
        return True


def create_workspace(problem: Problem, base_dir: Path | None = None) -> Workspace:
    """작업 폴더를 만들고 ``problem.json``을 기록한다.

    같은 이름이 있으면 ``-2``, ``-3``처럼 뒤에 번호를 붙여 덮어쓰기를 피한다.
    """
    root_dir = base_dir or WORKSPACES_DIR
    root_dir.mkdir(parents=True, exist_ok=True)
    requested = slugify(problem.slug or problem.title)
    slug = _unique_slug(root_dir, requested)
    workspace = Workspace(root=root_dir / slug)
    workspace.ensure_directories()
    workspace.save_problem(problem.with_changes(slug=slug))
    return workspace


def normalize_slug(slug: str) -> str:
    """조회용 정규형. 대소문자만 다른 이름이 같은 폴더를 가리키는 것을 막는다.

    Windows 파일시스템은 대소문자를 구분하지 않아 ``demo``와 ``DEMO``가 같은
    폴더다. 정규화하지 않으면 작업 잠금이 서로 다른 키로 갈라져 한 폴더에서
    두 작업이 동시에 돈다.
    """
    return ensure_safe_slug(slug).lower()


def open_workspace(slug: str, base_dir: Path | None = None) -> Workspace:
    """작업공간을 연다. ``WORKSPACES_DIR`` 밖은 절대 열지 않는다."""
    root_dir = (base_dir or WORKSPACES_DIR).resolve()
    candidate = (root_dir / normalize_slug(slug)).resolve()
    # 형식 검증을 뚫더라도(심볼릭 링크·정션 등) 최종 경로가 밖이면 거부한다.
    if candidate.parent != root_dir:
        raise WorkspaceError(f"작업공간 범위를 벗어났습니다: {slug!r}")
    workspace = Workspace(root=candidate)
    if not workspace.problem_path.exists():
        raise WorkspaceError(f"작업공간을 찾을 수 없습니다: {slug}")
    return workspace


def list_workspaces(base_dir: Path | None = None) -> tuple[Workspace, ...]:
    """``problem.json``이 있는 폴더만 작업공간으로 인정한다."""
    root_dir = base_dir or WORKSPACES_DIR
    if not root_dir.exists():
        return ()
    found = (
        Workspace(root=child)
        for child in sorted(root_dir.iterdir())
        if child.is_dir() and (child / PROBLEM_FILE).exists()
    )
    return tuple(found)


def _unique_slug(root_dir: Path, slug: str) -> str:
    candidate = slug or FALLBACK_SLUG
    if not (root_dir / candidate).exists():
        return candidate
    suffix = 2
    while (root_dir / f"{candidate}-{suffix}").exists():
        suffix += 1
    return f"{candidate}-{suffix}"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
