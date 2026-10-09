"""스크립트 기록·실행 헬퍼.

LLM이 만든 코드도 신뢰하지 않는다. 디스크에 쓰기 전에 정적 스캔을 거치고
(PRD NF-S5), 실행은 실행기 어댑터를 통해서만 한다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from tcgen import safety
from tcgen.models import Limits
from tcgen.runner.base import RunResult, Runner, Verdict
from tcgen.runner.local import python_command

TEMP_DIRNAME = ".tmp"


class SafetyBlocked(Exception):
    """차단 수준의 위험 패턴이 발견되어 실행하지 않았다."""

    def __init__(self, findings: Sequence[safety.Finding], source_name: str) -> None:
        detail = "; ".join(f"{item.line}행 {item.detail}" for item in findings if item.is_blocking)
        super().__init__(f"{source_name}에서 위험 패턴이 발견되어 중단했습니다: {detail}")
        self.findings = tuple(findings)
        self.source_name = source_name


def write_script(path: Path, source: str) -> tuple[safety.Finding, ...]:
    """스캔을 통과한 소스만 기록한다. 경고는 통과시키되 돌려준다."""
    findings = safety.scan_python_source(source)
    if safety.has_blocking(findings):
        raise SafetyBlocked(findings, path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return findings


@dataclass(frozen=True)
class Executor:
    """작업폴더 안에서 파이썬 스크립트를 실행한다."""

    runner: Runner
    work_dir: Path

    def run_script(
        self,
        script: Path,
        stdin_path: Path | None,
        limits: Limits,
        *,
        args: Sequence[str] = (),
        output_limit: int | None = None,
    ) -> RunResult:
        argv = (*python_command(script), *args)
        return self.runner.run(
            argv,
            stdin_path,
            limits,
            cwd=self.work_dir,
            output_limit=output_limit,
        )

    def run_on_bytes(
        self,
        script: Path,
        data: bytes,
        limits: Limits,
        *,
        args: Sequence[str] = (),
        output_limit: int | None = None,
    ) -> RunResult:
        """메모리에 있는 입력으로 실행한다. 임시 파일을 거친다."""
        temp_dir = self.work_dir / TEMP_DIRNAME
        temp_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(data).hexdigest()[:16]
        temp_path = temp_dir / f"{script.stem}-{digest}.in"
        temp_path.write_bytes(data)
        try:
            return self.run_script(
                script, temp_path, limits, args=args, output_limit=output_limit
            )
        finally:
            temp_path.unlink(missing_ok=True)


def succeeded(result: RunResult) -> bool:
    return result.verdict is Verdict.OK


def failure_note(result: RunResult) -> str:
    """실패 원인을 사람이 읽을 한 줄로 만든다."""
    head = result.stderr.strip().splitlines()
    detail = head[-1] if head else ""
    return f"{result.verdict.value} ({result.time_ms}ms){' · ' + detail if detail else ''}"
