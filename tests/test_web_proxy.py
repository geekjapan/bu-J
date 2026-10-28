import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from verification.web_benchmark import proxy


@pytest.mark.parametrize("host", ["google.com", "www.google.com", "en.wikipedia.org", "upload.wikimedia.org", "fonts.gstatic.com", "lh3.googleusercontent.com", "maps.googleapis.com", "lh3.ggpht.com"])
def test_approved_https_domains(host):
    assert proxy.destination(host + ":443", connect=True)[:2] == (host, 443)


@pytest.mark.parametrize("target", [
    "google.com.evil.test:443", "evilgoogle.com:443", "google.com.:443",
    "user:secret@google.com:443", "google.com:80", "google.com:invalid",
    "google.com:443/path", "google.com:443?query", "127.0.0.1:443",
    "localhost:443", "169.254.169.254:443", "management.azure.com:443",
    "google.com:443\r\nHost: evil.test", "foo..google.com:443",
])
def test_unapproved_connect_authorities_rejected(target):
    with pytest.raises(ValueError):
        proxy.destination(target, connect=True)


@pytest.mark.parametrize("target", [
    "http://localhost:8766/", "http://127.0.0.1:9222/", "http://127.0.0.2:8766/",
    "http://user:secret@127.0.0.1:8766/", "https://127.0.0.1:8766/",
    "http://google.com/", "http://127.0.0.1:8766/#fragment",
])
def test_only_exact_fixture_http_allowed(target):
    with pytest.raises(ValueError):
        proxy.destination(target)
    assert proxy.destination("http://127.0.0.1:8766/fixture.html?scenario=travel") == (
        "127.0.0.1", 8766, "/fixture.html?scenario=travel",
    )


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "192.0.2.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1"])
def test_every_dns_answer_must_be_public(monkeypatch, ip):
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: answers)
    monkeypatch.setattr(socket, "socket", lambda *args: pytest.fail("must reject before connecting"))
    with pytest.raises(ValueError, match="non-public"):
        proxy.open_destination("www.google.com", 443)


def test_connection_uses_validated_address_without_second_dns_lookup(monkeypatch):
    calls = []

    def resolve(*args, **kwargs):
        calls.append(args)
        assert len(calls) == 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    class Connection:
        def settimeout(self, value):
            pass

        def connect(self, address):
            assert address == ("8.8.8.8", 443)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", lambda *args: Connection())
    assert isinstance(proxy.open_destination("www.google.com", 443), Connection)


def test_fixture_works_through_tcp_proxy_without_forwarding_credentials(capsys):
    received = []

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"fixture response")

        def log_message(self, *args):
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 8766), Fixture)
    server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
    for service in [fixture, server]:
        threading.Thread(target=service.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(server.server_address) as client:
            client.sendall(b"GET http://127.0.0.1:8766/fixture.html?secret=hidden HTTP/1.0\r\nAuthorization: Bearer hidden\r\nCookie: hidden\r\n\r\n")
            response = b""
            while chunk := client.recv(4096):
                response += chunk
        assert b"200 OK" in response and response.endswith(b"fixture response")
        assert received == [{"Host": "127.0.0.1:8766", "Connection": "close"}]
    finally:
        for service in [server, fixture]:
            service.shutdown()
            service.server_close()
    assert "hidden" not in capsys.readouterr().out


def test_connect_preserves_bytes_sent_with_headers(monkeypatch):
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(self.request.recv(4096))

    echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
    server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
    monkeypatch.setattr(proxy, "open_destination", lambda host, port: socket.create_connection(echo.server_address))
    for service in [echo, server]:
        threading.Thread(target=service.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(server.server_address, timeout=2) as client:
            client.sendall(b"CONNECT www.google.com:443 HTTP/1.0\r\n\r\ntunnel payload")
            response = b""
            while chunk := client.recv(4096):
                response += chunk
        assert b"200 Connection Established" in response
        assert response.endswith(b"tunnel payload")
    finally:
        for service in [server, echo]:
            service.shutdown()
            service.server_close()
