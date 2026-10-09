"""환경 점검 (PRD NF-P2, NF-P3).

없는 도구를 숨기지 않는다. SKIP되는 언어를 그대로 보여주고, 격리 여부를
배너 문구로 돌려준다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from tcgen.config import MEASUREMENT_POLLING, MEASUREMENT_UNAVAILABLE

NO_ISOLATION_BANNER = "현재 격리 없음 — 로컬에서 직접 실행 중"
ISOLATED_BANNER = "격리 실행 중"

_VERSION_TIMEOUT_S = 5.0

#: 언어별로 필요한 실행 파일
LANGUAGE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "python": (),
    "c": ("gcc",),
    "cpp": ("g++",),
    "java": ("javac", "java"),
}

_PROBED_TOOLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("docker", ("docker", "--version")),
    ("gcc", ("gcc", "--version")),
    ("g++", ("g++", "--version")),
    ("javac", ("javac", "-version")),
    ("java", ("java", "-version")),
    ("claude", ("claude", "--version")),
)


@dataclass(frozen=True)
class ToolStatus:
    """도구 하나의 설치 여부."""

    name: str
    available: bool
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "available": self.available, "detail": self.detail}


@dataclass(frozen=True)
class EnvironmentReport:
    """환경 점검 결과."""

    tools: tuple[ToolStatus, ...]
    isolated: bool
    measurement: str
    available_languages: tuple[str, ...]
    skipped_languages: tuple[str, ...]

    @property
    def banner(self) -> str:
        return ISOLATED_BANNER if self.isolated else NO_ISOLATION_BANNER

    @property
    def llm_available(self) -> bool:
        return self.tool("claude").available

    def tool(self, name: str) -> ToolStatus:
        for status in self.tools:
            if status.name == name:
                return status
        return ToolStatus(name=name, available=False, detail="점검하지 않음")

    def to_dict(self) -> dict[str, object]:
        return {
            "tools": [status.to_dict() for status in self.tools],
            "isolated": self.isolated,
            "banner": self.banner,
            "measurement": self.measurement,
            "available_languages": list(self.available_languages),
            "skipped_languages": list(self.skipped_languages),
            "llm_available": self.llm_available,
        }


def probe_environment() -> EnvironmentReport:
    """현재 PC의 도구 설치 상태를 조사한다."""
    tools = (_python_status(), _psutil_status(), *(_probe_tool(name, argv) for name, argv in _PROBED_TOOLS))
    available_names = {status.name for status in tools if status.available}
    available_languages = tuple(
        language
        for language, required in LANGUAGE_REQUIREMENTS.items()
        if all(tool in available_names for tool in required)
    )
    skipped_languages = tuple(
        language for language in LANGUAGE_REQUIREMENTS if language not in available_languages
    )
    return EnvironmentReport(
        tools=tools,
        isolated=False,  # LocalRunner만 있는 동안은 항상 격리 없음
        measurement=MEASUREMENT_POLLING if _has_psutil() else MEASUREMENT_UNAVAILABLE,
        available_languages=available_languages,
        skipped_languages=skipped_languages,
    )


def _python_status() -> ToolStatus:
    return ToolStatus(name="python", available=True, detail=sys.version.split()[0])


def _psutil_status() -> ToolStatus:
    if _has_psutil():
        return ToolStatus(name="psutil", available=True, detail="메모리·시간 폴링 측정 가능")
    return ToolStatus(
        name="psutil",
        available=False,
        detail="pip install psutil — 없으면 메모리는 미측정으로 표시됩니다",
    )


def _probe_tool(name: str, argv: tuple[str, ...]) -> ToolStatus:
    if shutil.which(argv[0]) is None:
        return ToolStatus(name=name, available=False, detail="설치되지 않음")
    try:
        completed = subprocess.run(  # noqa: S603 - 버전 확인용 고정 명령
            list(argv),
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return ToolStatus(name=name, available=False, detail=f"실행 실패: {error}")
    output = (completed.stdout or completed.stderr or "").strip()
    return ToolStatus(name=name, available=True, detail=output.splitlines()[0] if output else "")


def _has_psutil() -> bool:
    try:
        import psutil  # noqa: F401
    except ImportError:
        return False
    return True
