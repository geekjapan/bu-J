from types import SimpleNamespace

import pytest

from verification import windows_browser as harness


def test_requires_windows_python(monkeypatch):
    monkeypatch.setattr(harness.platform, "system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="native Windows Python"):
        harness.chrome_executable()


def test_program_files_chrome_precedes_path(monkeypatch, tmp_path):
    executable = tmp_path / "Google/Chrome/Application/chrome.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(harness.platform, "system", lambda: "Windows")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setattr(harness.shutil, "which", lambda name: pytest.fail("PATH is fallback only"))
    assert harness.chrome_executable() == str(executable)


def test_path_fallback_and_missing_chrome(monkeypatch):
    monkeypatch.setattr(harness.platform, "system", lambda: "Windows")
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.setattr(harness.shutil, "which", lambda name: "C:/Chrome/chrome.exe")
    assert harness.chrome_executable() == "C:/Chrome/chrome.exe"
    monkeypatch.setattr(harness.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Google Chrome is required"):
        harness.chrome_executable()


def test_launch_owns_profile_and_loopback_debug_port(tmp_path):
    args = harness.chrome_arguments("chrome.exe", tmp_path)
    assert args[0] == "chrome.exe"
    assert f"--user-data-dir={tmp_path}" in args
    assert "--headless=new" in args
    assert "--remote-debugging-port=0" in args
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--proxy-server=http://127.0.0.1:8888" in args
    assert "--proxy-bypass-list=<-loopback>" in args
    assert not any("no-sandbox" in argument or "disable-setuid-sandbox" in argument for argument in args)


def test_endpoint_comes_from_owned_profile(monkeypatch, tmp_path):
    endpoint = "ws://127.0.0.1:45678/devtools/browser/owned"
    (tmp_path / "DevToolsActivePort").write_text("45678\n/devtools/browser/owned\n")
    def version(url):
        assert url == "http://127.0.0.1:45678/json/version"
        return {"webSocketDebuggerUrl": endpoint, "Browser": "Chrome/test"}
    monkeypatch.setattr(harness, "read_json", version)
    assert harness.wait_endpoint(tmp_path) == (endpoint, version("http://127.0.0.1:45678/json/version"))


def test_endpoint_mismatch_never_attaches(monkeypatch, tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("45678\n/devtools/browser/owned\n")
    monkeypatch.setattr(harness, "read_json", lambda url: {"webSocketDebuggerUrl": "ws://remote:1/other"})
    times = iter([0, 0, 2])
    monkeypatch.setattr(harness.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(harness.time, "sleep", lambda delay: None)
    with pytest.raises(TimeoutError):
        harness.wait_endpoint(tmp_path, timeout=1)


def test_cleanup_finds_profile_root_after_gui_stub_exits(monkeypatch, tmp_path):
    import psutil
    child = SimpleNamespace(pid=12)
    root = SimpleNamespace(pid=11, children=lambda recursive: [child], is_running=lambda: True)
    calls = []
    matches = iter([[root], []])
    monkeypatch.setattr(harness, "profile_processes", lambda profile: next(matches))
    monkeypatch.setattr(harness.subprocess, "run", lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: (processes, []))
    process = SimpleNamespace(pid=10, poll=lambda: 0)
    profile = tmp_path / "own-profile"
    profile.mkdir()
    result = harness.cleanup_chrome(process, profile)
    assert calls == [["taskkill", "/PID", "11", "/T", "/F"]]
    assert result == {"processes_absent": True, "profile_absent": True, "tracked_pids": [11, 12]}
    assert not profile.exists()


def test_cleanup_retains_profile_if_process_survives(monkeypatch, tmp_path):
    import psutil
    root = SimpleNamespace(pid=11, children=lambda recursive: [], is_running=lambda: True)
    monkeypatch.setattr(harness, "profile_processes", lambda profile: [root])
    monkeypatch.setattr(harness.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: ([], processes))
    with pytest.raises(RuntimeError, match="profile retained"):
        harness.cleanup_chrome(SimpleNamespace(poll=lambda: 0), tmp_path)
    assert tmp_path.exists()


def test_proxy_rejects_external_https():
    errors = []
    harness.FixtureProxy.do_CONNECT(SimpleNamespace(send_error=lambda *args: errors.append(args)))
    assert errors[0][0] == 403


def test_fixture_port_failure_retains_failure_proof_and_removes_only_profile(monkeypatch, tmp_path):
    profile = tmp_path / "owned-profile"
    profile.mkdir()
    untouched = tmp_path / "unrelated"
    untouched.mkdir()
    output = tmp_path / "evidence"
    monkeypatch.setattr(harness, "chrome_executable", lambda: "chrome.exe")
    monkeypatch.setattr(harness.platform, "platform", lambda: "Windows-test")
    monkeypatch.setattr(harness.tempfile, "mkdtemp", lambda **kwargs: str(profile))
    def unavailable(*args, **kwargs):
        raise OSError("Fixture port already occupied")
    monkeypatch.setattr(harness, "make_server", unavailable)
    monkeypatch.setattr(harness.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not launch"))
    with pytest.raises(OSError, match="occupied"):
        harness.main(["--output", str(output)])
    proof = harness.json.loads((output / "proof.json").read_text())
    assert proof["passed"] is False
    assert "occupied" in proof["error"]
    assert proof["cleanup"]["profile_absent"] is True
    assert untouched.is_dir()
    assert not profile.exists()
