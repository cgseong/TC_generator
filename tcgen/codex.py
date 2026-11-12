"""ChatGPT 구독 인증을 재사용하는 Codex CLI 어댑터.

빈 임시 폴더·읽기 전용 sandbox에서 텍스트와 직접 첨부한 그림만 전달한다.
사용자 설정·플러그인·MCP·명령 실행을 사용하지 않으며 API 인증으로 전환하지 않는다.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Sequence

from tcgen.llm import ClaudeCLI, CommandRunner, LLMError, LLMResponse, TokenUsage, _token_count, extract_json, redact
from tcgen.pricing import gpt_cost

# 앱이 실행·파일 기록을 담당하므로 모델에 실행·연결 도구를 제공하지 않는다.
_DISABLED_FEATURES = (
    "shell_tool", "code_mode_host", "plugins", "apps", "tool_suggest", "hooks",
    "multi_agent", "skill_search", "browser_use", "computer_use", "in_app_browser",
    "image_generation", "view_image", "sleep_tool", "goals", "memories",
)
_MAX_IMAGES = 20


def resolve_codex_executable(executable: str = "codex") -> str | None:
    found = shutil.which(executable)
    if found or executable != "codex":
        return found
    # IDE에 포함된 CLI도 사용한다. 터미널 PATH가 IDE PATH와 달라도 동작한다.
    system = platform.system()
    architecture = "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
    target = {"Darwin": f"macos-{architecture}", "Linux": f"linux-{architecture}",
              "Windows": f"windows-{architecture}"}.get(system)
    filename = "codex.exe" if system == "Windows" else "codex"
    candidates = []
    for folder in (".vscode", ".vscode-insiders"):
        root = Path.home() / folder / "extensions"
        if target and root.is_dir():
            candidates.extend(root.glob(f"openai.chatgpt-*/bin/{target}/{filename}"))
    if system == "Darwin":
        candidates.extend(Path(f"/Applications/{app}.app/Contents/Resources/codex") for app in ("ChatGPT", "Codex"))
    candidates = [path for path in candidates if path.is_file() and os.access(path, os.X_OK)]
    return str(max(candidates, key=lambda path: path.stat().st_mtime_ns)) if candidates else None


def _run_codex(argv: Sequence[str], prompt: str, timeout_s: float) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_THREAD_ID"):
        env.pop(key, None)
    return subprocess.run(list(argv), input=prompt, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout_s, check=False, env=env)


def codex_auth_status(executable: str | None = None, *, runner: CommandRunner = _run_codex) -> dict:
    resolved = resolve_codex_executable(executable or "codex")
    if not resolved:
        return {"ready": False, "method": "none", "message": "Codex CLI가 없습니다. 설치 후 `codex login`으로 ChatGPT에 로그인해 주세요."}
    try:
        result = runner((resolved, "login", "status"), "", 10)
    except (OSError, subprocess.SubprocessError):
        return {"ready": False, "method": "unknown", "message": "Codex 로그인 상태를 확인하지 못했습니다. 터미널에서 `codex login status`를 확인해 주세요."}
    output = f"{result.stdout or ''}\n{result.stderr or ''}".casefold()
    if result.returncode == 0 and "chatgpt" in output:
        return {"ready": True, "method": "chatgpt", "message": "ChatGPT 계정 로그인됨 · 구독의 Codex 사용 범위로 실행합니다."}
    if result.returncode == 0:
        return {"ready": False, "method": "api", "message": "구독 대신 API 인증이 설정되어 있습니다. `codex login`에서 Sign in with ChatGPT를 선택해 주세요."}
    return {"ready": False, "method": "none", "message": "ChatGPT 로그인이 필요합니다. 터미널에서 `codex login`을 실행해 주세요."}


def _extract_codex_text(completed: subprocess.CompletedProcess) -> str:
    messages, errors = [], []
    for line in (completed.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in {"error", "turn.failed"}:
            error = event.get("error") or event.get("message") or "Codex 요청이 실패했습니다."
            errors.append(str(error.get("message", error)) if isinstance(error, dict) else str(error))
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
            elif item.get("type") in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
                errors.append("텍스트 생성 중 허용되지 않은 도구 사용이 감지되었습니다.")
    if completed.returncode != 0 or errors:
        detail = " · ".join(dict.fromkeys(errors)) or completed.stderr or completed.stdout or "오류 설명이 없습니다."
        raise LLMError(f"Codex CLI가 실패했습니다(code={completed.returncode}): {redact(detail)[:1800]}")
    if not messages or not messages[-1].strip():
        raise LLMError("Codex 응답에서 최종 답변을 찾지 못했습니다. Codex CLI 버전과 로그를 확인해 주세요.")
    return messages[-1]


def _record_codex_usage(usage: TokenUsage, stdout: str | None, model: str | None) -> None:
    # Codex는 turn.completed에 토큰만 알린다. 모델은 지정한 값으로, 비용은 공개 단가로 계산한다.
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            stats = event["usage"]
            tokens = {"input": _token_count(stats.get("input_tokens")),
                      "cached": _token_count(stats.get("cached_input_tokens")),
                      "output": _token_count(stats.get("output_tokens"))}
            usage.add(**tokens, cost_usd=gpt_cost(model, tokens["input"], tokens["cached"], tokens["output"]),
                      models=[model] if model else [])


def _prepare_images(paths: Sequence[Path], folder: Path) -> tuple[Path, ...]:
    from tcgen.images import normalize
    if not paths:
        raise LLMError("읽을 파일이 지정되지 않았습니다.")
    result = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            raise LLMError(f"파일을 찾을 수 없습니다: {path}")
        if path.suffix.lower() == ".pdf":
            # AVIF/Pillow와 MuPDF의 네이티브 라이브러리 충돌이 서버를 종료하지 않게 분리한다.
            try:
                rendered = subprocess.run(
                    [sys.executable, "-I", "-B", str(Path(__file__).with_name("_pdf_render.py")),
                     str(path.resolve()), str(folder), str(len(result)), str(_MAX_IMAGES - len(result))],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                    check=False,
                )
                info = json.loads(rendered.stdout)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                raise LLMError(f"PDF 페이지를 변환하지 못했습니다: {path.name}") from error
            if rendered.returncode != 0 or info.get("error"):
                raise LLMError(f"PDF 페이지 변환 실패: {info.get('error') or rendered.stderr[:300]}")
            for name in info.get("images", []):
                image = folder / name
                if Path(name).name != name or not image.is_file():
                    raise LLMError("PDF 변환 결과 파일이 올바르지 않습니다.")
                result.append(image)
        else:
            output = folder / f"image-{len(result) + 1}.png"
            try:
                output.write_bytes(normalize(path.read_bytes()))
            except (OSError, ValueError) as error:
                raise LLMError(f"그림을 읽지 못했습니다: {path.name}: {error}") from error
            result.append(output)
        if len(result) > _MAX_IMAGES:
            raise LLMError(f"한 번에 첨부할 수 있는 그림은 {_MAX_IMAGES}개입니다.")
    return tuple(result)


@dataclass(frozen=True)
class CodexCLI(ClaudeCLI):
    provider: ClassVar[str] = "codex"
    executable: str = "codex"
    runner: CommandRunner = field(default=_run_codex, repr=False)
    auth_runner: CommandRunner = field(default=_run_codex, repr=False)

    def _codex_argv(self, folder: Path, images: Sequence[Path] = ()) -> tuple[str, ...]:
        executable = resolve_codex_executable(self.executable)
        if executable is None:
            raise LLMError("Codex CLI를 찾지 못했습니다. 설치 후 `codex login`으로 ChatGPT에 로그인해 주세요.")
        argv = [executable, "exec", "--json", "--ephemeral", "--ignore-user-config",
                "--skip-git-repo-check", "--sandbox", "read-only", "--cd", str(folder)]
        for feature in _DISABLED_FEATURES:
            argv.extend(["--disable", feature])
        for setting in ('approval_policy="never"', 'web_search="disabled"', "project_doc_max_bytes=0",
                        "tools.view_image=false", "mcp_servers={}"):
            argv.extend(["-c", setting])
        if self.model:
            argv.extend(["--model", self.model])
        if self.effort:
            argv.extend(["-c", f'model_reasoning_effort="{self.effort}"'])
        for path in images:
            argv.append(f"--image={path}")
        argv.append("-")
        return tuple(argv)

    def _ask_codex(self, prompt: str, *, tag: str, images: Sequence[Path] = ()) -> LLMResponse:
        status = codex_auth_status(self.executable, runner=self.auth_runner)
        if not status["ready"]:
            raise LLMError(status["message"])
        with tempfile.TemporaryDirectory(prefix="tcgen-codex-") as folder:
            argv = self._codex_argv(Path(folder), images)
            started = time.perf_counter()
            try:
                completed = self.runner(argv, prompt, self.timeout_s)
            except subprocess.TimeoutExpired as error:
                raise LLMError(f"Codex CLI 응답이 {self.timeout_s:.0f}초를 넘겼습니다.") from error
            except OSError as error:
                raise LLMError(f"Codex CLI를 실행할 수 없습니다: {error}") from error
            duration = int((time.perf_counter() - started) * 1000)
            _record_codex_usage(self.usage, completed.stdout, self.model)
            try:
                text = _extract_codex_text(completed)
            except LLMError as error:
                self._write_log(tag, prompt, completed, duration, error_detail=str(error))
                raise
            self._write_log(tag, prompt, completed, duration)
            return LLMResponse(text=text, duration_ms=duration, tag=tag)

    def ask(self, prompt: str, *, tag: str = "ask", readable_dirs: Sequence[Path] = ()) -> LLMResponse:
        if readable_dirs:
            raise LLMError("GPT의 파일 입력은 ask_json_about_files로 직접 첨부해 주세요.")
        return self._ask_codex(prompt, tag=tag)

    def ask_json_about_files(self, prompt: str, paths: Sequence[Path], *, tag: str = "file") -> dict:
        with tempfile.TemporaryDirectory(prefix="tcgen-codex-images-") as folder:
            images = _prepare_images(paths, Path(folder))
            for attempt in range(self.max_retries + 1):
                response = self._ask_codex(prompt, tag=f"{tag}-{attempt + 1}", images=images)
                try:
                    return extract_json(response.text)
                except LLMError as error:
                    if attempt == self.max_retries:
                        raise LLMError(f"{attempt + 1}회 시도했지만 JSON 해석에 실패했습니다: {error}") from error
                    prompt += f"\n[재시도] JSON 객체만 답해 주세요. 직전 형식 오류: {error}"
        raise LLMError("GPT의 파일 해석에 실패했습니다.")
