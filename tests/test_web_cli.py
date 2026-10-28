import json
from types import SimpleNamespace

import pytest

from decision_browser.web import cli


@pytest.fixture
def runtime(monkeypatch):
    events = []
    class Browser:
        browser_version = "Chrome/test"
        def __init__(self, task, endpoint):
            assert endpoint == "ws://127.0.0.1:45678/devtools/browser/owned"
            events.append(("browser", task, endpoint))
            self.cdp_counts = {}
        def observe(self):
            events.append("observe")
            return {"raw": True}
        def screenshot(self):
            events.append("screenshot")
            return b"PNG"
        def close(self):
            events.append("browser.close")
    class Model:
        calls = [{"request": {"inputs": ["actual"]}, "response": {"model": "microsoft-decision-1"}}]
        text_calls = [{"text": "generated"}]
        total_tokens = 12
        def __init__(self, *args, **kwargs):
            events.append(("model", args))
        def authenticate(self):
            pass
        def close(self):
            events.append("model.close")
    class Agent:
        def __init__(self, task, browser, model, journal, task_id):
            events.append(("agent", journal, task_id))
        def run(self):
            events.append("run")
            return {"status": "SUCCEEDED", "elapsed_ms": 42, "decisions": [{"accepted": True}]}
        def close(self):
            events.append("agent.close")
    monkeypatch.setattr(cli, "Browser", Browser)
    monkeypatch.setattr(cli, "ModelClient", Model)
    monkeypatch.setattr(cli, "WebAgent", Agent)
    def validate(path):
        events.append(("manifest", path))
        return {"endpoint": "ws://127.0.0.1:45678/devtools/browser/owned"}
    monkeypatch.setattr(cli, "validate_manifest", validate)
    monkeypatch.setattr(cli, "get_task", lambda name, date: SimpleNamespace(name=name, date=date))
    def verify(task, page):
        assert page == {"raw": True}
        events.append("verify")
        return {"passed": True}
    monkeypatch.setattr(cli, "verify_task", verify)
    for name, value in (("AZURE_ENDPOINT", "endpoint"), ("AZURE_DEPLOYMENT_NAME", "decision"),
                        ("AZURE_TEXT_ENDPOINT", "text-endpoint"), ("AZURE_TEXT_DEPLOYMENT", "text")):
        monkeypatch.setenv(name, value)
    return events


def argv(tmp_path):
    return ["--task", "flights", "--manifest", "owned.json", "--journal", str(tmp_path / "journal.db"),
            "--task-id", "unique", "--output", str(tmp_path / "result.json")]


def test_cli_uses_explicit_manifest_provider_arguments_and_independent_verification(tmp_path, runtime, capsys):
    assert cli.main(argv(tmp_path)) == 0
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["elapsed_ms"] == 42
    assert result["independent_verification"] == {"passed": True}
    assert result["calls"][0]["request"]["inputs"] == ["actual"]
    assert result["model_text_calls"] == [{"text": "generated"}]
    assert (tmp_path / "result.png").read_bytes() == b"PNG"
    assert runtime[0] == ("manifest", "owned.json")
    assert ("model", ("endpoint", "decision", "text-endpoint", "text")) in runtime
    assert runtime.index("run") < runtime.index("observe") < runtime.index("verify") < runtime.index("screenshot")
    assert runtime[-3:] == ["agent.close", "model.close", "browser.close"]
    assert "actual" not in capsys.readouterr().out


def test_unverified_success_is_nonzero(tmp_path, runtime, monkeypatch):
    monkeypatch.setattr(cli, "verify_task", lambda task, page: {"passed": False})
    assert cli.main(argv(tmp_path)) == 1
    assert json.loads((tmp_path / "result.json").read_text())["independent_verification"] == {"passed": False}


def test_model_constructor_failure_is_sanitized_and_browser_closed(tmp_path, runtime, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ValueError("TOKEN_SUPER_SECRET")
    monkeypatch.setattr(cli, "ModelClient", fail)
    assert cli.main(argv(tmp_path)) == 1
    text = (tmp_path / "result.json").read_text()
    assert "TOKEN_SUPER_SECRET" not in text + capsys.readouterr().out
    assert json.loads(text)["exception_type"] == "ValueError"
    assert runtime[-1] == "browser.close"


def test_manifest_failure_never_connects(tmp_path, runtime, monkeypatch):
    def fail(path):
        raise ValueError("unowned")
    monkeypatch.setattr(cli, "validate_manifest", fail)
    assert cli.main(argv(tmp_path)) == 1
    assert runtime == []


def test_run_failure_closes_every_created_resource(tmp_path, runtime, monkeypatch):
    def fail(self):
        raise RuntimeError("failure")
    monkeypatch.setattr(cli.WebAgent, "run", fail)
    assert cli.main(argv(tmp_path)) == 1
    assert runtime[-3:] == ["agent.close", "model.close", "browser.close"]


def test_manifest_argument_is_required(tmp_path):
    with pytest.raises(SystemExit) as error:
        cli.main(["--task", "flights", "--journal", "j", "--task-id", "t", "--output", "o"])
    assert error.value.code == 2


def test_module_entrypoint_in_fresh_process_requires_owned_manifest(tmp_path):
    import subprocess
    import sys

    completed = subprocess.run([sys.executable, "-m", "decision_browser.web.cli", "--task", "flights",
                                "--journal", str(tmp_path / "journal.db"), "--task-id", "fresh",
                                "--output", str(tmp_path / "result.json")],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 2
    assert "--manifest" in completed.stderr
    assert not (tmp_path / "journal.db").exists()
    assert not (tmp_path / "result.json").exists()
