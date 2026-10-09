"""로컬 실행기 동작 테스트 (PRD NF-S2, NF-S3, F2-D2, F2-D3)."""

import textwrap

import pytest

try:
    import psutil  # noqa: F401

    HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    HAS_PSUTIL = False

from tcgen.models import Limits
from tcgen.runner import LocalRunner, Verdict, python_command


def write_script(tmp_path, body: str):
    path = tmp_path / "script.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


@pytest.fixture
def runner():
    return LocalRunner()


class TestNormalExecution:
    def test_echo_sum_from_stdin(self, runner, tmp_path):
        script = write_script(
            tmp_path,
            """
            a, b = map(int, input().split())
            print(a + b)
            """,
        )
        stdin = tmp_path / "1.in"
        stdin.write_bytes(b"1 2\n")

        result = runner.run(python_command(script), stdin, Limits(time_ms=5000))

        assert result.verdict is Verdict.OK
        assert result.stdout.strip() == b"3"
        assert result.exit_code == 0

    def test_reports_not_isolated(self, runner, tmp_path):
        script = write_script(tmp_path, "print(1)")
        result = runner.run(python_command(script), None, Limits(time_ms=5000))
        assert result.isolated is False

    def test_runtime_error_is_re(self, runner, tmp_path):
        script = write_script(tmp_path, "raise SystemExit(3)")
        result = runner.run(python_command(script), None, Limits(time_ms=5000))
        assert result.verdict is Verdict.RE
        assert result.exit_code == 3

    def test_missing_executable_is_re_not_crash(self, runner, tmp_path):
        result = runner.run(["존재하지-않는-실행파일"], None, Limits(time_ms=1000))
        assert result.verdict is Verdict.RE
        assert "실행할 수 없습니다" in result.stderr


class TestLimits:
    def test_infinite_loop_is_tle(self, runner, tmp_path):
        script = write_script(
            tmp_path,
            """
            while True:
                pass
            """,
        )
        result = runner.run(python_command(script), None, Limits(time_ms=200))
        assert result.verdict is Verdict.TLE

    def test_slow_but_finishing_run_is_tle(self, runner, tmp_path):
        script = write_script(
            tmp_path,
            """
            import time
            time.sleep(0.3)
            print("done")
            """,
        )
        result = runner.run(python_command(script), None, Limits(time_ms=100))
        assert result.verdict is Verdict.TLE
        assert result.time_ms >= 100

    def test_output_limit_is_ole(self, runner, tmp_path):
        script = write_script(
            tmp_path,
            """
            import sys
            while True:
                sys.stdout.write("x" * 4096)
            """,
        )
        result = runner.run(
            python_command(script), None, Limits(time_ms=5000), output_limit=8192
        )
        assert result.verdict is Verdict.OLE

    @pytest.mark.skipif(not HAS_PSUTIL, reason="psutil이 없으면 메모리를 측정할 수 없다")
    def test_large_allocation_is_mle(self, runner, tmp_path):
        script = write_script(
            tmp_path,
            """
            import time
            blob = bytearray(300 * 1024 * 1024)
            time.sleep(2)
            print(len(blob))
            """,
        )
        result = runner.run(python_command(script), None, Limits(time_ms=5000, memory_mb=64))
        assert result.verdict is Verdict.MLE
        assert "참고치" in result.note


class TestMeasurement:
    def test_measurement_method_is_recorded(self, runner, tmp_path):
        script = write_script(tmp_path, "print(1)")
        result = runner.run(python_command(script), None, Limits(time_ms=5000))
        assert result.measurement in {"polling-lowres", "unavailable"}

