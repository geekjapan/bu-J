from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import uuid4

MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 40 * 1024 * 1024


def _open_upload(filename):
    if os.name != "nt":
        return os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
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


def stage_uploads(container, uploads):
    staged, receipts = {}, {}
    for token, filename in uploads.items():
        descriptor = _open_upload(filename)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if os.name == "nt" and (not hasattr(info, "st_file_attributes") or
                    info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise OSError(errno.ELOOP, "Upload reparse points are not allowed", filename)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                raise ValueError("upload must be a regular file at most 10 MiB")
            data = source.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError("upload exceeds 10 MiB")
        remote = "/tmp/bu-j-uploads/" + uuid4().hex + "/" + Path(filename).name
        script = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(); p.write_bytes(sys.stdin.buffer.read())"
        subprocess.run(["docker", "exec", "-i", container, "python", "-c", script, remote],
                       input=data, check=True, capture_output=True, timeout=30)
        staged[token] = remote
        receipts[token] = {"name": Path(filename).name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return staged, receipts


def collect_downloads(container, output):
    script = '''import base64,json,pathlib,stat
root=pathlib.Path('/tmp/bu-j-downloads')
items=[]
total=0
for path in sorted(root.iterdir()):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>10485760:
        raise ValueError('download must be a regular file at most 10 MiB')
    total+=info.st_size
    if total>41943040 or len(items)>=100:
        raise ValueError('download budget exceeded')
    items.append({'name':path.name,'data':base64.b64encode(path.read_bytes()).decode()})
print(json.dumps(items))
'''
    result = subprocess.run(["docker", "exec", container, "python", "-c", script],
                            check=True, capture_output=True, timeout=30)
    records = []
    for index, item in enumerate(json.loads(result.stdout)):
        name = item["name"]
        if name.endswith(".crdownload"):
            continue
        data = base64.b64decode(item["data"], validate=True)
        digest = hashlib.sha256(data).hexdigest()
        suffix = Path(name).suffix
        if not re.fullmatch(r"\.[a-zA-Z0-9]{1,12}", suffix):
            suffix = ".bin"
        filename = f"{index:03d}-{digest}{suffix}"
        folder = Path(output) / "downloads"
        folder.mkdir(exist_ok=True)
        with (folder / filename).open("xb") as destination:
            destination.write(data)
        records.append({"path": "downloads/" + filename, "name": name, "bytes": len(data), "sha256": digest})
    return records
