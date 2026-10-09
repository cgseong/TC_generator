"""실행기 공통 타입."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, Sequence

from tcgen.models import Limits


class Verdict(str, Enum):
    """실행 결과 판정."""

    OK = "OK"
    WA = "WA"
    TLE = "TLE"
    MLE = "MLE"
    RE = "RE"
    OLE = "OLE"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class RunResult:
    """한 번의 실행 결과.

    ``measurement``는 측정 방식을 담는다. Windows 폴링 측정은 정밀도가 낮아
    리포트에 그대로 노출해야 한다 (PRD F2-D6, NF-S6).
    """

    verdict: Verdict
    stdout: bytes = b""
    stderr: str = ""
    time_ms: int = 0
    memory_kb: int | None = None
    exit_code: int | None = None
    measurement: str = ""
    isolated: bool = False
    note: str = ""

    @property
    def succeeded(self) -> bool:
        return self.verdict is Verdict.OK


class Runner(Protocol):
    """실행기 프로토콜. Docker·WSL 실행기도 이 형태를 따른다."""

    name: str
    isolated: bool

    def run(
        self,
        argv: Sequence[str],
        stdin_path: Path | None,
        limits: Limits,
        cwd: Path | None = None,
        output_limit: int | None = None,
    ) -> RunResult: ...
