import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

from proxy import relay


class CDPRelay(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(("127.0.0.1", 9222), timeout=10) as upstream:
                relay(self.request, upstream)
        except OSError:
            pass


class CDPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(*, generic=False):
    children = []
    server = None
    Path("/tmp/home").mkdir(exist_ok=True)
    if generic:
        for directory in ("/tmp/bu-j-downloads", "/tmp/bu-j-uploads"):
            Path(directory).mkdir(exist_ok=True)

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        children.append(subprocess.Popen([sys.executable,
            "/opt/generic/proxy.py" if generic else "/opt/web-benchmark/proxy.py"]))
        fixture = ([sys.executable, "/opt/generic/fixture_server.py"] if generic else [
            sys.executable, "-m", "http.server", "8766", "--bind", "127.0.0.1",
            "--directory", "/opt/fixture",
        ])
        children.append(subprocess.Popen(fixture, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        with open("/tmp/chrome.log", "w") as log:
            chrome = subprocess.Popen([
                "google-chrome", "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-background-networking", "--disable-component-update", "--disable-sync",
                "--no-first-run", "--no-default-browser-check", "--metrics-recording-only",
                "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--proxy-server=http://127.0.0.1:8888", "--proxy-bypass-list=<-loopback>",
                "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9222",
                "--user-data-dir=/tmp/web-chrome-profile", "--window-size=1120,780",
                "--lang=en-US", "--accept-lang=en-US",
                *(["--disable-extensions"] if generic else []), "about:blank",
            ], stdout=subprocess.DEVNULL, stderr=log, env={**os.environ, "TZ": "UTC"})
            children.append(chrome)
        server = CDPServer(("0.0.0.0", 9223), CDPRelay)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        for _ in range(600):
            if any(child.poll() is not None for child in children):
                raise RuntimeError("BENCHMARK_CHILD_EXITED; inspect /tmp/chrome.log")
            try:
                with urlopen("http://127.0.0.1:9222/json/version", timeout=1) as response:
                    version = json.load(response)
                if not version.get("webSocketDebuggerUrl"):
                    raise ValueError("missing browser websocket")
                Path("/tmp/ready.json").write_text(json.dumps({
                    "version": version, "cdp_port": 9223, "fixture_port": 8766,
                    "proxy_port": 8888, "chrome_no_sandbox": True,
                    **({"mode": "generic"} if generic else {}),
                }))
                break
            except (OSError, ValueError):
                time.sleep(.1)
        else:
            raise RuntimeError("CHROME_NOT_READY")
        while all(child.poll() is None for child in children):
            time.sleep(.2)
        raise RuntimeError("BENCHMARK_CHILD_EXITED")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
