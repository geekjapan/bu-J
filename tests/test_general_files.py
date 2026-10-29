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
