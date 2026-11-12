"""문제별 토큰 사용량 집계와 effort 설정."""
import json
import subprocess

import pytest

from test_server import client
from tcgen import codex, llm, pricing, server
from tcgen.codex import CodexCLI
from tcgen.llm import ClaudeCLI


def completed(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


def claude_reply(text="```python\nprint(1)\n```", **extra):
    return json.dumps({"result": text, "is_error": False, "total_cost_usd": 0.0125, **extra,
                       "usage": {"input_tokens": 3, "cache_creation_input_tokens": 100,
                                 "cache_read_input_tokens": 50, "output_tokens": 20},
                       "modelUsage": {"claude-haiku-5-5": {}, "claude-sonnet-5-5": {}}})


@pytest.fixture(autouse=True)
def installed_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(pricing, "CODEX_MODELS_CACHE", tmp_path / "missing.json")
    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/test/bin/{name}")
    monkeypatch.setattr(codex, "resolve_codex_executable", lambda executable="codex": "/test/bin/codex")


def test_claude_usage_accumulates_across_calls_including_failures():
    replies = iter([completed(claude_reply()), completed(claude_reply(is_error=True), code=1)])
    cli = ClaudeCLI(runner=lambda argv, prompt, timeout: next(replies), max_retries=0)
    cli.ask_code("first")
    with pytest.raises(llm.LLMError):
        cli.ask_code("second")
    assert cli.usage.snapshot() == {"input": 306, "cached": 100, "output": 40, "calls": 2, "total": 346,
                                    "cost_usd": 0.025, "unpriced_calls": 0,
                                    "models": ["claude-haiku-5-5", "claude-sonnet-5-5"]}


def test_codex_usage_is_priced_from_public_rates():
    stdout = "\n".join(json.dumps(event) for event in [
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"a": 1}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 30}},
    ])
    def make(model):
        return CodexCLI(model=model, runner=lambda argv, prompt, timeout: completed(stdout),
                        auth_runner=lambda argv, prompt, timeout: completed("Logged in using ChatGPT"))
    cli = make("gpt-6.1-sol")
    assert cli.ask_json("q") == {"a": 1}
    # 200 x $2 + 800 x $0.10 + 30 x $10 (1M 토큰당)
    assert cli.usage.snapshot() == {"input": 1000, "cached": 800, "output": 30, "calls": 1, "total": 1030,
                                    "cost_usd": 0.00078, "unpriced_calls": 0, "models": ["gpt-6.1-sol"]}
    default = make(None)
    default.ask_json("q")
    assert default.usage.snapshot()["unpriced_calls"] == 1


def test_long_context_rate_applies_above_272k_input():
    assert pricing.gpt_cost("gpt-6-astra", 300_000, 0, 0) == pytest.approx(6.0)
    assert pricing.gpt_cost("gpt-6-astra", 272_000, 0, 0) == pytest.approx(2.72)
    assert pricing.gpt_cost("unknown-model", 1, 0, 1) is None


def test_effort_is_passed_to_each_cli():
    assert ClaudeCLI(effort="high")._build_argv()[-2:] == ("--effort", "high")
    argv = CodexCLI(effort="low")._codex_argv(llm.Path("/tmp"))
    assert 'model_reasoning_effort="low"' in argv
    assert "--effort" not in ClaudeCLI()._build_argv()


def test_model_and_effort_are_saved_per_provider_and_validated(client):
    saved = client.post("/api/settings", json={"provider": "claude", "model": "claude-sonnet-5-5", "effort": "max"}).json()
    assert (saved["model"], saved["effort"]) == ("claude-sonnet-5-5", "max")
    assert client.post("/api/settings", json={"provider": "codex", "effort": "ultra"}).status_code == 422
    saved = client.post("/api/settings", json={"provider": "codex", "model": "gpt-6.1-sol", "effort": "xhigh"}).json()
    assert (saved["model"], saved["effort"]) == ("gpt-6.1-sol", "xhigh")
    restored = client.post("/api/settings", json={"provider": "claude"}).json()
    assert (restored["model"], restored["effort"]) == ("claude-sonnet-5-5", "max")
    assert server._llm_settings() == {"provider": "claude", "model": "claude-sonnet-5-5", "effort": "max"}
    options = client.get("/api/env").json()["model_options"]
    assert len(options["claude"]) == 4 and all(item["priced"] for item in options["codex"])
