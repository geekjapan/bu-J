from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import select
import socket
from urllib.parse import urlsplit


ALLOWED_DOMAINS = (
    "google.com", "gstatic.com", "googleusercontent.com", "googleapis.com",
    "ggpht.com", "wikipedia.org", "wikimedia.org",
)
FIXTURE = ("127.0.0.1", 8766)


def destination(target, *, connect=False):
    """Parse the sole HTTP exception or an approved HTTPS CONNECT authority."""
    if any(ord(char) <= 32 or ord(char) >= 127 for char in target):
        raise ValueError("invalid target")
    parsed = urlsplit("//" + target if connect else target)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("invalid authority")
    host = parsed.hostname
    port = parsed.port
    if connect:
        if parsed.path or parsed.query or port != 443 or host is None:
            raise ValueError("HTTPS port 443 required")
        if not any(host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS):
            raise ValueError("domain denied")
        if not all(part and all(c.isalnum() or c == "-" for c in part) for part in host.split(".")):
            raise ValueError("invalid domain")
    elif parsed.scheme != "http" or (host, port) != FIXTURE:
        raise ValueError("only local fixture HTTP permitted")
    return host, port, (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")


def open_destination(host, port):
    if (host, port) == FIXTURE:
        return socket.create_connection(FIXTURE, timeout=10)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("empty DNS result")
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError("non-public DNS result")
    last_error = None
    for family, kind, protocol, _, address in addresses:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(10)
            connection.connect(address)
            return connection
        except OSError as error:
            last_error = error
            connection.close()
    raise OSError("validated destinations unreachable") from last_error


def relay(client, upstream):
    while True:
        ready, _, _ = select.select([client, upstream], [], [], 60)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (upstream if source is client else client).sendall(data)


class ProxyHandler(BaseHTTPRequestHandler):
    rbufsize = 0  # CONNECT bytes must remain in the socket for the tunnel.

    def log_message(self, *args):
        pass

    def handle_request(self, *, connect=False):
        host = None
        status = 403
        try:
            host, port, path = destination(self.path, connect=connect)
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                raise ValueError("request body denied")
            upstream = open_destination(host, port)
        except (ValueError, OSError):
            self.send_error(status, "Destination denied or unavailable")
        else:
            with upstream:
                status = 200
                if connect:
                    self.send_response(200, "Connection Established")
                    self.end_headers()
                else:
                    upstream.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode("ascii"))
                try:
                    relay(self.connection, upstream)
                except OSError:
                    pass
        finally:
            self.close_connection = True
            print(json.dumps({"method": self.command, "host": host, "status": status}), flush=True)

    def do_CONNECT(self):
        self.handle_request(connect=True)

    def do_GET(self):
        self.handle_request()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8888), ProxyHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
