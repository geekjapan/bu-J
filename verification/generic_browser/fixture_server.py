"""Local procurement sandbox for independent browser verification."""

import argparse
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit


DOWNLOAD = b"Northstar procurement sandbox\nReference: NS-204\nStatus: quotation ready\n"
ACTIONS = ("request", "same_frame", "cross_frame", "shadow", "scroll", "upload", "download",
           "boundary_target", "confirm", "redirect_out")
MAX_BODY = 1024 * 1024


def make_server(root: str | Path = "/opt/generic/fixture", port: int = 8766) -> ThreadingHTTPServer:
    root = Path(root)
    state = {"counters": dict.fromkeys(ACTIONS, 0), "last": {}}
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, status, body, content_type="application/json; charset=utf-8", **headers):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(body)

        def record(self, action, result):
            with lock:
                state["counters"][action] += 1
                state["last"][action] = result
                return {"action": action, "count": state["counters"][action], **result}

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/state":
                with lock:
                    self.respond(200, state)
                return
            if path == "/redirect-out":
                self.record("redirect_out", {})
                self.respond(302, {}, Location=f"http://localhost:{self.server.server_port}/boundary-target")
                return
            if path in ("/boundary-target", "/confirm-action"):
                action = "boundary_target" if path == "/boundary-target" else "confirm"
                self.respond(200, self.record(action, {}))
                return
            if path == "/download":
                self.record("download", {"filename": "northstar-quotation.txt", "bytes": len(DOWNLOAD),
                                         "sha256": sha256(DOWNLOAD).hexdigest()})
                self.respond(200, DOWNLOAD, "text/plain; charset=utf-8",
                             Content_Disposition='attachment; filename="northstar-quotation.txt"')
                return
            pages = {"/": "index.html", "/same-frame": "frame.html", "/cross-frame": "frame.html",
                     "/detail": "detail.html", "/style.css": "style.css", "/boundaries": "boundaries.html"}
            if path not in pages:
                self.respond(404, {"error": "Unknown sandbox route"})
                return
            try:
                body = (root / pages[path]).read_text(encoding='utf-8')
            except OSError:
                self.respond(500, {"error": "Sandbox files unavailable"})
                return
            kind = "cross_frame" if path == "/cross-frame" else "same_frame"
            body = body.replace("__PORT__", str(self.server.server_port)).replace("__KIND__", kind)
            body = body.replace("__FRAME_LABEL__", "External warehouse" if kind == "cross_frame" else "Local warehouse")
            self.respond(200, body.encode(), "text/css; charset=utf-8" if path.endswith(".css") else "text/html; charset=utf-8")

        def do_POST(self):
            path = urlsplit(self.path).path
            action = path.removeprefix("/action/")
            if path != "/upload" and (not path.startswith("/action/") or action not in ACTIONS[:5]):
                self.respond(404, {"error": "Unknown sandbox action"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= MAX_BODY:
                self.respond(413, {"error": "A body of at most 1 MiB is required"})
                return
            body = self.rfile.read(length)
            if path == "/upload":
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data;"):
                    self.respond(400, {"error": "Choose a specification file"})
                    return
                message = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
                parts = list(message.iter_parts())
                files = [p for p in parts if p.get_param("name", header="content-disposition") == "specification"
                         and p.get_filename()]
                if len(files) != 1:
                    self.respond(400, {"error": "Choose one specification file"})
                    return
                data = files[0].get_payload(decode=True)
                if data is None:
                    self.respond(400, {"error": "Invalid specification file"})
                    return
                result = {"filename": Path(files[0].get_filename()).name, "bytes": len(data),
                          "sha256": sha256(data).hexdigest()}
                self.respond(200, self.record("upload", result))
                return
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self.respond(400, {"error": "Expected form fields"})
                return
            try:
                fields = parse_qs(body.decode("utf-8"), strict_parsing=True, errors="strict")
            except (UnicodeDecodeError, ValueError):
                self.respond(400, {"error": "Invalid form fields"})
                return
            value = fields.get("name", [""])[0].strip()
            if not value or len(value) > 200:
                self.respond(400, {"error": "Enter a name of 1 to 200 characters"})
                return
            result = {"name": value}
            if action == "request":
                department = fields.get("department", [""])[0]
                if department not in ("Research", "Operations", "Finance"):
                    self.respond(400, {"error": "Choose a department"})
                    return
                result.update(department=department, priority=fields.get("priority") == ["on"])
            self.respond(200, self.record(action, result))

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("GENERIC_FIXTURE_ROOT", "/opt/generic/fixture"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GENERIC_FIXTURE_PORT", "8766")))
    args = parser.parse_args()
    with make_server(args.root, args.port) as server:
        print(f"Procurement sandbox at http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
