"""Disposable container entrypoint: no host files, credentials or profiles."""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.request import urlopen

Path('/tmp/home').mkdir()
server = subprocess.Popen(['python', '/opt/fixture/server.py'], stdout=subprocess.DEVNULL)
chrome = subprocess.Popen(['google-chrome', '--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
    '--disable-background-networking', '--disable-component-update', '--no-first-run', '--no-default-browser-check',
    '--disable-sync', '--metrics-recording-only', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9222',
    '--user-data-dir=/tmp/chrome-profile', 'http://127.0.0.1:8765/'], stdout=subprocess.DEVNULL, stderr=open('/tmp/chrome.log', 'w'))
try:
    for _ in range(600):
        if chrome.poll() is not None:
            raise RuntimeError('CHROME_EXITED: ' + Path('/tmp/chrome.log').read_text())
        try:
            with urlopen('http://127.0.0.1:9222/json/version', timeout=1) as response:
                version = json.load(response)
            targets = json.load(urlopen('http://127.0.0.1:9222/json/list', timeout=1))
            tab = next(t for t in targets if t.get('url', '').startswith('http://127.0.0.1:8765/'))
            Path('/tmp/ready.json').write_text(json.dumps({'version': version, 'tab': tab}))
            break
        except (OSError, ValueError, StopIteration):
            time.sleep(.1)
    else:
        raise RuntimeError('CHROME_NOT_READY')
    chrome.wait()
finally:
    chrome.terminate()
    server.terminate()
    chrome.wait()
    server.wait()
