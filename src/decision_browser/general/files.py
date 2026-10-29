from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 40 * 1024 * 1024


def _open_upload(filename):
    if os.name != "nt":
        return os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    generic_read, share_read_write_delete = 0x80000000, 0x7
    open_existing, open_reparse_point = 3, 0x00200000
    handle = create(os.fspath(filename), generic_read, share_read_write_delete, None,
                    open_existing, open_reparse_point, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise


def _read_file(filename: Path) -> bytes:
    descriptor = _open_upload(filename)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if os.name == "nt" and (not hasattr(info, "st_file_attributes") or
                info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise OSError(errno.ELOOP, "File reparse points are not allowed", filename)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise ValueError("file must be a regular file at most 10 MiB")
        data = source.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("file exceeds 10 MiB")
    return data


def _basename(name: str) -> str:
    if (not isinstance(name, str) or not name or name in (".", "..")
            or any(char in name for char in ("/", "\\", ":"))
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError("file name must be a plain basename")
    return name


def stage_uploads(output: Path, uploads: dict) -> tuple[dict, dict]:
    staged, receipts = {}, {}
    total = 0
    for token, filename in uploads.items():
        name = _basename(Path(filename).name)
        data = _read_file(Path(filename))
        total += len(data)
        if total > MAX_TOTAL:
            raise ValueError("upload budget exceeds 40 MiB")
        destination = Path(output).absolute() / "uploads" / uuid4().hex / name
        destination.parent.mkdir(parents=True, exist_ok=False)
        with destination.open("xb") as target:
            target.write(data)
        staged[token] = str(destination)
        receipts[token] = {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return staged, receipts


def collect_downloads(directory: Path | None, downloads: list[dict], output: Path) -> list[dict]:
    records = []
    total = 0
    if len(downloads) > 100:
        raise ValueError("download budget exceeds 100 files")
    for index, item in enumerate(downloads):
        record = {**item, "copied": False}
        records.append(record)
        name = item.get("suggested_filename") or item.get("name")
        if directory is None:
            record["copy_reason"] = "No download directory supplied; metadata only"
            continue
        if item.get("state") != "completed" or not item.get("filePath"):
            record["copy_reason"] = "Completed download metadata with an exact saved filePath required"
            continue
        saved = Path(item["filePath"])
        root = Path(directory).absolute()
        if not saved.is_absolute() or saved.parent != root or ".." in saved.parts:
            raise ValueError("completed download filePath must be directly within the supplied directory")
        saved_name = _basename(saved.name)
        name = name or saved_name
        if saved_name.lower().endswith((".crdownload", ".part", ".tmp")):
            record["copy_reason"] = "Download file is pending"
            continue
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or (os.name == "nt" and
                info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("download directory must be a regular directory without symlinks")
        try:
            data = _read_file(saved)
        except FileNotFoundError:
            record["copy_reason"] = "Completed download file is not available in the supplied directory"
            continue
        total += len(data)
        if total > MAX_TOTAL:
            raise ValueError("download budget exceeds 40 MiB")
        digest = hashlib.sha256(data).hexdigest()
        suffix = Path(saved_name).suffix
        if not re.fullmatch(r"\.[a-zA-Z0-9]{1,12}", suffix):
            suffix = ".bin"
        filename = f"{index:03d}-{digest}{suffix}"
        folder = Path(output) / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / filename).open("xb") as destination:
            destination.write(data)
        record.update(path="downloads/" + filename, name=name, bytes=len(data), sha256=digest, copied=True)
    return records
