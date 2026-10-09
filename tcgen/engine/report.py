"""리포트 작성 (PRD NF-R3).

실패·SKIP·낮은 신뢰도를 그대로 적는다. 숨기지 않는 것이 이 파일의 목적이다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from tcgen.models import CaseRecord, Problem

REPORT_FILENAME = "report.md"
RUN_FILENAME = "run.json"


def write_run_json(reports_dir: Path, payload: Mapping[str, Any]) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / RUN_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def render_report(
    problem: Problem,
    *,
    cases: Sequence[CaseRecord],
    strength: Mapping[str, Any] | None,
    stress: Mapping[str, Any] | None,
    export: Mapping[str, Any] | None,
    environment: Mapping[str, Any] | None,
    rejections: Sequence[Mapping[str, str]] = (),
) -> str:
    """사람이 읽을 리포트를 만든다."""
    sections = [
        _header(problem),
        _solution_section(problem, stress),
        _cases_section(cases, rejections),
        _strength_section(strength),
        _export_section(export),
        _environment_section(environment, strength),
    ]
    return "\n\n".join(section for section in sections if section).strip() + "\n"


def write_report(reports_dir: Path, content: str) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / REPORT_FILENAME
    path.write_text(content, encoding="utf-8")
    return path


def _header(problem: Problem) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"# 테스트케이스 리포트 — {problem.title}\n\n"
        f"| 항목 | 값 |\n|---|---|\n"
        f"| 작업 폴더 | `{problem.slug}` |\n"
        f"| 생성 시각 | {stamp} |\n"
        f"| 시간 제한 | {problem.limits.time_ms}ms"
        f"{' (가정값)' if problem.limits.assumed else ''} |\n"
        f"| 메모리 제한 | {problem.limits.memory_mb}MB"
        f"{' (가정값)' if problem.limits.assumed else ''} |\n"
        f"| 지문 해석 확인 | {'완료' if problem.interpretation_confirmed else '**미확인**'} |"
    )


def _solution_section(problem: Problem, stress: Mapping[str, Any] | None) -> str:
    lines = ["## 정답 코드"]
    source = {"author": "사용자 제공", "llm": "LLM 생성"}.get(problem.solution_source, "미상")
    lines.append(f"- 출처: {source}")
    lines.append(f"- 확정 여부: {'확정' if problem.solution_confirmed else '**미확정**'}")
    if stress is None:
        lines.append("- 브루트포스 대조: 수행하지 않음")
        return "\n".join(lines)

    passed = stress.get("passed")
    rounds = stress.get("rounds_run", 0)
    lines.append(f"- 브루트포스 대조: {'통과' if passed else '**실패**'} ({rounds}회)")
    if stress.get("note"):
        lines.append(f"- 비고: {stress['note']}")
    counterexample = stress.get("counterexample")
    if counterexample:
        lines.append("\n### 발견된 반례\n")
        lines.append(f"- 사유: {counterexample.get('reason', '')}")
        lines.append("```")
        lines.append(str(counterexample.get("input", "")).strip())
        lines.append("```")
        lines.append(f"- 정답 후보 출력: `{str(counterexample.get('solution_output', '')).strip()}`")
        lines.append(f"- 브루트포스 출력: `{str(counterexample.get('brute_output', '')).strip()}`")
    return "\n".join(lines)


def _cases_section(
    cases: Sequence[CaseRecord], rejections: Sequence[Mapping[str, str]]
) -> str:
    if not cases:
        return "## 테스트케이스\n\n생성된 케이스가 없습니다."

    lines = [f"## 테스트케이스 ({len(cases)}건)", "", "| 번호 | 종류 | 출처 | 입력 크기 | 출력 크기 |", "|---|---|---|---|---|"]
    for case in cases:
        lines.append(
            f"| {case.index} | {case.label} | {case.origin or '-'} | "
            f"{case.input_bytes:,}B | {case.output_bytes:,}B |"
        )
    if rejections:
        lines.extend(["", f"### 채택되지 않은 입력 ({len(rejections)}건)", ""])
        for rejection in rejections:
            lines.append(f"- `{rejection.get('origin', '')}` — {rejection.get('reason', '')}")
    return "\n".join(lines)


def _strength_section(strength: Mapping[str, Any] | None) -> str:
    if not strength:
        return "## 테스트 강도\n\n강도 검증을 수행하지 않았습니다."

    results = strength.get("results", [])
    survivors = [item for item in results if not item.get("killed")]
    lines = [
        "## 테스트 강도",
        "",
        f"- kill rate: **{strength.get('kill_rate', 0) * 100:.0f}%** "
        f"({len(results) - len(survivors)}/{len(results)})",
        "",
        "| 오답 코드 | 종류 | 결함 | 결과 | 잡은 케이스 |",
        "|---|---|---|---|---|",
    ]
    for result in results:
        status = f"{result.get('verdict')}로 잡힘" if result.get("killed") else "**생존**"
        killed_by = f"{result['killed_by']}번" if result.get("killed_by") else "-"
        lines.append(
            f"| {result.get('name')} | {result.get('kind')} | {result.get('flaw') or '-'} | "
            f"{status} | {killed_by} |"
        )
    if survivors:
        lines.extend(
            [
                "",
                "> **생존한 오답 코드가 있습니다.** 이 테스트로는 틀린 제출이 통과합니다. "
                "추가 케이스를 생성하거나 수동으로 보강하세요.",
            ]
        )
    return "\n".join(lines)


def _export_section(export: Mapping[str, Any] | None) -> str:
    if export is None:
        return "## 내보내기\n\n아직 내보내지 않았습니다."

    lines = [
        "## 내보내기",
        "",
        f"- 파일: `{export.get('zip_path', '')}`",
        f"- 케이스 수: {export.get('case_count', 0)}건",
        "- 구조: zip 루트에 `N.in`/`N.out` + 확장자 없는 `info`",
    ]
    issues = export.get("issues", [])
    if issues:
        lines.extend(["", "### 점검 결과", ""])
        for issue in issues:
            marker = "**오류**" if issue.get("severity") == "error" else "경고"
            lines.append(f"- {marker}: {issue.get('message', '')}")
    else:
        lines.append("- 자체 검증: 이상 없음")
    return "\n".join(lines)


def _environment_section(
    environment: Mapping[str, Any] | None, strength: Mapping[str, Any] | None
) -> str:
    lines = ["## 실행 환경과 측정 신뢰도"]
    if environment:
        lines.append(f"- {environment.get('banner', '')}")
        lines.append(f"- 측정 방식: {environment.get('measurement', '')}")
        skipped = environment.get("skipped_languages") or []
        if skipped:
            lines.append(f"- **SKIP된 언어: {', '.join(skipped)}** (검증되지 않았습니다)")
        available = environment.get("available_languages") or []
        if available:
            lines.append(f"- 검증한 언어: {', '.join(available)}")
    if strength:
        note = strength.get("reliability_note", "")
        if note:
            lines.append(f"- {note}")
    return "\n".join(lines)
