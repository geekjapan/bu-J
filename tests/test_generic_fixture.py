from hashlib import sha256
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from verification.generic_browser.fixture_server import DOWNLOAD, make_server


@pytest.fixture
def sandbox():
    root = Path(__file__).resolve().parents[1] / "fixtures" / "generic_browser"
    with make_server(root, port=0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}"
        server.shutdown()
        thread.join()


def read_json(url, fields=None):
    request = Request(url, data=None if fields is None else urlencode(fields).encode())
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def test_actions_have_independent_counters_and_retain_values(sandbox):
    initial = read_json(sandbox + "/state")
    assert set(initial["counters"]) == {"request", "same_frame", "cross_frame", "shadow", "scroll", "upload", "download",
                                       "boundary_target", "confirm", "redirect_out"}
    assert not any(initial["counters"].values())
    request = {"name": "Ada <Lab>", "department": "Research", "priority": "on"}
    result = read_json(sandbox + "/action/request", request)
    assert result == {"action": "request", "count": 1, "name": "Ada <Lab>", "department": "Research", "priority": True}
    assert read_json(sandbox + "/action/request", {"name": "Jo", "department": "Finance"})["priority"] is False
    for action in ("same_frame", "cross_frame", "shadow", "scroll"):
        assert read_json(sandbox + "/action/" + action, {"name": "Contact " + action})["count"] == 1
    state = read_json(sandbox + "/state")
    assert state["counters"] == dict(request=2, same_frame=1, cross_frame=1, shadow=1, scroll=1, upload=0, download=0,
                                     boundary_target=0, confirm=0, redirect_out=0)
    assert state["last"]["shadow"] == {"name": "Contact shadow"}


def test_upload_and_download_verify_real_bytes(sandbox):
    data = b"specification\x00\xff\r\n"
    multipart = (b'--fixture-boundary\r\nContent-Disposition: form-data; name="specification"; filename="spec.bin"\r\n'
                 b'Content-Type: application/octet-stream\r\n\r\n' + data + b'\r\n--fixture-boundary--\r\n')
    request = Request(sandbox + "/upload", data=multipart,
                      headers={"Content-Type": "multipart/form-data; boundary=fixture-boundary"})
    with urlopen(request, timeout=5) as response:
        assert json.load(response) == {"action": "upload", "count": 1, "filename": "spec.bin",
                                       "bytes": len(data), "sha256": sha256(data).hexdigest()}
    with urlopen(sandbox + "/download", timeout=5) as response:
        assert response.read() == DOWNLOAD
        assert response.headers["Content-Disposition"] == 'attachment; filename="northstar-quotation.txt"'
    state = read_json(sandbox + "/state")
    assert state["counters"]["upload"] == state["counters"]["download"] == 1
    assert state["last"]["download"]["sha256"] == sha256(DOWNLOAD).hexdigest()
    assert state["last"]["upload"]["sha256"] == sha256(data).hexdigest()


def test_pages_use_server_port_and_distinct_frame_origins(sandbox):
    with urlopen(sandbox, timeout=5) as response:
        page = response.read().decode()
    assert 'src="/same-frame"' in page
    assert f'src="{sandbox.replace("127.0.0.1", "localhost")}/cross-frame"' in page
    assert "attachShadow({mode: 'open'})" in page
    assert 'target="_blank"' in page
    for route, expected in (("/same-frame", "Local warehouse contact"), ("/cross-frame", "External warehouse contact"),
                            ("/detail", "Quotation status: ready for review."), ("/style.css", ".scroll-space")):
        with urlopen(sandbox + route, timeout=5) as response:
            assert expected in response.read().decode()
    assert not any(read_json(sandbox + "/state")["counters"].values())


@pytest.mark.parametrize("route,body,headers,status", [
    ("/action/request", b"name=Ada&department=Unknown", {}, 400),
    ("/action/shadow", b"name=", {}, 400),
    ("/action/shadow", b"name=%FF", {}, 400),
    ("/action/unknown", b"name=Ada", {}, 404),
    ("/upload", b"bad", {"Content-Type": "multipart/form-data; boundary=nope"}, 400),
    ("/upload", b"bad", {}, 400),
    ("/action/scroll", b"", {}, 413),
    ("/action/scroll", b"x", {"Content-Length": str(1024 * 1024 + 1)}, 413),
])
def test_invalid_submissions_do_not_change_counters(sandbox, route, body, headers, status):
    with pytest.raises(HTTPError) as error:
        urlopen(Request(sandbox + route, data=body, headers=headers), timeout=5)
    assert error.value.code == status
    assert not any(read_json(sandbox + "/state")["counters"].values())


def test_unknown_paths_cannot_read_outside_fixture(sandbox):
    with pytest.raises(HTTPError) as error:
        urlopen(sandbox + "/../pyproject.toml", timeout=5)
    assert error.value.code == 404


def test_boundary_targets_record_actual_http_requests(sandbox):
    assert read_json(sandbox + "/boundary-target") == {"action": "boundary_target", "count": 1}
    assert read_json(sandbox + "/confirm-action") == {"action": "confirm", "count": 1}
    state = read_json(sandbox + "/state")
    assert state["counters"]["boundary_target"] == state["counters"]["confirm"] == 1
    assert all(value == 0 for key, value in state["counters"].items() if key not in ("boundary_target", "confirm"))


def test_boundary_redirect_and_controls_use_distinct_exact_origins(sandbox):
    from urllib.request import HTTPRedirectHandler, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, newurl):
            return None

    with pytest.raises(HTTPError) as error:
        build_opener(NoRedirect).open(sandbox + "/redirect-out", timeout=5)
    assert error.value.code == 302
    destination = sandbox.replace("127.0.0.1", "localhost") + "/boundary-target"
    assert error.value.headers["Location"] == destination
    with urlopen(sandbox + "/boundaries", timeout=5) as response:
        page = response.read().decode()
    assert destination in page
    assert "if (confirm('Apply sandbox action?')) fetch('/confirm-action')" in page
    state = read_json(sandbox + "/state")
    assert state["counters"]["redirect_out"] == 1
    assert state["counters"]["boundary_target"] == state["counters"]["confirm"] == 0
