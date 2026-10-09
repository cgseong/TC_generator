"""정적 위험 패턴 스캔 테스트 (PRD NF-S5)."""

from tcgen import safety


def rules_of(source: str) -> set[str]:
    return {finding.rule for finding in safety.scan_python_source(source)}


class TestBlockedImports:
    def test_subprocess_is_blocked(self):
        findings = safety.scan_python_source("import subprocess")
        assert safety.has_blocking(findings)
        assert findings[0].rule == "blocked-import"

    def test_from_import_is_blocked(self):
        assert safety.has_blocking(safety.scan_python_source("from shutil import rmtree"))

    def test_submodule_import_is_blocked(self):
        assert safety.has_blocking(safety.scan_python_source("import urllib.request"))

    def test_os_import_is_allowed(self):
        # 빠른 입출력(os.read)을 위해 흔히 쓰므로 모듈 자체는 막지 않는다.
        assert not safety.has_blocking(safety.scan_python_source("import os, sys"))

    def test_stdlib_algorithm_modules_are_allowed(self):
        source = "import sys, math, heapq, bisect, collections, itertools, random"
        assert safety.scan_python_source(source) == ()


class TestDangerousCalls:
    def test_os_system_is_blocked(self):
        findings = safety.scan_python_source("import os\nos.system('dir')")
        assert safety.has_blocking(findings)
        assert "dangerous-os-call" in rules_of("import os\nos.system('dir')")

    def test_os_remove_is_blocked(self):
        assert safety.has_blocking(safety.scan_python_source("import os\nos.remove('x')"))

    def test_os_read_is_allowed(self):
        assert not safety.has_blocking(
            safety.scan_python_source("import os\ndata = os.read(0, 100)")
        )


class TestWarnings:
    def test_eval_is_blocked(self):
        # 동적 실행은 정적 분석을 무력화하므로 경고가 아니라 차단이다.
        findings = safety.scan_python_source("eval('1+1')")
        assert safety.has_blocking(findings)
        assert findings[0].rule == "dynamic-execution"

    def test_write_mode_open_is_warned(self):
        findings = safety.scan_python_source("open('out.txt', 'w')")
        assert "file-write" in {finding.rule for finding in findings}
        assert not safety.has_blocking(findings)

    def test_read_mode_open_is_clean(self):
        assert safety.scan_python_source("open('in.txt', 'r')") == ()

    def test_keyword_mode_is_detected(self):
        assert "file-write" in rules_of("open('out.txt', mode='a')")


class TestCleanSolution:
    def test_typical_solution_has_no_findings(self):
        source = """
import sys

def main() -> None:
    data = sys.stdin.read().split()
    print(int(data[0]) + int(data[1]))

main()
"""
        assert safety.scan_python_source(source) == ()


class TestSyntaxError:
    def test_unparsable_source_blocks(self):
        findings = safety.scan_python_source("def broken(:\n    pass")
        assert safety.has_blocking(findings)
        assert findings[0].rule == "syntax-error"


class TestSummary:
    def test_summary_counts_both_levels(self):
        findings = safety.scan_python_source("import socket\nopen('x.txt', 'w')")
        assert safety.summarize(findings) == "차단 1건, 경고 1건이 발견되었습니다."

    def test_summary_for_clean_source(self):
        assert safety.summarize(()) == "위험 패턴이 발견되지 않았습니다."
