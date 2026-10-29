"""File transport for the optional, disposable Docker verification fixture only."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from decision_browser.general import files

DOWNLOAD_DIRECTORY = '/tmp/bu-j-downloads'


def stage_uploads(container, output, uploads):
    local, receipts = files.stage_uploads(output, uploads)
    staged = {}
    for token, filename in local.items():
        remote = '/tmp/bu-j-uploads/' + uuid4().hex + '/' + Path(filename).name
        script = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True); p.open('xb').write(sys.stdin.buffer.read())"
        subprocess.run(['docker', 'exec', '-i', container, 'python', '-c', script, remote],
                       input=files._read_file(Path(filename)), check=True, capture_output=True, timeout=30)
        staged[token] = remote
    return staged, receipts


def collect_downloads(container, downloads, output):
    names = list(dict.fromkeys(files._basename(item['guid'])
                              for item in downloads if item.get('state') == 'completed'
                              and item.get('guid')))
    if len(names) > 100:
        raise ValueError('download budget exceeds 100 files')
    script = '''import base64,json,os,pathlib,stat,sys
root=pathlib.Path(sys.argv[1])
items=[]
total=0
for name in json.loads(sys.argv[2]):
    if name in ('.','..') or '/' in name or '\\\\' in name or ':' in name:
        raise ValueError('download must be a basename')
    try:
        descriptor=os.open(root/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:
        continue
    with os.fdopen(descriptor,'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>10485760:
            raise ValueError('download must be a regular file at most 10 MiB')
        data=source.read(10485761)
    if len(data)>10485760:
        raise ValueError('download exceeds 10 MiB')
    total+=len(data)
    if total>41943040:
        raise ValueError('download budget exceeded')
    items.append({'name':name,'data':base64.b64encode(data).decode()})
print(json.dumps(items))
'''
    result = subprocess.run(['docker', 'exec', container, 'python', '-c', script,
                             DOWNLOAD_DIRECTORY, json.dumps(names)],
                            check=True, capture_output=True, timeout=30)
    with TemporaryDirectory(prefix='bu-j-container-downloads-') as scratch:
        directory = Path(scratch)
        for item in json.loads(result.stdout):
            name = files._basename(item['name'])
            if name not in names:
                raise ValueError('Container returned an unrequested download')
            with (directory / name).open('xb') as destination:
                destination.write(base64.b64decode(item['data'], validate=True))
        local_metadata = [{**item, 'filePath': str(directory / item['guid'])}
                          if item.get('state') == 'completed' and item.get('guid') in names
                          else {key: value for key, value in item.items() if key != 'filePath'}
                          for item in downloads]
        records = files.collect_downloads(directory, local_metadata, output)
        for record, item in zip(records, downloads):
            if record.get('filePath'):
                record['filePath'] = DOWNLOAD_DIRECTORY + '/' + item['guid']
        return records
