from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from decision_browser.web import environment as env


RUN = "00000000-0000-4000-8000-000000000001"
CID = "a" * 64
IMAGE = "sha256:" + "b" * 64
BASE = "sha256:" + "c" * 64


def sample():
    manifest = dict(run_id=RUN, name="bu-j-web-" + RUN, container=CID,
                    image_id=IMAGE, port=45678, endpoint="ws://127.0.0.1:45678/devtools/browser/abc-123")
    ports = {"9223/tcp": [{"HostIp": "127.0.0.1", "HostPort": "45678"}]}
    state = dict(Id=CID, Name="/" + manifest["name"], Image=IMAGE, State={"Running": True},
                 Config={"Labels": {env.LABEL: RUN}}, Mounts=[],
                 HostConfig=dict(ReadonlyRootfs=True, CapDrop=["ALL"], SecurityOpt=["no-new-privileges"],
                                 NetworkMode="bridge", IpcMode="private", PidsLimit=256,
                                 Tmpfs={"/tmp": "rw,nosuid,nodev,size=1g"}, PortBindings=deepcopy(ports)),
                 NetworkSettings=dict(Networks={"bridge": {}}, Ports=ports))
    ready = {"version": {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc-123"}}
    return manifest, state, ready


def test_manifest_requires_live_identity_boundary_and_browser_uuid(tmp_path, monkeypatch):
    manifest, state, ready = sample()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(env, "inspect", lambda cid: deepcopy(state))
    monkeypatch.setattr(env, "ready", lambda cid: deepcopy(ready))
    assert env.validate_manifest(path) == manifest
    ready["version"]["webSocketDebuggerUrl"] += "stale"
    with pytest.raises(ValueError, match="endpoint"):
        env.validate_manifest(path)


@pytest.mark.parametrize("section,key,value", [
    ("HostConfig", "Privileged", True), ("HostConfig", "ReadonlyRootfs", False),
    ("HostConfig", "CapAdd", ["SYS_ADMIN"]), ("HostConfig", "CapDrop", []),
    ("HostConfig", "PidMode", "host"), ("HostConfig", "IpcMode", "host"),
    ("HostConfig", "Devices", [{"PathOnHost": "/dev/foo"}]),
    ("HostConfig", "NetworkMode", "host"), ("HostConfig", "SecurityOpt", []),
    ("HostConfig", "PidsLimit", -1), ("HostConfig", "Binds", ["/host:/guest"]),
    ("HostConfig", "PublishAllPorts", True),
    ("Config", "Labels", {env.LABEL: "unowned"}),
])
def test_rejects_weakened_boundary(section, key, value):
    manifest, state, ready = sample()
    state[section][key] = value
    with pytest.raises(ValueError):
        env._validate(manifest, state, ready)


def test_rejects_public_or_extra_port_and_mount():
    for mutate in (
        lambda s: s["NetworkSettings"]["Ports"]["9223/tcp"][0].update(HostIp="0.0.0.0"),
        lambda s: s["NetworkSettings"]["Ports"].update({"9222/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2"}]}),
        lambda s: s.update(Mounts=[{"Source": "/host"}]),
        lambda s: s.update(Image=BASE),
        lambda s: s["NetworkSettings"]["Networks"].update(other={}),
    ):
        manifest, state, ready = sample()
        mutate(state)
        with pytest.raises(ValueError):
            env._validate(manifest, state, ready)


class DockerFake:
    def __init__(self):
        self.commands = []
        self.exists = False
        self.log_failure = False
        self.run_failure = False

    def __call__(self, *args, **kwargs):
        self.commands.append(args)
        manifest, state, readiness = sample()
        if args[:2] == ("image", "inspect"):
            base = args[-1] == "bu-j-chrome-verifier:local" or args[-1].startswith("bu-j-web-base:")
            data = [{"Id": BASE if base else IMAGE, "RootFS": {"Layers": [BASE] if base else [BASE, IMAGE]},
                     "RepoDigests": ["bu-j-web-benchmark@" + IMAGE]}]
        elif args[0] == "tag":
            data = None
        elif args[0] == "build":
            data = None
        elif args[0] == "run":
            self.exists = True
            if self.run_failure:
                raise TimeoutError("uncertain launch")
            return SimpleNamespace(stdout=CID + "\n", stderr="")
        elif args[0] == "inspect":
            data = [state]
        elif args[0] == "exec":
            data = readiness
        elif args[0] == "logs":
            if self.log_failure:
                raise OSError("logs unavailable")
            data = None
        elif args[0] == "rm":
            assert args == ("rm", "-f", CID)
            self.exists = False
            data = None
        elif args[0] == "ps":
            return SimpleNamespace(stdout=CID if self.exists else "", stderr="")
        else:
            raise AssertionError(args)
        return SimpleNamespace(stdout=json.dumps(data) if data is not None else "", stderr="")


def setup_fake(monkeypatch):
    fake = DockerFake()
    monkeypatch.setattr(env, "docker", fake)
    monkeypatch.setattr(env, "uuid4", lambda: __import__("uuid").UUID(RUN))
    return fake


def test_context_builds_immutable_image_and_removes_only_owned_container(tmp_path, monkeypatch):
    fake = setup_fake(monkeypatch)
    fake.log_failure = True
    with env.ManagedChrome(tmp_path, BASE) as chrome:
        assert chrome.manifest == sample()[0]
        assert env.validate_manifest(chrome.manifest_path) == chrome.manifest
    assert not fake.exists
    build = next(c for c in fake.commands if c[0] == "build")
    assert f"BASE_IMAGE=bu-j-web-base:{RUN}" in build
    launch = next(c for c in fake.commands if c[0] == "run")
    assert launch[-1] == "bu-j-web-benchmark@" + IMAGE
    assert "127.0.0.1::9223" in launch
    assert "--env" not in launch and "--mount" not in launch and "--volume" not in launch
    assert json.loads((tmp_path / "cleanup.json").read_text())["absent"] is True
    assert all((tmp_path / name).exists() for name in ("image.json", "build.json", "run.json", "inspect.json", "ready.json", "manifest.json"))


def test_uncertain_launch_discovers_owned_name_and_label_before_cleanup(tmp_path, monkeypatch):
    fake = setup_fake(monkeypatch)
    fake.run_failure = True
    with pytest.raises(TimeoutError):
        with env.ManagedChrome(tmp_path):
            pass
    discovery = next(c for c in fake.commands if c[0] == "ps")
    assert f"name=^/bu-j-web-{RUN}$" in discovery
    assert f"label={env.LABEL}={RUN}" in discovery
    assert not fake.exists
