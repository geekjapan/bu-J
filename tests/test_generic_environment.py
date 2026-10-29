import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from decision_browser.web import environment as env
from verification.generic_browser import proxy
from test_web_environment import DockerFake, RUN, BASE, IMAGE, sample


def test_generic_manifest_requires_matching_live_mode(tmp_path, monkeypatch):
    manifest, state, readiness = sample()
    manifest["mode"] = "generic"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(env, "inspect", lambda cid: state)
    monkeypatch.setattr(env, "ready", lambda cid: readiness)
    with pytest.raises(ValueError, match="mode"):
        env.validate_manifest(path, generic=True)
    readiness["mode"] = "generic"
    assert env.validate_manifest(path, generic=True) == manifest
    del manifest["mode"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="mode"):
        env.validate_manifest(path, generic=True)


def test_generic_lifecycle_uses_shared_isolation_and_cleanup(tmp_path, monkeypatch):
    fake = DockerFake()

    def docker(*args, **kwargs):
        result = fake(*args, **kwargs)
        if args[:2] == ("image", "inspect"):
            result.stdout = result.stdout.replace("bu-j-web-benchmark@", "bu-j-generic-browser@")
        elif args[0] == "exec":
            readiness = json.loads(result.stdout)
            readiness["mode"] = "generic"
            result.stdout = json.dumps(readiness)
        return result

    monkeypatch.setattr(env, "docker", docker)
    monkeypatch.setattr(env, "uuid4", lambda: __import__("uuid").UUID(RUN))
    with env.ManagedChrome(tmp_path, BASE, generic=True) as chrome:
        assert chrome.manifest["mode"] == "generic"
        assert env.validate_manifest(chrome.manifest_path, generic=True) == chrome.manifest
    build = next(command for command in fake.commands if command[0] == "build")
    assert str(env.ROOT / "verification/generic_browser/Dockerfile") in build
    launch = next(command for command in fake.commands if command[0] == "run")
    assert launch[-1] == "bu-j-generic-browser@" + IMAGE
    assert not any(flag in launch for flag in ("--mount", "--volume", "--env"))
    assert not fake.exists


@pytest.mark.parametrize("target,connect", [
    ("https://example.com/", False), ("http://example.com/", False),
    ("http://localhost:9222/", False), ("http://127.0.0.2:8766/", False),
    ("http://127.0.0.1:8766@evil.com/", False), ("example.com:80", True),
    ("user@example.com:443", True), ("example.com:443/a", True),
    ("example.com:443?query", True), ("example.com:443#fragment", True),
    ("127.0.0.1:443", True), ("[::1]:443", True), ("169.254.169.254:443", True),
    ("example.com:443\r\nHost:x", True), ("example_com:443", True),
    ("-bad.example:443", True), ("example.com.:443", True),
])
def test_proxy_rejects_invalid_destinations(target, connect):
    with pytest.raises(ValueError):
        proxy.destination(target, connect=connect)


@pytest.mark.parametrize("target,connect,expected", [
    ("example.net:443", True, ("example.net", 443, "/")),
    ("8.8.8.8:443", True, ("8.8.8.8", 443, "/")),
    ("[2606:4700:4700::1111]:443", True, ("2606:4700:4700::1111", 443, "/")),
    ("http://localhost:8766/a?q=1", False, ("localhost", 8766, "/a?q=1")),
    ("http://127.0.0.1:8766/", False, ("127.0.0.1", 8766, "/")),
])
def test_proxy_accepts_public_https_and_exact_fixture(target, connect, expected):
    assert proxy.destination(target, connect=connect) == expected


@pytest.mark.parametrize("bad", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1",
                                     "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "::ffff:127.0.0.1"])
def test_dns_mixed_public_private_results_are_rejected_before_connect(monkeypatch, bad):
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", (bad, 443))]
    monkeypatch.setattr(proxy.fixed.socket, "getaddrinfo", lambda *args, **kwargs: addresses)
    monkeypatch.setattr(proxy.fixed.socket, "socket", lambda *args: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="non-public"):
        proxy.open_destination("public.example", 443)


def test_dns_connection_uses_validated_address_without_second_lookup(monkeypatch):
    calls = []
    address = ("8.8.8.8", 443)
    monkeypatch.setattr(proxy.fixed.socket, "getaddrinfo", lambda *args, **kwargs:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", address)])

    class Connection:
        def settimeout(self, value):
            pass

        def connect(self, value):
            calls.append(value)

    connection = Connection()
    monkeypatch.setattr(proxy.fixed.socket, "socket", lambda *args: connection)
    assert proxy.open_destination("public.example", 443) is connection
    assert calls == [address]


def test_proxy_forwards_fixture_form_and_denies_private_connect():
    received = []

    class Fixture(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"saved")

        def log_message(self, *args):
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 8766), Fixture)
    server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
    for instance in (fixture, server):
        threading.Thread(target=instance.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(server.server_address) as client:
            client.sendall(b"POST http://localhost:8766/form HTTP/1.0\r\nContent-Length: 3\r\n\r\na=1")
            response = bytearray()
            while chunk := client.recv(65536):
                response.extend(chunk)
        assert b"200 OK" in response and response.endswith(b"saved")
        assert received == [("/form", b"a=1")]
        with socket.create_connection(server.server_address) as client:
            client.sendall(b"CONNECT 169.254.169.254:443 HTTP/1.0\r\n\r\n")
            assert b"403" in client.recv(65536)
    finally:
        for instance in (server, fixture):
            instance.shutdown()
            instance.server_close()
