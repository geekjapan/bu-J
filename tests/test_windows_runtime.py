import argparse
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from decision_browser import _runtime, cli
from decision_browser.drivers import harness_mcp
from decision_browser.web import environment, provider


def test_windows_azure_cli_discovery_is_shared_and_keeps_fixed_arguments(monkeypatch):
    executable = r'C:\Program Files\Azure CLI\az.cmd'
    lookups, calls = [], []
    monkeypatch.setattr(_runtime, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(_runtime.shutil, 'which', lambda name: lookups.append(name) or executable)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, 'offline-token\n')

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(cli, 'AzureDecisionProvider', lambda endpoint, deployment, **auth: auth)
    args = argparse.Namespace(azure_endpoint='https://example.cognitiveservices.azure.com',
                              azure_deployment='decision', azure_auth='azure-cli')
    assert cli.azure_provider(args) == {'entra_token': 'offline-token'}
    assert provider._azure_token() == 'offline-token'
    assert lookups == ['az', 'az']
    for argv, kwargs in calls:
        assert argv == [executable, 'account', 'get-access-token', '--resource',
                        'https://cognitiveservices.azure.com/', '--query', 'accessToken', '--output', 'tsv']
        assert kwargs['encoding'] == 'utf-8' and kwargs['check'] is True
        assert not kwargs.get('shell', False)


@pytest.mark.parametrize('windows', [False, True])
def test_harness_environment_retains_only_required_windows_system_variable(monkeypatch, tmp_path, windows):
    monkeypatch.setattr(harness_mcp, 'os', SimpleNamespace(
        name='nt' if windows else 'posix', defpath=os.defpath,
        environ={'SystemRoot': r'C:\Windows', 'AZURE_ENTRA_TOKEN': 'secret', 'HOME': '/private',
                 'PATH': '/untrusted', 'COMSPEC': 'untrusted.exe'}))
    settings = harness_mcp.HarnessSettings('ws://127.0.0.1:9222', tmp_path / 'profile', tmp_path,
                                           'fixture', 'http://127.0.0.1:8766', 'tab')
    env = settings.environment()
    expected = {'PATH', 'BU_CDP_WS', 'BU_NAME', 'BH_RUNTIME_DIR', 'BH_UPDATE_CHECK', 'BH_OPEN_LIVE_URL'}
    assert set(env) == expected | ({'SystemRoot'} if windows else set())
    assert env['PATH'] == os.defpath
    if windows:
        assert env['SystemRoot'] == r'C:\Windows'


def test_docker_json_output_decodes_utf8_with_legacy_parent_encoding(monkeypatch):
    run = subprocess.run

    def docker_process(argv, **kwargs):
        assert argv == ['docker', 'inspect', 'fixture']
        return run([sys.executable, '-c', "import sys; sys.stdout.buffer.write('{\"name\":\"日本語\"}'.encode('utf-8'))"],
                   env={**os.environ, 'PYTHONIOENCODING': 'cp1252'}, **kwargs)

    monkeypatch.setattr(environment, 'subprocess', SimpleNamespace(run=docker_process))
    assert json.loads(environment.docker('inspect', 'fixture').stdout) == {'name': '日本語'}


def test_general_task_and_json_files_are_utf8_with_legacy_default(tmp_path):
    task = tmp_path / 'task.json'
    task.write_bytes(json.dumps({'url': 'https://example.com', 'goal': '日本語の内容を確認'}, ensure_ascii=False).encode('utf-8'))
    output = tmp_path / 'output'
    script = '''import json, sys
from pathlib import Path
from decision_browser.general import cli
from decision_browser.web.environment import save
original_open = Path.open
def legacy_open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
    if 'b' not in mode and encoding is None:
        encoding = 'cp1252'
    return original_open(self, mode, buffering, encoding, errors, newline)
Path.open = legacy_open
async def execute(args, task, output, journal):
    result = {'status': 'REPORTED', 'goal': task.goal}
    cli.save(output / 'result.json', result)
    save(output / 'docker.json', {'name': task.goal})
    return result
cli.execute = execute
raise SystemExit(cli.main(['--task', sys.argv[1], '--output', sys.argv[2], '--cdp-url', 'ws://localhost:9222/devtools/browser/unused']))
'''
    process = subprocess.run([sys.executable, '-c', script, str(task), str(output)],
                             capture_output=True, encoding='utf-8', timeout=15,
                             env={**os.environ, 'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'})
    assert process.returncode == 0, process.stderr + process.stdout
    assert json.loads((output / 'task.json').read_bytes())['goal'] == '日本語の内容を確認'
    assert json.loads((output / 'result.json').read_bytes())['goal'] == '日本語の内容を確認'
    assert json.loads((output / 'docker.json').read_bytes())['name'] == '日本語の内容を確認'


def test_synthetic_cli_emits_utf8_json_under_legacy_pipe_encoding(tmp_path):
    output = tmp_path / 'result.json'
    process = subprocess.run([sys.executable, '-c', 'from decision_browser.cli import main; raise SystemExit(main())',
                              '--query', '日本語', '--task-id', '日本語',
                              '--mode', 'observe-only', '--journal', str(tmp_path / 'journal.sqlite3'),
                              '--output', str(output)], capture_output=True, encoding='utf-8', timeout=15,
                             env={**os.environ, 'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'})
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == json.loads(output.read_bytes())
    assert '日本語' in process.stdout
