"""기본 해시·문자열 비교와 문제에 명시된 실수 허용 오차 비교.

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

로컬 비교(:func:`outputs_match`)는 기본적으로 **양쪽을 모두 정규화**한다. Windows에서
파이썬이 텍스트 모드로 출력하면 ``\\n``이 ``\\r\\n``으로 바뀌어 돌아오는데,
저장되는 ``.out``은 항상 LF이므로 이 보정이 있어야 로컬 판정이 리눅스 채점기의
판정과 같아진다.

알려진 예외: 프로그램이 **의도적으로** 줄 중간에 CRLF를 출력하는 경우, 로컬은
정규화로 통과시키지만 리눅스 채점기는 오답으로 본다. 이런 출력은
:func:`has_crlf`로 탐지해 내보내기 단계에서 경고한다.

문제의 출력 설명에 절대/상대 허용 오차가 명시되면 예제·브루트포스·오답 대조는
그 기준으로 수치 토큰을 비교한다. 저장된 출력의 재현성·무결성과 md5 계산은
여전히 엄격하게 확인한다. 실수 채점은 대상 서버에도 별도 설정/SPJ가 필요하다.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
from typing import Any

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


@dataclass(frozen=True)
class OutputComparison:
    absolute: Decimal = Decimal(0)
    relative: Decimal = Decimal(0)

    @property
    def tolerant(self) -> bool:
        return self.absolute > 0 or self.relative > 0

    def to_dict(self) -> dict[str, str]:
        return {"mode": "float" if self.tolerant else "exact",
                "absolute_tolerance": str(self.absolute), "relative_tolerance": str(self.relative)}

    def describe(self) -> str:
        if not self.tolerant:
            return "문자열 비교(끝 공백·개행 정규화)"
        return f"실수 비교(절대 오차 {self.absolute}, 상대 오차 {self.relative})"


_NUMBER = re.compile(rb"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_NONFINITE = {b"nan", b"+nan", b"-nan", b"inf", b"+inf", b"-inf", b"infinity", b"+infinity", b"-infinity"}
_POWER = r"(?:(?:\d+(?:\.\d+)?)\s*(?:\\times|[×*])\s*)?10\s*\^?\s*-\s*\d{1,4}"
_DECIMAL = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_ERROR_RULE = re.compile(
    r"(?P<label>(?:절대|상대)(?:\s*(?:/|또는|혹은|및|와|과|나)\s*(?:절대|상대))?\s*오차"
    r"|(?:absolute|relative)(?:\s*(?:/|or|and)\s*(?:absolute|relative))?\s*error)"
    r"[^\d\n]{0,70}?(?P<value>" + _POWER + "|" + _DECIMAL + r")",
    re.IGNORECASE,
)


def comparison_for(problem: Any) -> OutputComparison:
    """출력 설명에 수치가 명시된 절대/상대 오차만 채택한다. 없으면 엄격 비교한다."""
    text = unicodedata.normalize("NFKC", problem.output_spec or "").replace("−", "-")
    text = text.replace("{", "").replace("}", "").replace("$", "")
    text = re.sub(r"(절대|상대)\s*오차\s*(또는|혹은|및|와|과|나|/)\s*(절대|상대)\s*오차",
                  r"\1/\3 오차", text)
    if re.search(r"허용(?:하|되)?지\s*않|오차\s*없이|no\s+(?:error|tolerance)", text, re.I):
        return OutputComparison()
    absolute, relative = [], []
    for match in _ERROR_RULE.finditer(text):
        token = re.sub(r"\s+", "", match["value"]).replace("\\times", "*").replace("×", "*")
        # 인식하지 못한 분수·지수식의 앞 숫자만 허용 오차로 채택하지 않는다.
        tail = text[match.end():]
        if tail.lstrip().startswith(("/", "^")) or tail.startswith(("e", "E")):
            continue
        try:
            power = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\*)?10\^?-(\d+)", token)
            value = Decimal(power[1] or 1) * Decimal(10) ** -int(power[2]) if power else Decimal(token)
        except DecimalException:
            continue
        if not value.is_finite() or not 0 < value <= 1:
            continue
        label = match["label"].lower()
        if "절대" in label or "absolute" in label:
            absolute.append(value)
        if "상대" in label or "relative" in label:
            relative.append(value)
    return OutputComparison(min(absolute, default=Decimal(0)), min(relative, default=Decimal(0)))


def outputs_match(expected: bytes, actual: bytes, *, comparison: OutputComparison | None = None) -> bool:
    """기본은 엄격 비교. 명시된 실수 오차가 있을 때만 숫자 토큰을 비교한다."""
    expected, actual = canonical_output(expected), canonical_output(actual)
    if comparison is None or not comparison.tolerant:
        return expected == actual
    expected_tokens, actual_tokens = expected.split(), actual.split()
    if len(expected_tokens) != len(actual_tokens):
        return False
    for left, right in zip(expected_tokens, actual_tokens):
        if left.lower() in _NONFINITE or right.lower() in _NONFINITE:
            return False
        if left == right:
            continue
        if not _NUMBER.fullmatch(left) or not _NUMBER.fullmatch(right):
            return False
        # 정수 카운트·인덱스에는 상대 오차를 적용하지 않는다.
        if not any(char in left.lower() + right.lower() for char in (b".", b"e")):
            return False
        try:
            with localcontext() as context:
                context.prec = max(50, len(left) + len(right) + 8)
                reference, value = Decimal(left.decode("ascii")), Decimal(right.decode("ascii"))
                error = abs(reference - value)
                if error > max(comparison.absolute, comparison.relative * abs(reference)):
                    return False
        except (DecimalException, UnicodeError):
            return False
    return True


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
