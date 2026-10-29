from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


spec = importlib.util.spec_from_file_location(
    "fixed_browser_proxy", Path(__file__).resolve().parents[1] / "web_benchmark" / "proxy.py")
fixed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixed)
relay = fixed.relay
FIXTURE_HOSTS = {"127.0.0.1", "localhost"}
MAX_BODY = 16 * 1024 * 1024


def destination(target, *, connect=False):
    if any(ord(char) <= 32 or ord(char) >= 127 for char in target):
        raise ValueError("invalid target")
    parsed = urlsplit("//" + target if connect else target)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("invalid authority")
    host, port = parsed.hostname, parsed.port
    if connect:
        if parsed.path or parsed.query or port != 443 or host is None:
            raise ValueError("HTTPS port 443 required")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                          for label in host.split(".")):
                raise ValueError("invalid domain") from None
        else:
            if not address.is_global or address.is_multicast or address.is_reserved:
                raise ValueError("non-public address")
    elif parsed.scheme != "http" or host not in FIXTURE_HOSTS or port != 8766:
        raise ValueError("only local fixture HTTP permitted")
    return host, port, (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")


def open_destination(host, port):
    if host in FIXTURE_HOSTS and port == 8766:
        return fixed.open_destination("127.0.0.1", 8766)
    if port != 443:
        raise ValueError("HTTPS port 443 required")
    return fixed.open_destination(host, port)


class ProxyHandler(BaseHTTPRequestHandler):
    rbufsize = 0  # CONNECT bytes must stay in the socket for the tunnel.

    def log_message(self, *args):
        pass

    def handle_request(self, *, connect=False):
        host = None
        status = 403
        try:
            host, port, path = destination(self.path, connect=connect)
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
                raise ValueError("ambiguous request body")
            length = int(lengths[0]) if lengths else 0
            if not 0 <= length <= MAX_BODY or (connect and length):
                raise ValueError("invalid request body")
            self.connection.settimeout(10)
            body = bytearray()
            while len(body) < length:
                chunk = self.rfile.read(min(65536, length - len(body)))
                if not chunk:
                    raise ValueError("incomplete request body")
                body.extend(chunk)
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
                    ignored = {"host", "connection", "proxy-connection", "proxy-authorization", "keep-alive", "upgrade"}
                    ignored.update(part.strip().lower() for part in self.headers.get("Connection", "").split(","))
                    headers = "".join(f"{key}: {value}\r\n" for key, value in self.headers.items()
                                      if key.lower() not in ignored)
                    upstream.sendall((f"{self.command} {path} HTTP/1.0\r\nHost: {host}:{port}\r\n"
                                      f"{headers}Connection: close\r\n\r\n").encode("latin-1") + body)
                try:
                    relay(self.connection, upstream)
                except OSError:
                    pass
        finally:
            self.close_connection = True
            print(json.dumps({"method": self.command, "host": host, "status": status}), flush=True)

    def do_CONNECT(self):
        self.handle_request(connect=True)

    do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_request


def main():
    ThreadingHTTPServer(("127.0.0.1", 8888), ProxyHandler).serve_forever()


if __name__ == "__main__":
    main()
