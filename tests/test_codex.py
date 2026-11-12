"""GPT 구독 경로: 도구 차단·인증·JSONL·그림/PDF·제공자 설정."""
import io
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from PIL import Image

from fixtures import make_problem
from test_initial_catalog import collection
from test_server import client
from tcgen import codex, server, workspace as workspace_module
from tcgen.codex import CodexCLI, _extract_codex_text, codex_auth_status
from tcgen.llm import ClaudeCLI, LLMError, make_cli
from tcgen.workspace import create_workspace


def completed(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def events(text):
    return '\n'.join(json.dumps(event) for event in [
        {"type": "thread.started", "thread_id": "irrelevant"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "not the answer"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"output_tokens": 10}},
    ])


def auth(argv, prompt, timeout):
    return completed(stderr="Logged in using ChatGPT")


@pytest.fixture(autouse=True)
def installed_cli(monkeypatch):
    monkeypatch.setattr(codex, "resolve_codex_executable", lambda executable="codex": "/test/bin/codex")


def test_factory_selects_requested_provider():
    assert isinstance(make_cli("claude"), ClaudeCLI)
    assert type(make_cli("codex")) is CodexCLI
    assert make_cli("codex", model="custom").model == "custom"
    with pytest.raises(ValueError):
        make_cli("unknown")


def test_exec_is_ephemeral_read_only_in_empty_directory_with_tools_disabled():
    calls = []
    def runner(argv, prompt, timeout):
        calls.append(argv)
        folder = Path(argv[argv.index("--cd") + 1])
        assert folder.is_dir() and list(folder.iterdir()) == []
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in argv and "--ephemeral" in argv
        assert 'web_search="disabled"' in argv and 'approval_policy="never"' in argv
        disabled = [argv[i+1] for i, value in enumerate(argv[:-1]) if value == "--disable"]
        assert {"shell_tool", "code_mode_host", "hooks", "plugins", "apps", "multi_agent"} <= set(disabled)
        assert prompt == "return code" and prompt not in argv
        return completed(events('```python\nprint(1)\n```'))
    assert CodexCLI(runner=runner, auth_runner=auth).ask_code("return code") == "print(1)"
    assert not Path(calls[0][calls[0].index("--cd")+1]).exists()


def test_last_agent_message_is_used():
    stream = events("first") + '\n' + json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final"}})
    assert _extract_codex_text(completed(stream)) == "final"


@pytest.mark.parametrize("event", [
    {"type": "turn.failed", "error": {"message": "usage limit reached"}},
    {"type": "error", "message": "usage limit reached"},
])
def test_errors_are_not_mistaken_for_answers(event):
    with pytest.raises(LLMError, match="usage limit reached"):
        _extract_codex_text(completed(json.dumps(event)))


def test_api_authentication_is_rejected_without_running_generation():
    def forbidden(*args):
        pytest.fail("API credentials must not run generation")
    cli = CodexCLI(runner=forbidden, auth_runner=lambda *args: completed(stderr="Logged in using an API key"))
    with pytest.raises(LLMError, match="Sign in with ChatGPT"):
        cli.ask_code("x")


def test_logged_out_authentication_gives_login_command():
    status = codex_auth_status(runner=lambda *args: completed(code=1, stderr="Not logged in"))
    assert not status["ready"] and "codex login" in status["message"]


def test_runner_does_not_inherit_api_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("CODEX_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")
    captured = {}
    def run(*args, **kwargs):
        captured.update(kwargs)
        return completed()
    monkeypatch.setattr(subprocess, "run", run)
    codex._run_codex(["codex"], "x", 10)
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"} & captured["env"].keys()


def test_error_logs_identify_provider_and_keep_actual_error(tmp_path):
    def runner(*args):
        return completed(json.dumps({"type": "turn.failed", "error": {"message": "subscription unavailable"}}), code=1)
    with pytest.raises(LLMError):
        CodexCLI(runner=runner, auth_runner=auth, log_dir=tmp_path).ask("x", tag="failure")
    log = json.loads(next(tmp_path.glob('*-failure.json')).read_text())
    assert log["provider"] == "codex" and "subscription unavailable" in log["error"]


def test_image_attachments_survive_json_format_retry(tmp_path):
    source = tmp_path / "figure.avif"
    Image.new("RGB", (4, 4), "red").save(source, format="AVIF")
    seen = []
    def runner(argv, *args):
        paths = [Path(arg.split("=", 1)[1]) for arg in argv if arg.startswith("--image=")]
        assert len(paths) == 1 and paths[0].read_bytes().startswith(b'\x89PNG')
        seen.append(paths[0])
        return completed(events("not JSON" if len(seen) == 1 else '{"ok":true}'))
    result = CodexCLI(runner=runner, auth_runner=auth).ask_json_about_files("read image", [source])
    assert result == {"ok": True} and seen[0] == seen[1]
    assert not seen[0].exists()


def test_pdf_is_rendered_to_ordered_page_images(tmp_path):
    source = tmp_path / "problem.pdf"
    subprocess.run([sys.executable, "-I", "-c",
        "import pymupdf,sys; d=pymupdf.open(); "
        "d.new_page().insert_text((20,30),'Page one'); "
        "d.new_page().insert_text((20,30),'Page two'); d.save(sys.argv[1])", str(source)], check=True)
    def runner(argv, *args):
        paths = [Path(arg.split("=", 1)[1]) for arg in argv if arg.startswith("--image=")]
        assert len(paths) == 2 and all(path.is_file() for path in paths)
        assert paths[0].read_bytes() != paths[1].read_bytes()
        return completed(events('{"pages":2}'))
    assert CodexCLI(runner=runner, auth_runner=auth).ask_json_about_files("read PDF", [source]) == {"pages": 2}


def test_provider_and_separate_model_preferences_persist(client):
    assert client.post('/api/settings', json={"provider": "claude", "model": "sonnet"}).status_code == 200
    assert client.post('/api/settings', json={"provider": "codex", "model": "gpt-custom"}).status_code == 200
    restored = client.post('/api/settings', json={"provider": "claude"}).json()
    assert restored["model"] == "sonnet"
    assert client.post('/api/settings', json={"provider": "codex"}).json()["model"] == "gpt-custom"
    assert server._load_settings()["provider"] == "codex"
    assert server._load_settings()["models"] == {"claude": "sonnet", "codex": "gpt-custom"}
    assert client.post('/api/settings', json={"provider": "unsupported"}).status_code == 422


def test_invalid_settings_do_not_partly_change_provider(client):
    assert client.post('/api/settings', json={"provider":"claude", "model":"sonnet"}).status_code == 200
    assert client.post('/api/settings', json={"provider":"codex", "drive_folder":"invalid"}).status_code == 400
    assert server._llm_settings() == {"provider":"claude", "model":"sonnet", "effort": None}


def test_environment_reports_selected_cli_and_login_status(client, monkeypatch):
    client.post('/api/settings', json={"provider":"codex"})
    monkeypatch.setattr(server, "codex_auth_status", lambda: {"ready": True, "method":"chatgpt", "message":"signed in"})
    status = client.get('/api/llm-status').json()
    assert status["provider"] == "codex" and status["method"] == "chatgpt"
    html = client.get('/').text
    assert '<option value="codex" selected>' in html


def test_batch_keeps_provider_selected_at_start(client, collection, monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    seen = []
    def worker(number, cancel, progress, skip, *, llm_settings):
        seen.append(dict(llm_settings))
        if len(seen) == 1:
            started.set()
            assert release.wait(3)
        if len(seen) == 2:
            finished.set()
        return {"status":"exported"}
    monkeypatch.setattr(server, "_execute_batch_item", worker)
    client.post('/api/settings', json={"provider":"codex", "model":"gpt-custom"})
    assert client.post('/api/batches', json={"problem_ids":[1000,1001]}).status_code == 200
    assert started.wait(3)
    client.post('/api/settings', json={"provider":"claude", "model":"sonnet"})
    release.set()
    assert finished.wait(3)
    assert seen == [{"provider":"codex", "model":"gpt-custom", "effort": None}] * 2
    assert server._batch_manager().snapshot()["llm"]["provider"] == "codex"
