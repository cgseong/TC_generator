"""로컬 실행기.

격리가 없다. Docker/WSL 실행기가 붙기 전까지 쓰는 1차 구현이며, UI는
"현재 격리 없음" 배너를 띄워야 한다 (PRD NF-S1).

안전장치:
- 시간 초과 시 **프로세스 트리 전체**를 종료한다 (NF-S2). 자식을 남기면
  다음 실행의 측정을 오염시킨다.
- 출력 상한을 넘기면 즉시 끊는다 (NF-S3).
- 작업디렉터리를 호출자가 지정한 폴더로 고정하고 환경변수를 최소화한다 (NF-S4).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

from tcgen.config import (
    CHILD_REFRESH_INTERVAL_S,
    KILL_GRACE_PERIOD_S,
    MEASUREMENT_POLLING,
    MEASUREMENT_UNAVAILABLE,
    MEMORY_POLL_INTERVAL_S,
    OUTPUT_LIMIT_BYTES,
    TIME_LIMIT_SLACK,
)
from tcgen.models import Limits
from tcgen.runner.base import RunResult, Verdict

logger = logging.getLogger(__name__)

try:  # psutil이 없으면 메모리는 '미측정'으로 처리한다.
    import psutil
except ImportError:  # pragma: no cover - 설치 환경에 따라 달라진다
    psutil = None  # type: ignore[assignment]

_READ_CHUNK = 64 * 1024
_KEEP_ENV_VARS = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "LANG", "LC_ALL")


def python_command(script_path: Path) -> tuple[str, ...]:
    """파이썬 스크립트 실행 명령.

    - ``-I``(isolated): 환경변수·사용자 site-packages·스크립트 디렉터리 자동
      삽입을 차단한다. 작업폴더에 놓인 ``json.py`` 같은 파일이 표준 모듈을
      가로채지 못하게 하려는 조치다.
    - ``-X utf8``: **반드시 명령줄로 줘야 한다.** ``-I``는 ``-E``를 함의해
      ``PYTHONIOENCODING`` 같은 ``PYTHON*`` 환경변수를 전부 무시하므로, 한국어
      Windows에서 stdout이 cp949가 된다. 그러면 비ASCII 출력이 cp949로
      ``N.out``에 기록되어 리눅스 채점기(UTF-8)와 해시가 어긋난다.
    - ``-B``: ``__pycache__``를 작업폴더에 남기지 않는다(같은 이유로
      ``PYTHONDONTWRITEBYTECODE``는 효과가 없다).
    """
    return (sys.executable, "-I", "-B", "-X", "utf8", str(script_path))


class _CappedReader(threading.Thread):
    """파이프를 끝까지 읽되 상한을 넘으면 중단 표시를 남긴다."""

    def __init__(self, stream, limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._buffer = bytearray()
        self.overflowed = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(_READ_CHUNK)
                if not chunk:
                    break
                if len(self._buffer) + len(chunk) > self._limit:
                    self._buffer.extend(chunk[: max(0, self._limit - len(self._buffer))])
                    self.overflowed = True
                    break
                self._buffer.extend(chunk)
        except (ValueError, OSError):  # 프로세스를 죽이면 파이프가 닫힌다
            pass
        finally:
            try:
                self._stream.close()
            except (ValueError, OSError):
                pass

    @property
    def data(self) -> bytes:
        return bytes(self._buffer)


class _MemorySampler:
    """프로세스 트리의 RSS 최대치를 추적한다.

    ``children(recursive=True)``는 Windows에서 프로세스 테이블 전체를 훑으므로
    매 폴링마다 부르면 측정 대상의 시간을 왜곡한다. 자식 목록은 낮은 주기로만
    갱신하고, 부모 RSS는 매 폴링마다 읽는다.
    """

    def __init__(self, pid: int) -> None:
        self._process = None
        self._children: tuple = ()
        self._last_refresh = 0.0
        self.peak_kb = 0
        if psutil is not None:
            try:
                self._process = psutil.Process(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._process = None

    def sample(self, now: float) -> int:
        if self._process is None:
            return self.peak_kb
        try:
            total = self._process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return self.peak_kb

        if now - self._last_refresh >= CHILD_REFRESH_INTERVAL_S:
            try:
                self._children = tuple(self._process.children(recursive=True))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._children = ()
            self._last_refresh = now

        for child in self._children:
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self.peak_kb = max(self.peak_kb, total // 1024)
        return self.peak_kb


class LocalRunner:
    """현재 PC에서 격리 없이 실행한다."""

    name = "local"
    isolated = False

    def run(
        self,
        argv: Sequence[str],
        stdin_path: Path | None,
        limits: Limits,
        cwd: Path | None = None,
        output_limit: int | None = None,
    ) -> RunResult:
        cap = output_limit if output_limit is not None else OUTPUT_LIMIT_BYTES
        measurement = MEASUREMENT_POLLING if psutil else MEASUREMENT_UNAVAILABLE
        stdin_file = None
        try:
            if stdin_path is not None:
                stdin_file = stdin_path.open("rb")
            process = subprocess.Popen(  # noqa: S603 - 실행이 이 도구의 목적이다
                list(argv),
                stdin=stdin_file or subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=_minimal_env(),
                creationflags=_creation_flags(),
            )
        except OSError as error:
            if stdin_file is not None:
                stdin_file.close()
            return RunResult(
                verdict=Verdict.RE,
                stderr=f"실행할 수 없습니다: {error}",
                measurement=measurement,
                isolated=self.isolated,
                note="프로세스 생성 실패",
            )

        try:
            return self._supervise(process, limits, cap, measurement)
        finally:
            # 감시 중 예기치 못한 예외가 나도 자식을 남기지 않는다.
            if process.poll() is None:
                _kill_process_tree(process)
            if stdin_file is not None:
                stdin_file.close()

    def _supervise(
        self,
        process: subprocess.Popen,
        limits: Limits,
        cap: int,
        measurement: str,
    ) -> RunResult:
        out_reader = _CappedReader(process.stdout, cap)
        err_reader = _CappedReader(process.stderr, cap)
        out_reader.start()
        err_reader.start()

        started = time.perf_counter()
        # 제한 직전에 끊지 않고 여유를 둔 뒤 실제 경과 시간으로 판정한다.
        deadline = started + (limits.time_ms / 1000.0) * TIME_LIMIT_SLACK
        memory_limit_kb = limits.memory_mb * 1024
        sampler = _MemorySampler(process.pid)
        killed_for: Verdict | None = None

        while process.poll() is None:
            now = time.perf_counter()
            peak_kb = sampler.sample(now)
            if memory_limit_kb and peak_kb > memory_limit_kb:
                killed_for = Verdict.MLE
            elif out_reader.overflowed or err_reader.overflowed:
                killed_for = Verdict.OLE
            elif now > deadline:
                killed_for = Verdict.TLE
            if killed_for is not None:
                _kill_process_tree(process)
                break
            time.sleep(MEMORY_POLL_INTERVAL_S)

        try:
            process.wait(timeout=KILL_GRACE_PERIOD_S * 4)
        except subprocess.TimeoutExpired:
            # 종료에 실패하면 여기서 영원히 기다리지 않는다. 기다리면 해당
            # 작업공간의 잡이 영구히 '실행 중'으로 잠긴다.
            logger.warning("프로세스가 종료되지 않았습니다: pid=%s", process.pid)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        out_reader.join(timeout=KILL_GRACE_PERIOD_S)
        err_reader.join(timeout=KILL_GRACE_PERIOD_S)
        truncated = out_reader.is_alive() or err_reader.is_alive()

        overflowed = out_reader.overflowed or err_reader.overflowed
        return RunResult(
            verdict=_decide_verdict(killed_for, overflowed, process.returncode, elapsed_ms, limits),
            stdout=out_reader.data,
            stderr=err_reader.data.decode("utf-8", errors="replace"),
            time_ms=elapsed_ms,
            memory_kb=sampler.peak_kb if psutil and sampler.peak_kb else None,
            exit_code=process.returncode,
            measurement=measurement,
            isolated=self.isolated,
            note=_note_for(killed_for, overflowed, truncated, measurement),
        )


def _decide_verdict(
    killed_for: Verdict | None,
    overflowed: bool,
    return_code: int | None,
    elapsed_ms: int,
    limits: Limits,
) -> Verdict:
    if killed_for is not None:
        return killed_for
    # 상한을 넘겨 읽기를 끊으면 파이프가 막혀 프로세스가 먼저 죽을 수 있다.
    # 그 경우의 비정상 종료는 출력 초과가 원인이므로 RE보다 우선한다.
    if overflowed:
        return Verdict.OLE
    # 스스로 비정상 종료한 경우는 '느리다'보다 '죽었다'가 더 중요한 사실이다.
    if return_code != 0:
        return Verdict.RE
    if elapsed_ms > limits.time_ms:
        return Verdict.TLE
    return Verdict.OK


def _note_for(
    killed_for: Verdict | None, overflowed: bool, truncated: bool, measurement: str
) -> str:
    notes: list[str] = []
    if killed_for is Verdict.MLE and measurement == MEASUREMENT_POLLING:
        notes.append("메모리는 폴링 측정이라 정밀도가 낮습니다(참고치).")
    if killed_for is Verdict.OLE or overflowed:
        notes.append("출력 상한을 초과해 중단했습니다.")
    if truncated:
        notes.append("출력을 끝까지 회수하지 못했습니다(잔류 프로세스 가능).")
    return " ".join(notes)


def _kill_process_tree(process: subprocess.Popen) -> None:
    """자식까지 모두 종료한다 (PRD NF-S2).

    psutil이 없는 환경에서는 Windows ``taskkill /T``로 대체한다. 둘 다
    실패하면 직계 자식만 종료된다는 사실을 로그로 남긴다.
    """
    if psutil is not None:
        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                _terminate_quietly(child)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            logger.debug("프로세스 트리 조회 실패: pid=%s", process.pid)
    elif sys.platform == "win32":  # pragma: no cover - psutil은 필수 의존성이다
        _taskkill_tree(process.pid)
    try:
        process.kill()
    except OSError:
        logger.debug("프로세스 종료 실패: pid=%s", process.pid)


def _taskkill_tree(pid: int) -> None:  # pragma: no cover - 폴백 경로
    try:
        subprocess.run(  # noqa: S603, S607 - 트리 종료 폴백
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            timeout=KILL_GRACE_PERIOD_S * 4,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("taskkill 실패: pid=%s (%s)", pid, error)


def _terminate_quietly(proc: "psutil.Process") -> None:
    try:
        proc.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def _minimal_env() -> dict[str, str]:
    """실행에 필요한 최소 환경변수만 넘긴다 (PRD NF-S4).

    ``PYTHON*`` 변수는 ``-I``가 전부 무시하므로 넣지 않는다. 인코딩과 바이트
    코드 생성은 :func:`python_command`의 명령줄 플래그로 제어한다.
    """
    return {name: os.environ[name] for name in _KEEP_ENV_VARS if name in os.environ}


def _creation_flags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return 0
