import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[3]
LABEL = "bu-j.web-benchmark"
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")


def docker(*args, check=True, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          check=check, timeout=timeout)


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _output(error, name):
    value = getattr(error, name, None) or ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def inspect(container):
    return json.loads(docker("inspect", container).stdout)[0]


def ready(container):
    return json.loads(docker("exec", container, "cat", "/tmp/ready.json", timeout=5).stdout)


def _validate(manifest, state, readiness):
    host = state["HostConfig"]
    run_id = manifest["run_id"]
    if (str(UUID(run_id)) != run_id or manifest["name"] != f"bu-j-web-{run_id}"
            or not IMAGE_ID.fullmatch(manifest["image_id"])
            or not CONTAINER_ID.fullmatch(manifest["container"])):
        raise ValueError("Invalid managed Chrome identity")
    if (state["Id"] != manifest["container"] or state["Name"] != "/" + manifest["name"]
            or state["Image"] != manifest["image_id"]
            or state["Config"].get("Labels", {}).get(LABEL) != run_id
            or not state["State"]["Running"]):
        raise ValueError("Managed Chrome ownership does not match")
    if (host.get("ReadonlyRootfs") is not True or state.get("Mounts")
            or host.get("Binds") or host.get("Privileged") or host.get("CapAdd")
            or {str(cap).upper() for cap in host.get("CapDrop", [])} != {"ALL"}
            or host.get("SecurityOpt") not in (["no-new-privileges"], ["no-new-privileges:true"])
            or host.get("NetworkMode") != "bridge"
            or set(state["NetworkSettings"]["Networks"]) != {"bridge"}
            or host.get("PidMode") or host.get("IpcMode") not in ("private", "")
            or host.get("Devices") or host.get("DeviceRequests") or host.get("DeviceCgroupRules")
            or host.get("PidsLimit") != 256 or set(host.get("Tmpfs", {})) != {"/tmp"}):
        raise ValueError("Managed Chrome isolation does not match")
    expected = {"9223/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(manifest["port"])}]}
    requested = host.get("PortBindings", {})
    if (requested not in (expected, {"9223/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]})
            or {k: v for k, v in state["NetworkSettings"]["Ports"].items() if v} != expected
            or host.get("PublishAllPorts")):
        raise ValueError("Managed Chrome published ports do not match")
    raw = urlsplit(readiness["version"]["webSocketDebuggerUrl"])
    if (raw.scheme != "ws" or raw.hostname != "127.0.0.1" or raw.port != 9222
            or raw.username or raw.password or raw.query or raw.fragment
            or not re.fullmatch(r"/devtools/browser/[a-zA-Z0-9-]+", raw.path)):
        raise ValueError("Invalid managed Chrome ready websocket")
    if type(manifest["port"]) is not int or not 1 <= manifest["port"] <= 65535:
        raise ValueError("Invalid managed Chrome port")
    endpoint = f"ws://127.0.0.1:{manifest['port']}{raw.path}"
    if manifest["endpoint"] != endpoint:
        raise ValueError("Managed Chrome endpoint does not match live readiness")
    return manifest


def validate_manifest(path):
    manifest = json.loads(Path(path).read_text())
    if not CONTAINER_ID.fullmatch(manifest.get("container", "")):
        raise ValueError("Invalid managed Chrome container ID")
    return _validate(manifest, inspect(manifest["container"]), ready(manifest["container"]))


class ManagedChrome:
    def __init__(self, evidence: Path, base_image_id=None):
        self.evidence = Path(evidence)
        self.base_image_id = base_image_id
        self.run_id = str(uuid4())
        self.name = f"bu-j-web-{self.run_id}"
        self.container = None
        self.launch_attempted = False
        self.manifest = None
        self.manifest_path = self.evidence / "manifest.json"

    def __enter__(self):
        self.evidence.mkdir(parents=True, exist_ok=True)
        try:
            dockerfile = ROOT / "verification/web_benchmark/Dockerfile"
            if not dockerfile.is_file():
                raise RuntimeError("Web benchmark Docker resources missing from repository root")
            base_info = json.loads(docker("image", "inspect", "--platform", "linux/amd64", "bu-j-chrome-verifier:local").stdout)[0]
            base = base_info["Id"]
            if not IMAGE_ID.fullmatch(base) or self.base_image_id not in (None, base):
                raise ValueError("Captured base image does not match existing verifier image")
            base_ref = f"bu-j-web-base:{self.run_id}"
            docker("tag", "bu-j-chrome-verifier:local", base_ref)
            if json.loads(docker("image", "inspect", "--platform", "linux/amd64", base_ref).stdout)[0]["Id"] != base:
                raise ValueError("Base image alias changed")
            save(self.evidence / "image.json", {"base_image_id": base, "base_reference": base_ref})
            tag = f"bu-j-web-benchmark:{self.run_id}"
            command = ["build", "--platform", "linux/amd64", "--build-arg", f"BASE_IMAGE={base_ref}", "-f", str(dockerfile), "-t", tag, str(ROOT)]
            try:
                built = docker(*command, timeout=600)
            except subprocess.SubprocessError as error:
                save(self.evidence / "build.json", {"argv": command, "exception_type": type(error).__name__,
                     "stdout": _output(error, "stdout"), "stderr": _output(error, "stderr")})
                raise
            save(self.evidence / "build.json", {"argv": command, "stdout": built.stdout, "stderr": built.stderr})
            built_info = json.loads(docker("image", "inspect", "--platform", "linux/amd64", tag).stdout)[0]
            image_id = built_info["Id"]
            if not IMAGE_ID.fullmatch(image_id):
                raise ValueError("Built image has no immutable image ID")
            layers = base_info["RootFS"]["Layers"]
            if not layers or built_info["RootFS"]["Layers"][:len(layers)] != layers:
                raise ValueError("Built image does not contain the captured base layers")
            run_ref = next(ref for ref in built_info["RepoDigests"]
                           if re.fullmatch(r"bu-j-web-benchmark@sha256:[0-9a-f]{64}", ref))
            platform_image_id, image_id = image_id, run_ref.split("@", 1)[1]
            save(self.evidence / "image.json", {"base_image_id": base, "base_reference": base_ref,
                 "image_id": image_id, "platform_image_id": platform_image_id, "run_reference": run_ref})
            command = ["run", "-d", "--platform", "linux/amd64", "--name", self.name, "--label", f"{LABEL}={self.run_id}",
                       "--network", "bridge", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
                       "--publish", "127.0.0.1::9223", run_ref]
            self.launch_attempted = True
            save(self.evidence / "run.json", {"argv": command, "status": "STARTING"})
            launched = docker(*command)
            self.container = launched.stdout.strip()
            save(self.evidence / "run.json", {"argv": command, "container": self.container})
            if not CONTAINER_ID.fullmatch(self.container):
                self.container = None
                raise ValueError("Docker did not return an immutable container ID")
            deadline = time.monotonic() + 60
            while True:
                try:
                    readiness = ready(self.container)
                    break
                except (subprocess.SubprocessError, ValueError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Managed Chrome readiness deadline exceeded") from None
                    time.sleep(.1)
            state = inspect(self.container)
            port = int(state["NetworkSettings"]["Ports"]["9223/tcp"][0]["HostPort"])
            path = urlsplit(readiness["version"]["webSocketDebuggerUrl"]).path
            self.manifest = dict(endpoint=f"ws://127.0.0.1:{port}{path}", container=self.container,
                                 name=self.name, run_id=self.run_id, image_id=image_id, port=port)
            save(self.evidence / "inspect.json", state)
            save(self.evidence / "ready.json", readiness)
            _validate(self.manifest, state, readiness)
            save(self.manifest_path, self.manifest)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        if self.container is None and self.launch_attempted:
            found = docker("ps", "-aq", "--filter", f"name=^/{self.name}$", "--filter",
                           f"label={LABEL}={self.run_id}")
            ids = found.stdout.split()
            if len(ids) == 1:
                state = inspect(ids[0])
                if state["Name"] == "/" + self.name and state["Config"].get("Labels", {}).get(LABEL) == self.run_id:
                    self.container = state["Id"]
        if self.container is not None:
            try:
                logs = docker("logs", self.container, check=False)
                save(self.evidence / "logs.json", {"stdout": logs.stdout, "stderr": logs.stderr})
            except Exception:
                pass
            finally:
                docker("rm", "-f", self.container)
            remaining = docker("ps", "-aq", "--no-trunc", "--filter", f"id={self.container}")
            absent = self.container not in remaining.stdout.split()
            save(self.evidence / "cleanup.json", {"container": self.container, "absent": absent})
            if not absent:
                raise RuntimeError("Managed Chrome cleanup was not confirmed")
        return False
