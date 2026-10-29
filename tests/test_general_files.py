import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from decision_browser.general.files import collect_downloads, stage_uploads


def test_staged_upload_keeps_filename_and_copies_the_opened_bytes(tmp_path, monkeypatch):
    source = tmp_path / '仕様書.pdf'
    source.write_bytes(b'approved file')
    calls = []
    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=b'')
    monkeypatch.setattr('decision_browser.general.files.subprocess.run', execute)
    paths, receipts = stage_uploads('owned-container', {'spec': str(source)})
    assert paths['spec'].startswith('/tmp/bu-j-uploads/')
    assert paths['spec'].endswith('/仕様書.pdf')
    assert calls[0][0][-1] == paths['spec']
    assert calls[0][1]['input'] == b'approved file'
    assert receipts['spec']['sha256'] == hashlib.sha256(b'approved file').hexdigest()


def test_upload_symlink_rejected_before_transfer(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.write_text('secret')
    link = tmp_path / 'link'
    link.symlink_to(source)
    calls = []
    monkeypatch.setattr('decision_browser.general.files.subprocess.run', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(OSError):
        stage_uploads('owned-container', {'bad': str(link)})
    assert calls == []


def test_unicode_download_is_preserved_under_safe_artifact_name(tmp_path, monkeypatch):
    data = b'quoted text'
    response = [{'name': '見積書.txt', 'data': base64.b64encode(data).decode()}]
    monkeypatch.setattr('decision_browser.general.files.subprocess.run',
                        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(response).encode()))
    records = collect_downloads('owned-container', tmp_path)
    assert records[0]['name'] == '見積書.txt'
    assert (tmp_path / records[0]['path']).read_bytes() == data
    assert records[0]['sha256'] == hashlib.sha256(data).hexdigest()


def test_binary_upload_does_not_translate_windows_control_bytes(tmp_path, monkeypatch):
    source = tmp_path / 'binary.dat'
    data = b'first\r\nsecond\x00\x1a\xff\n'
    source.write_bytes(data)
    sent = []
    monkeypatch.setattr('decision_browser.general.files.subprocess.run',
                        lambda *args, **kwargs: sent.append(kwargs['input']))
    _, receipts = stage_uploads('owned-container', {'binary': str(source)})
    assert sent == [data]
    assert receipts['binary']['bytes'] == len(data)


def test_download_names_are_portable_and_case_collision_free(tmp_path, monkeypatch):
    names = ['NUL.txt', 'COM1', 'report.', 'Report.csv', 'report.csv', 'CON', '見積書.pdf']
    response = [{'name': name, 'data': base64.b64encode(name.encode('utf-8')).decode()} for name in names]
    monkeypatch.setattr('decision_browser.general.files.subprocess.run',
                        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(response).encode()))
    records = collect_downloads('owned-container', tmp_path)
    assert len({r['path'].casefold() for r in records}) == len(names)
    for record, name in zip(records, names):
        assert record['name'] == name
        assert (tmp_path / record['path']).read_bytes() == name.encode('utf-8')
        assert not record['path'].endswith('.')
        assert record['path'].isascii()


def test_symlink_introduced_at_open_is_rejected_even_if_original_target(tmp_path, monkeypatch):
    from decision_browser.general import files
    source = tmp_path / 'source.txt'
    original = tmp_path / 'original.txt'
    source.write_bytes(b'authorized bytes')
    original.hardlink_to(source)
    open_upload = files._open_upload
    def swap(filename):
        source.unlink()
        source.symlink_to(original)
        return open_upload(filename)
    calls = []
    monkeypatch.setattr(files, '_open_upload', swap)
    monkeypatch.setattr(files.subprocess, 'run', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(OSError):
        stage_uploads('owned-container', {'file': str(source)})
    assert not calls


def test_download_collection_never_overwrites_an_existing_artifact(tmp_path, monkeypatch):
    response = [{'name': 'file.txt', 'data': base64.b64encode(b'content').decode()}]
    monkeypatch.setattr('decision_browser.general.files.subprocess.run',
                        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(response).encode()))
    records = collect_downloads('owned-container', tmp_path)
    artifact = tmp_path / records[0]['path']
    artifact.write_bytes(b'keep this evidence')
    with pytest.raises(FileExistsError):
        collect_downloads('owned-container', tmp_path)
    assert artifact.read_bytes() == b'keep this evidence'
