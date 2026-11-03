"""Anthropic Python SDK 직접 호출 클라이언트.

Claude CLI subprocess 대신 SDK를 직접 사용해 프로세스 시작 오버헤드 없이 빠르게 동작한다.
ANTHROPIC_API_KEY 환경 변수 또는 `ant auth login`으로 인증한 프로필이 필요하다.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Sequence

from tcgen.config import LLM_MAX_RETRIES, LLM_TIMEOUT_S
from tcgen.llm import LLMError, LLMResponse, extract_code_block, extract_json, redact

logger = logging.getLogger(__name__)

_LOG_SEQUENCE = itertools.count()
_LOG_STDOUT_LIMIT = 20_000

_IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class AnthropicSDKClient:
    """Anthropic Python SDK 직접 호출 클라이언트."""

    provider: ClassVar[str] = "anthropic"

    model: str = "claude-opus-5-5"
    timeout_s: float = LLM_TIMEOUT_S
    log_dir: Path | None = None
    max_retries: int = LLM_MAX_RETRIES

    def _make_client(self):
        try:
            import anthropic
        except ImportError as error:
            raise LLMError(
                "anthropic 패키지가 없습니다. `pip install anthropic`을 실행하세요."
            ) from error
        return anthropic.Anthropic(timeout=self.timeout_s)

    def _ask(
        self,
        prompt: str,
        *,
        tag: str,
        extra_content: list[dict] | None = None,
    ) -> LLMResponse:
        import anthropic

        if extra_content:
            content: Any = extra_content + [{"type": "text", "text": prompt}]
        else:
            content = prompt

        client = self._make_client()
        started = time.perf_counter()
        try:
            with client.messages.stream(
                model=self.model,
                max_tokens=64000,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                response = stream.get_final_message()
        except anthropic.APITimeoutError as error:
            raise LLMError(
                f"Anthropic API 응답이 {self.timeout_s:.0f}초를 넘겼습니다."
            ) from error
        except anthropic.AuthenticationError as error:
            raise LLMError(
                "Anthropic API 인증 실패. ANTHROPIC_API_KEY를 확인하거나 "
                "`ant auth login`으로 로그인하세요."
            ) from error
        except anthropic.APIStatusError as error:
            raise LLMError(
                f"Anthropic API 오류 (HTTP {error.status_code}): {error.message}"
            ) from error
        except anthropic.APIConnectionError as error:
            raise LLMError(f"Anthropic API 연결 실패: {error}") from error

        duration_ms = int((time.perf_counter() - started) * 1000)
        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text.strip():
            raise LLMError("응답에서 텍스트를 찾지 못했습니다.")

        self._write_log(tag, prompt, text, duration_ms, response)
        return LLMResponse(text=text, duration_ms=duration_ms, tag=tag)

    def _ask_with_retry(
        self,
        prompt: str,
        *,
        tag: str,
        extract,
        extra_content: list[dict] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            response = self._ask(
                prompt, tag=f"{tag}-{attempt + 1}", extra_content=extra_content
            )
            try:
                return extract(response.text)
            except LLMError as error:
                last_error = error
                logger.warning("응답 해석 실패(%s, 시도 %d): %s", tag, attempt + 1, error)
                prompt = f"{prompt}\n\n[재시도] 직전 응답을 해석할 수 없었습니다: {error}"
        raise LLMError(
            f"{self.max_retries + 1}회 시도했지만 해석에 실패했습니다: {last_error}"
        )

    def ask_code(self, prompt: str, *, tag: str = "code") -> str:
        return self._ask_with_retry(prompt, tag=tag, extract=extract_code_block)

    def ask_json(
        self, prompt: str, *, tag: str = "json", readable_dirs: Sequence[Path] = ()
    ) -> dict[str, Any]:
        return self._ask_with_retry(prompt, tag=tag, extract=extract_json)

    def ask_json_about_files(
        self, prompt: str, paths: Sequence[Path], *, tag: str = "file"
    ) -> dict[str, Any]:
        """파일을 base64로 인코딩해 content block으로 첨부하고 JSON을 받는다."""
        extra_content: list[dict] = []
        for path in paths:
            resolved = Path(path).resolve()
            if not resolved.is_file():
                raise LLMError(f"파일을 찾을 수 없습니다: {resolved}")
            data = base64.standard_b64encode(resolved.read_bytes()).decode("utf-8")
            suffix = resolved.suffix.lower()
            if suffix == ".pdf":
                extra_content.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": data,
                    },
                })
            elif suffix in _IMAGE_MEDIA_TYPES:
                extra_content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _IMAGE_MEDIA_TYPES[suffix],
                        "data": data,
                    },
                })
            else:
                raise LLMError(f"지원하지 않는 파일 형식입니다: {resolved.name}")
        return self._ask_with_retry(
            prompt, tag=tag, extract=extract_json, extra_content=extra_content
        )

    def _write_log(
        self,
        tag: str,
        prompt: str,
        text: str,
        duration_ms: int,
        response: Any = None,
    ) -> None:
        if self.log_dir is None:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = self.log_dir / f"{stamp}-{next(_LOG_SEQUENCE):04d}-{tag}.json"
            payload: dict[str, Any] = {
                "provider": self.provider,
                "model": self.model,
                "tag": tag,
                "duration_ms": duration_ms,
                "prompt": redact(prompt),
                "response": redact(text)[:_LOG_STDOUT_LIMIT],
            }
            if response is not None:
                usage = getattr(response, "usage", None)
                if usage:
                    payload["usage"] = {
                        "input_tokens": getattr(usage, "input_tokens", None),
                        "output_tokens": getattr(usage, "output_tokens", None),
                    }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as error:
            logger.warning("LLM 호출 로그를 남기지 못했습니다: %s", error)
