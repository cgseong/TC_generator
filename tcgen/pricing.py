"""고를 수 있는 모델과 API 환산 비용.

구독으로 실행하더라도 같은 호출을 API로 했을 때의 비용을 기준으로 비교한다.
Claude CLI는 호출마다 정가 기준 비용(``total_cost_usd``)을 직접 알려 주므로 표가
필요 없다. Codex CLI는 토큰만 알려 주므로 OpenAI 공개 단가(Standard, 2026-11
기준, 1M 토큰당 USD)로 계산한다. https://developers.openai.com/api/docs/pricing
"""
from __future__ import annotations

import json
from pathlib import Path

CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_MODELS = (
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-haiku-5-5", "Haiku 5.5"),
    ("claude-fable-5-1", "Fable 5.1"),
)

# (입력, 캐시 입력, 출력) — 입력 272K 이하 / 초과
LONG_CONTEXT_TOKENS = 272_000
GPT_PRICES: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "gpt-6-astra": ((10.00, 1.00, 50.00), (20.00, 2.00, 75.00)),
    "gpt-6.1-sol": ((2.00, 0.10, 10.00), (4.00, 0.20, 15.00)),
    "gpt-6.1-terra": ((1.00, 0.05, 5.00), (2.00, 0.10, 7.50)),
    "gpt-6-luna": ((0.10, 0.01, 0.50), (0.20, 0.02, 0.75)),
    "gpt-6-sol": ((2.00, 0.20, 10.00), (4.00, 0.40, 15.00)),
    "gpt-5.6-sol": ((4.00, 0.40, 20.00), (8.00, 0.80, 30.00)),
    "gpt-5.6-terra": ((2.00, 0.20, 12.00), (4.00, 0.40, 18.00)),
    "gpt-5.6-luna": ((0.20, 0.02, 1.20), (0.40, 0.04, 1.80)),
}
_GPT_FALLBACK_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_MODELS_CACHE = Path.home() / ".codex" / "models_cache.json"


def gpt_cost(model: str | None, input_tokens: int, cached: int, output: int) -> float | None:
    """Codex 호출 한 번의 API 환산 비용. 단가를 모르는 모델이면 None."""
    prices = GPT_PRICES.get(model or "")
    if prices is None:
        return None
    rate_in, rate_cached, rate_out = prices[input_tokens > LONG_CONTEXT_TOKENS]
    cached = min(cached, input_tokens)
    return ((input_tokens - cached) * rate_in + cached * rate_cached + output * rate_out) / 1_000_000


def _codex_models() -> list[dict]:
    """Codex가 받아 둔 모델 목록(화면에 노출되는 것)을 쓴다. 없으면 단가표를 쓴다."""
    try:
        cached = json.loads(CODEX_MODELS_CACHE.read_text(encoding="utf-8")).get("models", [])
        models = [{"id": item["slug"], "label": item.get("display_name") or item["slug"],
                   "efforts": [level["effort"] for level in item.get("supported_reasoning_levels") or []
                               if isinstance(level, dict) and level.get("effort")] or list(_GPT_FALLBACK_EFFORTS),
                   "default_effort": item.get("default_reasoning_level")}
                  for item in sorted(cached, key=lambda item: item.get("priority", 999))
                  if isinstance(item, dict) and item.get("slug") and item.get("visibility") == "list"]
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        models = []
    if not models:
        models = [{"id": slug, "label": slug, "efforts": list(_GPT_FALLBACK_EFFORTS), "default_effort": None}
                  for slug in GPT_PRICES]
    for model in models:
        model["priced"] = model["id"] in GPT_PRICES
    return models


def model_options() -> dict[str, list[dict]]:
    claude = [{"id": model_id, "label": label, "efforts": list(CLAUDE_EFFORTS),
               "default_effort": None, "priced": True} for model_id, label in CLAUDE_MODELS]
    return {"claude": claude, "codex": _codex_models()}


def allowed_efforts(provider: str, model: str | None) -> tuple[str, ...]:
    options = model_options().get(provider, [])
    chosen = next((item for item in options if item["id"] == model), None)
    if chosen is not None:
        return tuple(chosen["efforts"])
    return tuple(dict.fromkeys(level for item in options for level in item["efforts"]))
