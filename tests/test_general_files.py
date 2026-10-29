import hashlib
from pathlib import Path

import pytest

from decision_browser.general import files
from decision_browser.general.files import collect_downloads, stage_uploads


def completed(name, directory=None, *, saved_name=None):
    metadata = {'suggested_filename': name, 'state': 'completed', 'guid': 'scoped-guid'}
    if directory is not None:
        metadata['filePath'] = str(directory / (saved_name or name))
    return metadata


def test_staged_upload_keeps_filename_and_opened_binary_bytes(tmp_path):
    source = tmp_path / '仕様書.pdf'
    data = b'first\r\nsecond\x00\x1a\xff\n'
    source.write_bytes(data)
    paths, receipts = stage_uploads(tmp_path / 'evidence', {'spec': str(source)})
    destination = Path(paths['spec'])
    assert destination.is_absolute()
    assert destination.name == source.name
    assert destination.parent.parent == tmp_path / 'evidence' / 'uploads'
    assert destination.read_bytes() == data
    assert receipts['spec'] == {'name': source.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def test_upload_symlink_rejected(tmp_path):
    source = tmp_path / 'source'
    source.write_text('secret')
    link = tmp_path / 'link'
    link.symlink_to(source)
    with pytest.raises(OSError):
        stage_uploads(tmp_path / 'evidence', {'bad': str(link)})
    assert not (tmp_path / 'evidence').exists()


def test_symlink_introduced_at_open_is_rejected_even_if_original_target(tmp_path, monkeypatch):
    source = tmp_path / 'source.txt'
    original = tmp_path / 'original.txt'
    source.write_bytes(b'authorized bytes')
    original.hardlink_to(source)
    open_upload = files._open_upload

    def swap(filename):
        source.unlink()
        source.symlink_to(original)
        return open_upload(filename)

    monkeypatch.setattr(files, '_open_upload', swap)
    with pytest.raises(OSError):
        stage_uploads(tmp_path / 'evidence', {'file': str(source)})
    assert not (tmp_path / 'evidence').exists()


def test_download_uses_scoped_metadata_preserves_unicode_and_binary(tmp_path):
    directory = tmp_path / 'normal-downloads'
    directory.mkdir()
    name = '見積書.txt'
    data = b'quoted\r\ntext\x00\x1a\xff'
    (directory / name).write_bytes(data)
    (directory / 'unrelated-private.txt').write_text('do not copy')
    output = tmp_path / 'evidence'
    records = collect_downloads(directory, [completed(name, directory)], output)
    assert records[0]['name'] == name
    assert records[0]['copied'] is True
    assert records[0]['guid'] == 'scoped-guid'
    assert records[0]['path'].isascii()
    assert (output / records[0]['path']).read_bytes() == data
    assert records[0]['sha256'] == hashlib.sha256(data).hexdigest()
    assert len(list((output / 'downloads').iterdir())) == 1


def test_download_names_are_portable_and_case_collision_free(tmp_path):
    directory = tmp_path / 'normal-downloads'
    directory.mkdir()
    names = ['NUL.txt', 'COM1', 'report.', 'CON', '見積書.pdf']
    # Reserved Windows names cannot be created there; ordinary names exercise the same artifact mapping.
    import os
    if os.name == 'nt':
        names = ['Report.csv', 'report-2.csv', '見積書.pdf']
    for name in names:
        (directory / name).write_bytes(name.encode('utf-8'))
    records = collect_downloads(directory, [completed(name, directory) for name in names], tmp_path / 'out')
    assert len({r['path'].casefold() for r in records}) == len(names)
    for record, name in zip(records, names):
        assert record['name'] == name
        assert not record['path'].endswith('.')
        assert record['path'].isascii()
        assert (tmp_path / 'out' / record['path']).read_bytes() == name.encode('utf-8')


@pytest.mark.parametrize('metadata', [{}, {'name': 'pending', 'state': 'inProgress'}, completed('pending.crdownload')])
def test_missing_or_pending_metadata_never_claims_copy(tmp_path, metadata):
    records = collect_downloads(tmp_path, [metadata], tmp_path / 'out')
    assert records[0]['copied'] is False
    assert 'path' not in records[0]
    assert not (tmp_path / 'out').exists()


def test_no_directory_returns_metadata_without_reading_files(tmp_path):
    metadata = completed('observed.txt')
    records = collect_downloads(None, [metadata], tmp_path / 'out')
    assert records[0]['suggested_filename'] == 'observed.txt'
    assert records[0]['copied'] is False
    assert not (tmp_path / 'out').exists()
    assert collect_downloads(tmp_path, [], tmp_path / 'out') == []


def test_missing_completed_file_is_metadata_only(tmp_path):
    records = collect_downloads(tmp_path, [completed('absent.txt', tmp_path)], tmp_path / 'out')
    assert records[0]['copied'] is False
    assert 'not available' in records[0]['copy_reason']


@pytest.mark.parametrize('path', ['../secret', '/outside/secret', 'nested/file', 'bad\x00file'])
def test_download_rejects_invalid_saved_path(tmp_path, path):
    metadata = completed('safe-suggestion.txt')
    metadata['filePath'] = path
    with pytest.raises(ValueError):
        collect_downloads(tmp_path, [metadata], tmp_path / 'out')


def test_download_symlink_rejected(tmp_path):
    (tmp_path / 'secret').write_text('private')
    (tmp_path / 'link').symlink_to(tmp_path / 'secret')
    with pytest.raises(OSError):
        collect_downloads(tmp_path, [completed('link', tmp_path)], tmp_path / 'out')


def test_download_collection_never_overwrites_existing_artifact(tmp_path):
    (tmp_path / 'file.txt').write_bytes(b'content')
    output = tmp_path / 'out'
    records = collect_downloads(tmp_path, [completed('file.txt', tmp_path)], output)
    artifact = output / records[0]['path']
    artifact.write_bytes(b'keep this evidence')
    with pytest.raises(FileExistsError):
        collect_downloads(tmp_path, [completed('file.txt', tmp_path)], output)
    assert artifact.read_bytes() == b'keep this evidence'


def test_file_size_and_total_budgets(tmp_path, monkeypatch):
    (tmp_path / 'file').write_bytes(b'abcd')
    monkeypatch.setattr(files, 'MAX_FILE', 3)
    with pytest.raises(ValueError, match='regular file'):
        stage_uploads(tmp_path / 'out', {'file': str(tmp_path / 'file')})
    with pytest.raises(ValueError, match='regular file'):
        collect_downloads(tmp_path, [completed('file', tmp_path)], tmp_path / 'out')
    monkeypatch.setattr(files, 'MAX_FILE', 10)
    monkeypatch.setattr(files, 'MAX_TOTAL', 3)
    with pytest.raises(ValueError, match='budget'):
        stage_uploads(tmp_path / 'out', {'file': str(tmp_path / 'file')})
    with pytest.raises(ValueError, match='budget'):
        collect_downloads(tmp_path, [completed('file', tmp_path)], tmp_path / 'out')


def test_collision_without_exact_saved_path_never_copies_existing_file(tmp_path):
    (tmp_path / 'report.txt').write_bytes(b'unrelated existing file')
    (tmp_path / 'report (1).txt').write_bytes(b'new task download')
    metadata = completed('report.txt')
    records = collect_downloads(tmp_path, [metadata], tmp_path / 'out')
    assert records[0]['copied'] is False
    assert 'exact saved filePath' in records[0]['copy_reason']
    assert not (tmp_path / 'out').exists()
    metadata['filePath'] = str(tmp_path / 'report (1).txt')
    records = collect_downloads(tmp_path, [metadata], tmp_path / 'out')
    assert records[0]['name'] == 'report.txt'
    assert records[0]['suggested_filename'] == 'report.txt'
    assert (tmp_path / 'out' / records[0]['path']).read_bytes() == b'new task download'
    assert (tmp_path / 'report.txt').read_bytes() == b'unrelated existing file'


def test_fifo_is_rejected_without_blocking(tmp_path):
    import os
    if not hasattr(os, 'mkfifo'):
        pytest.skip('POSIX named pipes only')
    fifo = tmp_path / 'pipe'
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match='regular file'):
        stage_uploads(tmp_path / 'upload-out', {'fifo': str(fifo)})
    with pytest.raises(ValueError, match='regular file'):
        collect_downloads(tmp_path, [completed('pipe', tmp_path)], tmp_path / 'download-out')
