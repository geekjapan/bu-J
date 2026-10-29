"""Offline checks for optional fixture transport; never contact Docker."""
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from verification.generic_browser import container_files


def test_upload_transport_uses_locally_staged_binary_bytes(tmp_path, monkeypatch):
    source = tmp_path / '仕様書.pdf'
    data = b'approved\r\n\x00\x1a\xff'
    source.write_bytes(data)
    calls = []
    monkeypatch.setattr(container_files.subprocess, 'run', lambda argv, **kwargs: calls.append((argv, kwargs)))
    output = tmp_path / 'evidence'
    paths, receipts = container_files.stage_uploads('owned-container', output, {'spec': str(source)})
    assert Path(paths['spec']).name == source.name
    assert calls[0][0][:4] == ['docker', 'exec', '-i', 'owned-container']
    assert calls[0][1]['input'] == data
    staged = list((output / 'uploads').glob('*/*'))
    assert len(staged) == 1 and staged[0].read_bytes() == data
    assert receipts['spec']['sha256'] == hashlib.sha256(data).hexdigest()


def test_download_transport_fetches_only_completed_scoped_names(tmp_path, monkeypatch):
    data = b'downloaded\r\n\x00\x1a\xff'
    metadata = [{'suggested_filename': '見積書.txt', 'state': 'completed', 'guid': 'scoped'},
                {'name': 'pending.txt', 'state': 'inProgress'}]
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps([
            {'name': 'scoped', 'data': base64.b64encode(data).decode()},
        ]).encode())

    monkeypatch.setattr(container_files.subprocess, 'run', execute)
    records = container_files.collect_downloads('owned-container', metadata, tmp_path / 'out')
    assert json.loads(calls[0][-1]) == ['scoped']
    assert records[0]['copied'] is True and records[1]['copied'] is False
    assert (tmp_path / 'out' / records[0]['path']).read_bytes() == data
    assert records[0]['sha256'] == hashlib.sha256(data).hexdigest()


def test_container_download_reader_does_not_scan_or_follow_links(tmp_path, monkeypatch):
    downloads = tmp_path / 'container-downloads'
    downloads.mkdir()
    (downloads / 'approved.txt').write_bytes(b'approved')
    (downloads / 'unrelated.txt').write_bytes(b'private')
    (downloads / 'link.txt').symlink_to(downloads / 'unrelated.txt')
    execute = subprocess.run

    def local_container_script(argv, **kwargs):
        assert argv[:5] == ['docker', 'exec', 'owned-container', 'python', '-c']
        return execute([sys.executable, '-c', argv[5], str(downloads), argv[7]], **kwargs)

    monkeypatch.setattr(container_files.subprocess, 'run', local_container_script)
    records = container_files.collect_downloads('owned-container', [
        {'name': 'approved.txt', 'guid': 'approved.txt', 'state': 'completed'},
    ], tmp_path / 'out')
    assert len(records) == 1 and records[0]['name'] == 'approved.txt'
    assert len(list((tmp_path / 'out' / 'downloads').iterdir())) == 1
    with pytest.raises(subprocess.CalledProcessError):
        container_files.collect_downloads('owned-container', [
            {'name': 'link.txt', 'guid': 'link.txt', 'state': 'completed'},
        ], tmp_path / 'link-out')


def test_allow_and_name_transport_without_guid_never_falls_back_to_suggestion(tmp_path, monkeypatch):
    calls = []
    def execute(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=b'[]')
    monkeypatch.setattr(container_files.subprocess, 'run', execute)
    records = container_files.collect_downloads('owned-container', [
        {'suggested_filename': 'unrelated.txt', 'state': 'completed'},
    ], tmp_path / 'out')
    assert json.loads(calls[0][-1]) == []
    assert records[0]['copied'] is False
    assert not (tmp_path / 'out').exists()
