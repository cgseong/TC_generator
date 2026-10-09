"""Claude Code CLI headless 연동 (PRD F2-A).

**앱은 CLI로부터 텍스트/JSON만 받는다.** 파일 쓰기·실행·비교·패키징은 전부 앱
코드가 수행한다. 그래서 호출할 때 도구 권한을 명령줄에서 차단한다.

- ``--restricted`` : 명령·코드를 실행하는 내장 도구를 제거
- ``--disallowed-tools`` : 파일 읽기/쓰기·검색·웹 도구를 거부
- ``--strict-mcp-config`` : 사용자의 MCP 서버를 쓰지 않음
- ``--no-session-persistence`` : 세션을 디스크에 남기지 않음

프롬프트는 argv가 아니라 stdin으로 넘긴다. Windows 명령줄 길이 제한(약 32K)에
지문이 걸리는 것을 피하기 위함이다.

**예외는 PDF 지문 하나뿐이다.** 그림은 텍스트로 옮겨 붙일 수 없으므로
:meth:`ClaudeCLI.ask_json_about_files`는 지정한 폴더에 한해 ``Read``를 연다.
받는 것은 여전히 JSON 텍스트뿐이고, 쓰기·실행·네트워크 도구는 그대로 막힌다.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tcgen.config import LLM_MAX_RETRIES, LLM_TIMEOUT_S

logger = logging.getLogger(__name__)

READ_TOOL = "Read"

DISALLOWED_TOOLS = (
    "Edit",
    "Write",
    "NotebookEdit",
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
)

_FENCE_PATTERN = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)


#: 로그에 남으면 안 되는 토큰 형태들
_SECRET_PATTERN = re.compile(r"(sk-ant-[\w-]+|sk-[A-Za-z0-9]{20,}|Bearer\s+[\w.\-]+)")
#: 같은 초에 같은 태그로 두 번 호출해도 로그가 덮어써지지 않게 한다.
_LOG_SEQUENCE = itertools.count()
_LOG_STDOUT_LIMIT = 20_000
_LOG_STDERR_LIMIT = 4_000


def redact(text: str) -> str:
    """로그에 남기기 전에 토큰 형태를 가린다."""
    return _SECRET_PATTERN.sub("[REDACTED]", text or "")


def resolve_readable_dirs(paths: Sequence[Path]) -> tuple[Path, ...]:
    """읽기를 열어 줄 폴더 목록을 만든다.

    파일이 실제로 있는지 먼저 확인한다. 없는 경로로 CLI를 띄우면 모델이 파일을
    못 찾은 채 지문을 지어내고, 그 결과가 조용히 문제로 저장된다.
    """
    if not paths:
        raise LLMError("읽을 파일이 지정되지 않았습니다.")
    directories: list[Path] = []
    for path in paths:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise LLMError(f"파일을 찾을 수 없습니다: {resolved}")
        parent = resolved.parent
        if parent not in directories:
            directories.append(parent)
    return tuple(directories)


class LLMError(Exception):
    """CLI 호출 또는 응답 해석 실패."""


@dataclass(frozen=True)
class LLMResponse:
    """CLI 한 번의 응답."""

    text: str
    duration_ms: int
    tag: str = ""


CommandRunner = Callable[[Sequence[str], str, float], subprocess.CompletedProcess]


def _run_subprocess(
    argv: Sequence[str], prompt: str, timeout_s: float
) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - 실행 파일은 claude CLI로 고정된다
        list(argv),
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )


@dataclass(frozen=True)
class ClaudeCLI:
    """Claude Code CLI 클라이언트."""

    executable: str = "claude"
    model: str | None = None
    timeout_s: float = LLM_TIMEOUT_S
    log_dir: Path | None = None
    max_retries: int = LLM_MAX_RETRIES
    runner: CommandRunner = field(default=_run_subprocess, repr=False)

    def ask(
        self, prompt: str, *, tag: str = "ask", readable_dirs: Sequence[Path] = ()
    ) -> LLMResponse:
        """프롬프트를 보내고 응답 텍스트를 받는다.

        ``readable_dirs``가 주어지면 그 폴더에 한해 ``Read``만 열어 준다.
        PDF 지문처럼 파일을 직접 보여줘야 하는 호출에만 쓴다.
        """
        argv = self._build_argv(readable_dirs=readable_dirs)
        started = time.perf_counter()
        try:
            completed = self.runner(argv, prompt, self.timeout_s)
        except subprocess.TimeoutExpired as error:
            raise LLMError(f"CLI 응답이 {self.timeout_s:.0f}초를 넘겼습니다.") from error
        except OSError as error:
            raise LLMError(f"CLI를 실행할 수 없습니다: {error}") from error

        duration_ms = int((time.perf_counter() - started) * 1000)
        text = _extract_result_text(completed)
        self._write_log(tag, prompt, completed, duration_ms)
        return LLMResponse(text=text, duration_ms=duration_ms, tag=tag)

    def ask_code(self, prompt: str, *, tag: str = "code") -> str:
        """코드 블록 하나를 받아낸다. 실패하면 재시도한다."""
        return self._ask_with_retry(prompt, tag=tag, extract=extract_code_block)

    def ask_json(
        self, prompt: str, *, tag: str = "json", readable_dirs: Sequence[Path] = ()
    ) -> dict[str, Any]:
        """JSON 객체 하나를 받아낸다. 실패하면 재시도한다."""
        return self._ask_with_retry(
            prompt, tag=tag, extract=extract_json, readable_dirs=readable_dirs
        )

    def ask_json_about_files(
        self, prompt: str, paths: Sequence[Path], *, tag: str = "file"
    ) -> dict[str, Any]:
        """지정한 파일만 읽게 하고 JSON 하나를 받아낸다.

        PDF 지문처럼 텍스트로 옮길 수 없는 자료를 다루는 유일한 통로다.
        파일이 있는 폴더만 ``--add-dir``로 열고 ``Read``만 허용한다. 쓰기·실행·
        네트워크 도구는 다른 호출과 똑같이 막힌 채로 둔다.
        """
        return self.ask_json(prompt, tag=tag, readable_dirs=resolve_readable_dirs(paths))

    def _ask_with_retry(
        self,
        prompt: str,
        *,
        tag: str,
        extract: Callable[[str], Any],
        readable_dirs: Sequence[Path] = (),
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            response = self.ask(
                prompt, tag=f"{tag}-{attempt + 1}", readable_dirs=readable_dirs
            )
            try:
                return extract(response.text)
            except LLMError as error:
                last_error = error
                logger.warning("응답 해석 실패(%s, 시도 %d): %s", tag, attempt + 1, error)
                prompt = f"{prompt}\n\n[재시도] 직전 응답을 해석할 수 없었습니다: {error}"
        raise LLMError(f"{self.max_retries + 1}회 시도했지만 해석에 실패했습니다: {last_error}")

    def _build_argv(self, *, readable_dirs: Sequence[Path] = ()) -> tuple[str, ...]:
        resolved = shutil.which(self.executable)
        if resolved is None:
            raise LLMError(
                f"'{self.executable}' 실행 파일을 찾을 수 없습니다. "
                "Claude Code CLI를 설치하고 PATH에 등록하세요."
            )
        # --disallowed-tools가 --allowed-tools를 이긴다. Read를 열려면 거부
        # 목록에서 빼야 한다. 나머지 도구는 어느 경우에도 그대로 막힌다.
        disallowed = (
            tuple(tool for tool in DISALLOWED_TOOLS if tool != READ_TOOL)
            if readable_dirs
            else DISALLOWED_TOOLS
        )
        argv = [
            resolved,
            "-p",
            "--output-format",
            "json",
            "--restricted",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--disallowed-tools",
            " ".join(disallowed),
        ]
        if self.model:
            argv.extend(["--model", self.model])
        if readable_dirs:
            # --add-dir은 가변 인자라 뒤따르는 값을 모두 삼킨다. 맨 끝에 둔다.
            argv.extend(["--allowed-tools", READ_TOOL, "--add-dir"])
            argv.extend(str(directory) for directory in readable_dirs)
        return tuple(argv)

    def _write_log(
        self,
        tag: str,
        prompt: str,
        completed: subprocess.CompletedProcess,
        duration_ms: int,
    ) -> None:
        """호출 기록을 남긴다 (PRD F2-A4, NF-R1)."""
        if self.log_dir is None:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = self.log_dir / f"{stamp}-{next(_LOG_SEQUENCE):04d}-{tag}.json"
            payload = {
                "tag": tag,
                "duration_ms": duration_ms,
                "returncode": completed.returncode,
                "prompt": redact(prompt),
                "stdout": redact(completed.stdout)[:_LOG_STDOUT_LIMIT],
                "stderr": redact(completed.stderr)[:_LOG_STDERR_LIMIT],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:  # 로그 실패가 본 작업을 막아서는 안 된다
            logger.warning("LLM 호출 로그를 남기지 못했습니다: %s", error)


def _extract_result_text(completed: subprocess.CompletedProcess) -> str:
    """``--output-format json`` 응답에서 본문 텍스트를 꺼낸다."""
    stdout = (completed.stdout or "").strip()
    if completed.returncode != 0:
        # 종료 코드가 0이 아니면 stdout에 뭔가 있어도 답변으로 쓰지 않는다.
        # 오류 메시지가 코드로 둔갑해 파일에 기록되는 것을 막는다.
        detail = (completed.stderr or stdout or "").strip()[:300]
        raise LLMError(f"CLI가 실패했습니다(code={completed.returncode}): {detail}")
    if not stdout:
        raise LLMError("CLI가 빈 응답을 돌려주었습니다.")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # 형식이 바뀌었더라도 본문을 그대로 쓸 수 있으면 쓴다.
        return stdout

    if isinstance(payload, Mapping):
        if payload.get("is_error"):
            raise LLMError(f"CLI가 오류를 보고했습니다: {payload.get('result') or stdout[:200]}")
        result = payload.get("result")
        if isinstance(result, str):
            return result
    raise LLMError("CLI 응답에서 result 텍스트를 찾지 못했습니다.")


def extract_code_block(text: str) -> str:
    """응답에서 첫 코드 블록을 꺼낸다. 없으면 전체를 코드로 본다."""
    match = _FENCE_PATTERN.search(text)
    if match:
        code = match.group(1).strip("\n")
        if code.strip():
            return code
        raise LLMError("코드 블록이 비어 있습니다.")
    stripped = text.strip()
    if not stripped:
        raise LLMError("응답이 비어 있어 코드를 꺼낼 수 없습니다.")
    return stripped


def extract_json(text: str) -> dict[str, Any]:
    """응답에서 JSON 객체를 꺼낸다."""
    candidates: list[str] = []
    match = _FENCE_PATTERN.search(text)
    if match:
        candidates.append(match.group(1))
    candidates.append(text)

    for candidate in candidates:
        payload = _try_parse_object(candidate)
        if payload is not None:
            return payload
    raise LLMError("응답에서 JSON 객체를 찾지 못했습니다.")


def _try_parse_object(candidate: str) -> dict[str, Any] | None:
    text = candidate.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None
