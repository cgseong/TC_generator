"""실행 전 정적 위험 패턴 스캔 (PRD NF-S5).

LLM이 만든 코드도 신뢰하지 않는다. Docker 격리가 없는 동안 이 스캔이 유일한
방어선이므로, 걸리면 실행을 멈추고 사람에게 확인을 받는다.

**완전한 샌드박스가 아니다.** 목적은 '악의적 코드 차단'이 아니라 '사고성 파괴
행위를 실행 전에 드러내기'다. 다만 다음 세 가지는 평범한 파이썬 관용구라
반드시 잡아야 한다.

- ``import os as o`` / ``from os import system`` — 별칭과 직접 바인딩
- ``pathlib.Path(...).unlink()`` — ``shutil``을 막아도 남는 삭제 경로
- PEP-263 인코딩 선언 — 스캐너(``str``)와 인터프리터(파일 바이트)가 **서로 다른
  소스를 보게** 만들어 스캔 전체를 무력화할 수 있다
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Sequence

SEVERITY_BLOCK = "block"
SEVERITY_WARN = "warn"

#: 코딩테스트 정답 코드가 쓸 이유가 없는 모듈들
BLOCKED_MODULES: frozenset[str] = frozenset(
    {
        "subprocess",
        "socket",
        "shutil",
        "ctypes",
        "multiprocessing",
        "requests",
        "urllib",
        "http",
        "ftplib",
        "smtplib",
        "telnetlib",
        "webbrowser",
        "winreg",
        "pty",
        "importlib",
        "pickle",
        "marshal",
    }
)

#: ``os`` 자체는 빠른 입출력(``os.read``)에 흔히 쓰이므로 위험한 호출만 막는다.
BLOCKED_OS_CALLS: frozenset[str] = frozenset(
    {
        "system",
        "popen",
        "remove",
        "unlink",
        "rmdir",
        "removedirs",
        "rename",
        "replace",
        "chmod",
        "chown",
        "kill",
        "killpg",
        "startfile",
        "fork",
        "execv",
        "execve",
        "execl",
        "spawnl",
        "spawnv",
        "setuid",
        "truncate",
    }
)

#: 객체가 무엇이든 이 이름으로 호출하면 파괴적이다 (주로 pathlib).
BLOCKED_METHOD_CALLS: frozenset[str] = frozenset(
    {"unlink", "rmtree", "rmdir", "write_text", "write_bytes", "chmod"}
)

#: 정적 분석을 무력화하는 동적 실행. 정답 코드가 쓸 이유가 없어 차단한다.
BLOCKED_BUILTINS: frozenset[str] = frozenset({"eval", "exec", "compile", "__import__"})

_WRITE_MODES = ("w", "a", "x", "+")

#: PEP-263 인코딩 선언. 1~2행에만 유효하다.
_CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
_ALLOWED_ENCODINGS = frozenset({"utf-8", "utf8", "ascii", "us-ascii"})


@dataclass(frozen=True)
class Finding:
    """위험 패턴 한 건."""

    severity: str
    rule: str
    line: int
    detail: str

    @property
    def is_blocking(self) -> bool:
        return self.severity == SEVERITY_BLOCK


def scan_python_source(source: str) -> tuple[Finding, ...]:
    """파이썬 소스에서 위험 패턴을 찾는다."""
    cookie = _encoding_cookie_finding(source)
    if cookie is not None:
        return (cookie,)

    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return (
            Finding(
                severity=SEVERITY_BLOCK,
                rule="syntax-error",
                line=error.lineno or 0,
                detail=f"구문 오류로 분석할 수 없습니다: {error.msg}",
            ),
        )
    except ValueError as error:
        # NUL 바이트 등은 SyntaxError가 아니라 ValueError로 올라온다.
        return (
            Finding(
                severity=SEVERITY_BLOCK,
                rule="unparsable-source",
                line=0,
                detail=f"소스를 분석할 수 없습니다: {error}",
            ),
        )

    scanner = _Scanner()
    scanner.visit(tree)
    return tuple(sorted(scanner.findings, key=lambda item: (item.line, item.rule)))


def has_blocking(findings: Sequence[Finding]) -> bool:
    return any(finding.is_blocking for finding in findings)


def summarize(findings: Sequence[Finding]) -> str:
    """사용자에게 보여줄 한 줄 요약."""
    if not findings:
        return "위험 패턴이 발견되지 않았습니다."
    blocking = sum(1 for finding in findings if finding.is_blocking)
    return f"차단 {blocking}건, 경고 {len(findings) - blocking}건이 발견되었습니다."


class _Scanner(ast.NodeVisitor):
    """import 별칭을 추적하면서 위험 호출을 찾는다."""

    def __init__(self) -> None:
        self.findings: list[Finding] = []
        #: ``os``를 가리키는 이름들. ``import os as o``면 ``{"os", "o"}``.
        self.os_aliases: set[str] = set()
        #: ``from os import system``으로 직접 바인딩된 위험 함수 이름들.
        self.bound_os_calls: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = _root_module(alias.name)
            if root in BLOCKED_MODULES:
                self.findings.append(_module_finding(alias.name, node.lineno))
            if root == "os":
                self.os_aliases.add(alias.asname or alias.name.split(".", 1)[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = _root_module(module)
        if root in BLOCKED_MODULES:
            self.findings.append(_module_finding(module, node.lineno))
        elif root == "os":
            for alias in node.names:
                if alias.name in BLOCKED_OS_CALLS:
                    name = alias.asname or alias.name
                    self.bound_os_calls.add(name)
                    self.findings.append(
                        Finding(
                            severity=SEVERITY_BLOCK,
                            rule="dangerous-os-call",
                            line=node.lineno,
                            detail=f"from os import {alias.name} 은 파일·프로세스를 조작할 수 있습니다.",
                        )
                    )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.findings.extend(self._inspect_call(node))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # sys.modules["os"] 로 차단을 우회하는 경로
        value = node.value
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "modules"
            and isinstance(value.value, ast.Name)
            and value.value.id == "sys"
        ):
            self.findings.append(
                Finding(
                    severity=SEVERITY_BLOCK,
                    rule="module-table-access",
                    line=node.lineno,
                    detail="sys.modules 직접 접근은 모듈 차단을 우회합니다.",
                )
            )
        self.generic_visit(node)

    def _inspect_call(self, node: ast.Call) -> list[Finding]:
        func = node.func
        if isinstance(func, ast.Attribute):
            return self._inspect_attribute_call(node, func)
        if isinstance(func, ast.Name):
            return self._inspect_name_call(node, func)
        return []

    def _inspect_attribute_call(self, node: ast.Call, func: ast.Attribute) -> list[Finding]:
        owner = func.value
        if (
            isinstance(owner, ast.Name)
            and owner.id in self.os_aliases
            and func.attr in BLOCKED_OS_CALLS
        ):
            return [
                Finding(
                    severity=SEVERITY_BLOCK,
                    rule="dangerous-os-call",
                    line=node.lineno,
                    detail=f"{owner.id}.{func.attr}() 호출은 파일·프로세스를 조작할 수 있습니다.",
                )
            ]
        if func.attr in BLOCKED_METHOD_CALLS:
            return [
                Finding(
                    severity=SEVERITY_BLOCK,
                    rule="destructive-method",
                    line=node.lineno,
                    detail=f".{func.attr}() 호출은 파일을 지우거나 덮어씁니다.",
                )
            ]
        return []

    def _inspect_name_call(self, node: ast.Call, func: ast.Name) -> list[Finding]:
        if func.id in BLOCKED_BUILTINS:
            return [
                Finding(
                    severity=SEVERITY_BLOCK,
                    rule="dynamic-execution",
                    line=node.lineno,
                    detail=f"{func.id}() 는 정적 분석을 무력화합니다.",
                )
            ]
        if func.id in self.bound_os_calls:
            return [
                Finding(
                    severity=SEVERITY_BLOCK,
                    rule="dangerous-os-call",
                    line=node.lineno,
                    detail=f"{func.id}() 는 os 모듈에서 직접 가져온 위험 함수입니다.",
                )
            ]
        if func.id == "getattr" and _targets_os(node, self.os_aliases):
            return [
                Finding(
                    severity=SEVERITY_BLOCK,
                    rule="dynamic-attribute",
                    line=node.lineno,
                    detail="getattr로 os 속성을 꺼내는 것은 차단을 우회합니다.",
                )
            ]
        if func.id == "open" and _opens_for_writing(node):
            return [
                Finding(
                    severity=SEVERITY_WARN,
                    rule="file-write",
                    line=node.lineno,
                    detail="쓰기 모드로 파일을 엽니다. 작업폴더 밖을 건드리지 않는지 확인하세요.",
                )
            ]
        return []


def _encoding_cookie_finding(source: str) -> Finding | None:
    """UTF-8이 아닌 인코딩 선언을 막는다.

    ``write_script``는 ``str``을 검사한 뒤 UTF-8 바이트로 기록하지만,
    인터프리터는 파일을 쿠키가 지정한 인코딩으로 다시 디코딩한다. 둘이
    달라지면 스캐너가 본 소스와 실제 실행되는 소스가 어긋난다.
    """
    for index, line in enumerate(source.splitlines()[:2], start=1):
        match = _CODING_COOKIE.match(line)
        if match is None:
            continue
        encoding = match.group(1).lower().replace("_", "-")
        if encoding not in _ALLOWED_ENCODINGS:
            return Finding(
                severity=SEVERITY_BLOCK,
                rule="encoding-cookie",
                line=index,
                detail=(
                    f"'{match.group(1)}' 인코딩 선언은 정적 분석과 실제 실행을 어긋나게 합니다."
                ),
            )
    return None


def _targets_os(node: ast.Call, os_aliases: set[str]) -> bool:
    return bool(
        node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in os_aliases
    )


def _opens_for_writing(node: ast.Call) -> bool:
    mode = _literal_mode(node)
    if mode is None:
        # 모드가 변수면 판단할 수 없다. 보수적으로 경고한다.
        return len(node.args) >= 2 or any(keyword.arg == "mode" for keyword in node.keywords)
    return any(flag in mode for flag in _WRITE_MODES)


def _literal_mode(node: ast.Call) -> str | None:
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        value = node.args[1].value
        return value if isinstance(value, str) else None
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, str) else None
    return None


def _module_finding(module: str, line: int) -> Finding:
    return Finding(
        severity=SEVERITY_BLOCK,
        rule="blocked-import",
        line=line,
        detail=f"'{module}' 모듈은 코딩테스트 정답 코드가 쓸 이유가 없습니다.",
    )


def _root_module(name: str) -> str:
    return name.split(".", 1)[0]
