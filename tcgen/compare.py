"""QingdaoU OnlineJudge와 동일한 출력 비교 규칙.

해시는 서로 다른 두 지점에서 계산된다.

1. **업로드 시** (``OnlineJudge/problem/views/admin.py``, ``TestCaseZipProcessor``)
   zip에서 읽은 ``.out`` 내용의 CRLF를 LF로 치환한 뒤 ``bytes.rstrip()``한 값의
   md5를 ``stripped_output_md5``로 기록한다.

2. **채점 시** (``JudgeServer/server/judge_client.py``)
   제출 프로그램의 출력을 raw bytes로 읽어 ``bytes.rstrip()``한 값의 md5를 위
   값과 비교한다. **이쪽에는 CRLF 정규화가 없다.**

따라서 ``.out`` 파일을 LF로만 기록해야 두 경로의 해시가 일치한다(PRD F3-6).
이 모듈의 :func:`canonical_output`을 거쳐 기록하면 그 조건이 보장된다.

``rstrip()``은 파일 **끝** 공백만 제거한다. 키 이름이 ``stripped_``로 시작해
전체 공백을 무시할 것처럼 보이지만, 줄 중간의 잔여 공백이나 들여쓰기 차이는
그대로 오답 처리된다(PRD 5.3).

로컬 비교(:func:`outputs_match`)는 **양쪽을 모두 정규화**한다. Windows에서
파이썬이 텍스트 모드로 출력하면 ``\\n``이 ``\\r\\n``으로 바뀌어 돌아오는데,
저장되는 ``.out``은 항상 LF이므로 이 보정이 있어야 로컬 판정이 리눅스 채점기의
판정과 같아진다.

알려진 예외: 프로그램이 **의도적으로** 줄 중간에 CRLF를 출력하는 경우, 로컬은
정규화로 통과시키지만 리눅스 채점기는 오답으로 본다. 이런 출력은
:func:`has_crlf`로 탐지해 내보내기 단계에서 경고한다.
"""

from __future__ import annotations

import hashlib

CRLF = b"\r\n"
LF = b"\n"


def canonical_output(content: bytes) -> bytes:
    """``.out`` 파일에 기록할 정규형. CRLF를 LF로 바꾸고 끝 공백을 제거한다."""
    return content.replace(CRLF, LF).rstrip()


def expected_md5(content: bytes) -> str:
    """업로드 경로의 해시. 서버가 ``stripped_output_md5``에 기록하는 값."""
    return hashlib.md5(canonical_output(content)).hexdigest()


def judge_md5(content: bytes) -> str:
    """채점 경로의 해시. CRLF 정규화 없이 ``rstrip``만 적용한다."""
    return hashlib.md5(content.rstrip()).hexdigest()


def outputs_match(expected: bytes, actual: bytes) -> bool:
    """로컬 판정. 양쪽을 정규화해 비교한다(위 docstring 참조)."""
    return canonical_output(expected) == canonical_output(actual)


def has_crlf(content: bytes) -> bool:
    """CRLF 개행이 섞여 있는지. 기록 전 출력에 대한 진단용."""
    return CRLF in content


def lines_with_trailing_space(content: bytes) -> tuple[int, ...]:
    """줄 끝에 공백이 남은 줄 번호(1-based)를 돌려준다.

    파일 맨 끝 공백은 ``rstrip``으로 무시되므로 보고하지 않는다. 중간 줄의
    잔여 공백만 실제 오답 위험이다.
    """
    normalized = canonical_output(content)
    if not normalized:
        return ()
    lines = normalized.split(LF)
    return tuple(
        index
        for index, line in enumerate(lines, start=1)
        if index < len(lines) and line != line.rstrip()
    )
