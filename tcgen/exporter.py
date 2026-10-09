"""QingdaoU OnlineJudge 테스트케이스 패키징 (PRD F3, 5장).

공식 소스에서 확인한 규칙을 그대로 구현한다.

- 메타데이터 파일 이름은 ``info.json``이 **아니라** 확장자 없는 ``info``다.
- ``N.in``/``N.out``은 zip **루트**에 둔다. 하위 폴더는 인식되지 않는다.
- 번호는 1부터 빈틈 없이 연속이어야 한다. 서버는 첫 결번에서 스캔을 멈추므로
  결번 뒤의 케이스는 **조용히 사라진다**.
- ``stripped_output_md5``는 CRLF를 LF로 바꾼 뒤 ``rstrip``한 값의 md5다.

내보낸 zip은 반드시 다시 읽어 검증한다 (F3-5). 관리자 UI 업로드 경로에서는
서버가 ``info``를 재생성하므로 우리 쪽 오류가 드러나지 않고, 직접 배치
경로에서만 터지기 때문이다.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from tcgen import compare
from tcgen.models import CaseRecord

INFO_FILENAME = "info"
INFO_INDENT = 4
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

_INPUT_PATTERN = re.compile(r"^(\d+)\.in$")


class ExportError(Exception):
    """내보내기를 진행할 수 없는 구조적 오류."""


@dataclass(frozen=True)
class ExportIssue:
    """내보내기 과정에서 발견한 문제."""

    severity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "message": self.message}


@dataclass(frozen=True)
class ExportResult:
    """내보내기 결과."""

    zip_path: Path
    case_count: int
    issues: tuple[ExportIssue, ...] = ()

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == SEVERITY_ERROR for issue in self.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "zip_path": str(self.zip_path),
            "case_count": self.case_count,
            "issues": [issue.to_dict() for issue in self.issues],
            "has_errors": self.has_errors,
        }


def write_case(
    cases_dir: Path,
    index: int,
    input_bytes: bytes,
    raw_output: bytes,
    *,
    label: str,
    origin: str = "",
    seed: int | None = None,
) -> CaseRecord:
    """케이스 한 건을 기록한다.

    출력은 :func:`tcgen.compare.canonical_output`을 거쳐 **LF로만** 저장된다.
    Windows에서 파이썬이 찍은 CRLF를 그대로 두면 업로드 해시와 채점 해시가
    어긋난다 (PRD 5.1).
    """
    cases_dir.mkdir(parents=True, exist_ok=True)
    canonical = compare.canonical_output(raw_output)
    normalized_input = input_bytes.replace(compare.CRLF, compare.LF)
    (cases_dir / f"{index}.in").write_bytes(normalized_input)
    (cases_dir / f"{index}.out").write_bytes(canonical)
    return CaseRecord(
        index=index,
        label=label,
        origin=origin,
        seed=seed,
        input_bytes=len(normalized_input),
        output_bytes=len(canonical),
        crlf_normalized=compare.has_crlf(raw_output),
    )


def collect_case_numbers(cases_dir: Path) -> tuple[int, ...]:
    """``N.in`` 파일에서 케이스 번호를 모아 정렬해 돌려준다."""
    if not cases_dir.exists():
        return ()
    numbers = (
        int(match.group(1))
        for match in (_INPUT_PATTERN.match(path.name) for path in cases_dir.iterdir())
        if match
    )
    return tuple(sorted(numbers))


def inspect_cases(cases_dir: Path) -> tuple[ExportIssue, ...]:
    """내보내기 전 구조 점검. 치명적 문제와 경고를 모두 모은다."""
    numbers = collect_case_numbers(cases_dir)
    if not numbers:
        return (ExportIssue(SEVERITY_ERROR, "내보낼 테스트케이스가 없습니다."),)

    issues: list[ExportIssue] = []
    expected = tuple(range(1, len(numbers) + 1))
    if numbers != expected:
        missing = sorted(set(expected) - set(numbers))
        issues.append(
            ExportIssue(
                SEVERITY_ERROR,
                f"케이스 번호가 1부터 연속이 아닙니다(빠진 번호: {missing}). "
                "서버는 첫 결번에서 스캔을 멈춰 뒤쪽 케이스를 버립니다.",
            )
        )

    for number in numbers:
        issues.extend(_inspect_single_case(cases_dir, number))
    return tuple(issues)


def build_info(cases_dir: Path, numbers: Sequence[int], *, spj: bool = False) -> dict:
    """``info`` 파일의 내용을 만든다."""
    test_cases: dict[str, dict[str, object]] = {}
    for number in numbers:
        input_path = cases_dir / f"{number}.in"
        output_path = cases_dir / f"{number}.out"
        output_bytes = output_path.read_bytes()
        test_cases[str(number)] = {
            "stripped_output_md5": compare.expected_md5(output_bytes),
            "input_size": input_path.stat().st_size,
            "output_size": output_path.stat().st_size,
            "input_name": input_path.name,
            "output_name": output_path.name,
        }
    return {"spj": spj, "test_cases": test_cases}


def export_zip(cases_dir: Path, destination: Path, *, spj: bool = False) -> ExportResult:
    """zip을 만들고 즉시 다시 읽어 검증한다 (PRD F3-5)."""
    issues = inspect_cases(cases_dir)
    fatal = [issue for issue in issues if issue.severity == SEVERITY_ERROR]
    if fatal:
        raise ExportError(" / ".join(issue.message for issue in fatal))

    numbers = collect_case_numbers(cases_dir)
    info = build_info(cases_dir, numbers, spj=spj)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for number in numbers:
            archive.write(cases_dir / f"{number}.in", f"{number}.in")
            archive.write(cases_dir / f"{number}.out", f"{number}.out")
        archive.writestr(INFO_FILENAME, json.dumps(info, indent=INFO_INDENT))

    verification = verify_zip(destination)
    if any(issue.severity == SEVERITY_ERROR for issue in verification):
        # 자체 검증에 실패한 zip을 남겨 두면 사용자가 그걸 업로드한다.
        destination.unlink(missing_ok=True)
    return ExportResult(
        zip_path=destination,
        case_count=len(numbers),
        issues=tuple(issues) + verification,
    )


def verify_zip(zip_path: Path) -> tuple[ExportIssue, ...]:
    """만들어진 zip이 실제로 규격에 맞는지 다시 확인한다."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            if INFO_FILENAME not in names:
                return (ExportIssue(SEVERITY_ERROR, "zip에 'info' 파일이 없습니다."),)
            try:
                info = json.loads(archive.read(INFO_FILENAME).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                return (ExportIssue(SEVERITY_ERROR, f"'info'를 읽을 수 없습니다: {error}"),)
            return _verify_contents(archive, names, info)
    except (OSError, zipfile.BadZipFile) as error:
        return (ExportIssue(SEVERITY_ERROR, f"zip을 열 수 없습니다: {error}"),)


def _verify_contents(
    archive: zipfile.ZipFile, names: set[str], info: dict
) -> tuple[ExportIssue, ...]:
    issues: list[ExportIssue] = []
    test_cases = info.get("test_cases", {})
    if not isinstance(test_cases, dict) or not test_cases:
        return (ExportIssue(SEVERITY_ERROR, "'info'에 test_cases가 없습니다."),)

    numbers = sorted(int(key) for key in test_cases)
    if numbers != list(range(1, len(numbers) + 1)):
        issues.append(ExportIssue(SEVERITY_ERROR, "'info'의 케이스 번호가 1부터 연속이 아닙니다."))

    for key, entry in test_cases.items():
        issues.extend(_verify_entry(archive, names, key, entry))

    extra = names - {INFO_FILENAME} - {f"{key}.in" for key in test_cases} - {
        f"{key}.out" for key in test_cases
    }
    if extra:
        issues.append(
            ExportIssue(SEVERITY_WARNING, f"zip에 불필요한 파일이 있습니다: {sorted(extra)}")
        )
    if any("/" in name for name in names):
        issues.append(
            ExportIssue(SEVERITY_ERROR, "zip 루트가 아닌 하위 폴더에 파일이 있습니다.")
        )
    return tuple(issues)


def _verify_entry(
    archive: zipfile.ZipFile, names: set[str], key: str, entry: dict
) -> list[ExportIssue]:
    input_name = f"{key}.in"
    output_name = f"{key}.out"
    if input_name not in names or output_name not in names:
        return [ExportIssue(SEVERITY_ERROR, f"{key}번 케이스의 입력/출력 파일이 빠졌습니다.")]

    content = archive.read(output_name)
    actual_md5 = compare.expected_md5(content)
    if entry.get("stripped_output_md5") != actual_md5:
        return [
            ExportIssue(
                SEVERITY_ERROR,
                f"{key}번 케이스의 stripped_output_md5가 실제 출력과 다릅니다.",
            )
        ]
    if entry.get("output_size") != len(content):
        return [ExportIssue(SEVERITY_ERROR, f"{key}번 케이스의 output_size가 맞지 않습니다.")]
    return []


def _inspect_single_case(cases_dir: Path, number: int) -> list[ExportIssue]:
    issues: list[ExportIssue] = []
    output_path = cases_dir / f"{number}.out"
    if not output_path.exists():
        issues.append(ExportIssue(SEVERITY_ERROR, f"{number}.out 파일이 없습니다."))
        return issues

    content = output_path.read_bytes()
    if compare.has_crlf(content):
        issues.append(
            ExportIssue(
                SEVERITY_ERROR,
                f"{number}.out에 CRLF 개행이 있습니다. 채점기 해시와 어긋납니다.",
            )
        )
    dirty_lines = compare.lines_with_trailing_space(content)
    if dirty_lines:
        issues.append(
            ExportIssue(
                SEVERITY_WARNING,
                f"{number}.out의 {list(dirty_lines)}번째 줄 끝에 공백이 있습니다. "
                "줄 중간의 공백은 오답 처리됩니다.",
            )
        )
    if not content:
        issues.append(ExportIssue(SEVERITY_WARNING, f"{number}.out이 비어 있습니다."))
    return issues
