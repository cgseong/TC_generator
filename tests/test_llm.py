"""Claude CLI 연동 테스트 (PRD F2-A).

실제 CLI를 부르지 않고 러너를 주입해 검증한다.
"""

import json
import subprocess

import pytest

from tcgen.llm import ClaudeCLI, LLMError, extract_code_block, extract_json


def fake_completed(stdout: str, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def cli_result(text: str, *, is_error: bool = False) -> str:
    return json.dumps({"type": "result", "is_error": is_error, "result": text})


class RecordingRunner:
    """호출 인자를 기록하고 정해둔 응답을 돌려준다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, prompt, timeout_s):
        self.calls.append({"argv": list(argv), "prompt": prompt, "timeout": timeout_s})
        return self.responses.pop(0) if self.responses else fake_completed(cli_result("ok"))


class TestCommandConstruction:
    def test_tool_permissions_are_blocked(self, tmp_path):
        runner = RecordingRunner(fake_completed(cli_result("안녕")))
        ClaudeCLI(runner=runner).ask("테스트", tag="t")

        argv = runner.calls[0]["argv"]
        assert "--restricted" in argv
        assert "--strict-mcp-config" in argv
        assert "--no-session-persistence" in argv
        assert "--disallowed-tools" in argv
        disallowed = argv[argv.index("--disallowed-tools") + 1]
        for tool in ("Edit", "Write", "Read", "WebFetch"):
            assert tool in disallowed

    def test_headless_json_output(self):
        runner = RecordingRunner(fake_completed(cli_result("안녕")))
        ClaudeCLI(runner=runner).ask("테스트")
        argv = runner.calls[0]["argv"]
        assert "-p" in argv
        assert argv[argv.index("--output-format") + 1] == "json"

    def test_prompt_goes_through_stdin_not_argv(self):
        runner = RecordingRunner(fake_completed(cli_result("안녕")))
        ClaudeCLI(runner=runner).ask("아주 긴 지문입니다")
        assert runner.calls[0]["prompt"] == "아주 긴 지문입니다"
        assert "아주 긴 지문입니다" not in runner.calls[0]["argv"]

    def test_model_is_passed_when_set(self):
        runner = RecordingRunner(fake_completed(cli_result("안녕")))
        ClaudeCLI(model="sonnet", runner=runner).ask("테스트")
        argv = runner.calls[0]["argv"]
        assert argv[argv.index("--model") + 1] == "sonnet"

    def test_missing_executable_raises(self):
        with pytest.raises(LLMError, match="찾을 수 없습니다"):
            ClaudeCLI(executable="존재하지-않는-claude").ask("테스트")


class TestResponseParsing:
    def test_result_text_is_returned(self):
        runner = RecordingRunner(fake_completed(cli_result("정답입니다")))
        assert ClaudeCLI(runner=runner).ask("x").text == "정답입니다"

    def test_error_flag_raises(self):
        runner = RecordingRunner(fake_completed(cli_result("한도 초과", is_error=True)))
        with pytest.raises(LLMError, match="오류"):
            ClaudeCLI(runner=runner).ask("x")

    def test_empty_output_raises(self):
        runner = RecordingRunner(fake_completed(""))
        with pytest.raises(LLMError):
            ClaudeCLI(runner=runner).ask("x")

    def test_nonzero_exit_without_output_raises(self):
        runner = RecordingRunner(fake_completed("", returncode=1, stderr="죽었습니다"))
        with pytest.raises(LLMError, match="실패"):
            ClaudeCLI(runner=runner).ask("x")

    def test_timeout_raises_llm_error(self):
        def timing_out(argv, prompt, timeout_s):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout_s)

        with pytest.raises(LLMError, match="초를 넘겼습니다"):
            ClaudeCLI(runner=timing_out).ask("x")


class TestExtraction:
    def test_code_block_is_extracted(self):
        text = "설명입니다.\n```python\nprint(1)\n```\n끝"
        assert extract_code_block(text) == "print(1)"

    def test_bare_text_is_treated_as_code(self):
        assert extract_code_block("print(1)") == "print(1)"

    def test_empty_response_raises(self):
        with pytest.raises(LLMError):
            extract_code_block("   ")

    def test_json_in_fence_is_extracted(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_bare_json_is_extracted(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_json_surrounded_by_prose_is_extracted(self):
        assert extract_json('다음과 같습니다.\n{"a": [1, 2]}\n이상입니다.') == {"a": [1, 2]}

    def test_non_json_raises(self):
        with pytest.raises(LLMError):
            extract_json("JSON이 없습니다")


class TestRetry:
    def test_retries_until_parseable(self):
        runner = RecordingRunner(
            fake_completed(cli_result("형식이 틀렸습니다")),
            fake_completed(cli_result('```json\n{"ok": true}\n```')),
        )
        assert ClaudeCLI(runner=runner).ask_json("x") == {"ok": True}
        assert len(runner.calls) == 2

    def test_gives_up_after_max_retries(self):
        runner = RecordingRunner(*[fake_completed(cli_result("쓰레기"))] * 3)
        with pytest.raises(LLMError, match="해석에 실패"):
            ClaudeCLI(max_retries=2, runner=runner).ask_json("x")
        assert len(runner.calls) == 3


class TestLogging:
    def test_call_is_logged(self, tmp_path):
        runner = RecordingRunner(fake_completed(cli_result("안녕")))
        ClaudeCLI(log_dir=tmp_path, runner=runner).ask("프롬프트", tag="solution")

        logs = list(tmp_path.glob("*-solution.json"))
        assert len(logs) == 1
        payload = json.loads(logs[0].read_text(encoding="utf-8"))
        assert payload["prompt"] == "프롬프트"
        assert payload["tag"] == "solution"


class TestCliFailureDetails:
    def test_nonzero_json_reports_actual_result_after_large_metadata(self):
        payload = {
            "usage": {"padding": "x" * 1000}, "session_id": "irrelevant-session",
            "is_error": True, "api_error_status": 403,
            "api_error_code": "oauth_not_allowed_for_organization",
            "result": "Your organization has disabled Claude subscription access for Claude Code",
        }
        runner = RecordingRunner(fake_completed(json.dumps(payload), returncode=1, stderr="unrelated warning"))
        with pytest.raises(LLMError) as caught:
            ClaudeCLI(runner=runner).ask("x")
        message = str(caught.value)
        assert "Your organization has disabled" in message
        assert "HTTP 403" in message and "oauth_not_allowed_for_organization" in message
        assert "claude auth login" in message
        assert "irrelevant-session" not in message and "padding" not in message

    @pytest.mark.parametrize("fields, expected", [
        ({"error": {"message": "server unavailable"}}, "server unavailable"),
        ({"error": "authentication failed"}, "authentication failed"),
        ({"errors": ["first error", {"message": "second error"}]}, "first error · second error"),
    ])
    def test_alternate_error_shapes(self, fields, expected):
        runner = RecordingRunner(fake_completed(json.dumps(fields), returncode=1))
        with pytest.raises(LLMError, match=expected):
            ClaudeCLI(runner=runner).ask("x")

    def test_failed_call_is_logged_before_exception(self, tmp_path):
        secret = "sk-ant-verysecretcredential1234567890"
        payload = {"usage": {"padding": "x" * 21000}, "is_error": True,
                   "api_error_code": "authentication_error", "api_error_status": 401,
                   "result": f"Invalid API key: {secret}"}
        runner = RecordingRunner(fake_completed(json.dumps(payload), returncode=1))
        with pytest.raises(LLMError) as caught:
            ClaudeCLI(runner=runner, log_dir=tmp_path).ask("test", tag="failed")
        assert secret not in str(caught.value)
        logs = list(tmp_path.glob("*-failed.json"))
        assert len(logs) == 1
        log = json.loads(logs[0].read_text())
        assert log["returncode"] == 1
        assert "Invalid API key" in log["error"] and "claude auth login" in log["error"]
        assert secret not in logs[0].read_text()

    def test_zero_exit_error_flag_is_also_logged(self, tmp_path):
        runner = RecordingRunner(fake_completed(cli_result("permission denied", is_error=True)))
        with pytest.raises(LLMError):
            ClaudeCLI(runner=runner, log_dir=tmp_path).ask("x", tag="flag")
        data = json.loads(next(tmp_path.glob("*-flag.json")).read_text())
        assert data["error"] == "permission denied"

    def test_non_json_error_still_uses_stderr(self):
        runner = RecordingRunner(fake_completed("unusable stdout", returncode=1, stderr="actual stderr error"))
        with pytest.raises(LLMError, match="actual stderr error"):
            ClaudeCLI(runner=runner).ask("x")
