"""Exercise the pinned Python image, a locally built Go image, and Python rollback."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_IMAGE = "ghcr.io/imymi/rss-watch@sha256:a3d405242bdaa12a25bbf89a3b7e74c951bfe52f3fa054f59648b0d92f6e0156"


def certificates(directory):
    def openssl(*args):
        subprocess.run(
            ["openssl", *args], cwd=directory, check=True, capture_output=True, timeout=30,
        )
    (directory / "ca.cnf").write_text(
        "[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=ca\n"
        "[dn]\nCN=RSS Watch rehearsal CA\n"
        "[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
    )
    (directory / "server.cnf").write_text(
        "[server]\nbasicConstraints=critical,CA:FALSE\n"
        "subjectAltName=DNS:open.feishu.cn\nextendedKeyUsage=serverAuth\n"
    )
    openssl("req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "2",
            "-config", "ca.cnf", "-keyout", "ca.key", "-out", "ca.pem")
    openssl("req", "-new", "-nodes", "-newkey", "rsa:2048", "-subj", "/CN=open.feishu.cn",
            "-keyout", "server.key", "-out", "server.csr")
    openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-extfile", "server.cnf", "-extensions", "server",
            "-days", "2", "-out", "server.pem")
    # The CA private key is never mounted, and the worker receives only the CA certificate.
    exported = directory / "fixtures"
    exported.mkdir(mode=0o755)
    for name in ("ca.pem", "server.pem", "server.key"):
        shutil.copyfile(directory / name, exported / name)
        (exported / name).chmod(0o644)
    shutil.copyfile(ROOT / "tests/compose/fixture.py", exported / "fixture.py")
    (exported / "fixture.py").chmod(0o644)
    return exported


class Rehearsal:
    def __init__(self, context, image, fixtures, platform):
        self.docker = ["docker", "--context", context]
        self.project = "rss-watch-rehearsal-" + uuid.uuid4().hex[:12]
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
        self.env.update({
            "RSS_WATCH_REHEARSAL_FIXTURES": str(fixtures),
            "RSS_WATCH_REHEARSAL_PYTHON_IMAGE": PYTHON_IMAGE,
            "RSS_WATCH_GO_IMAGE": image,
            "RSS_WATCH_BIND": "127.0.0.1",
            "RSS_WATCH_PORT": "8080",
        })
        self.compose_files = ["-f", "compose.yaml", "-f", "tests/compose/rehearsal.yaml"]
        self.image = image
        self.platform = platform or self.call(
            "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image,
        ).strip()
        self.env["RSS_WATCH_REHEARSAL_PLATFORM"] = self.platform

    def call(self, *args, data=None, check=True, timeout=90):
        process = subprocess.run(
            [*self.docker, *args], cwd=ROOT, env=self.env, input=data,
            text=True, capture_output=True, timeout=timeout,
        )
        if check and process.returncode:
            raise RuntimeError(f"{' '.join(args)} failed ({process.returncode})\n{process.stdout}{process.stderr}")
        return process.stdout

    def compose(self, *args, go=False, **options):
        extra = ["-f", "compose.go.yaml"] if go else []
        return self.call(
            "compose", "--env-file", os.devnull, "-p", self.project, *self.compose_files, *extra, *args, **options,
        )

    def check_project_unused(self):
        label = f"label=com.docker.compose.project={self.project}"
        for command in (("ps", "-aq"), ("volume", "ls", "-q"), ("network", "ls", "-q")):
            assert not self.call(*command, "--filter", label).strip(), f"unexpected pre-existing project {self.project}"

    def up(self, go=False, first=False):
        args = ["up", "-d", "--wait", "--wait-timeout", "55", "--no-build", "--pull", "never"]
        if first:
            self.compose(*args, "mock")
        else:
            self.compose("stop", "worker", "web", go=go)
            args.append("--force-recreate")
        self.compose(*args, "web", "worker", go=go)
        return self.verify_containers(self.image if go else PYTHON_IMAGE)

    def image_id(self, image):
        result = json.loads(self.call("image", "inspect", "--platform", self.platform, image))[0]
        assert f'{result["Os"]}/{result["Architecture"]}' == self.platform, f"wrong image platform for {image}"
        return result["Id"]

    def verify_containers(self, image):
        image_id = self.image_id(image)
        mock_image_id = self.image_id(PYTHON_IMAGE)
        network_name = self.project + "_default"
        assert self.call("network", "inspect", "--format", "{{.Internal}}", network_name).strip() == "true"
        for service in ("web", "worker", "mock"):
            container = self.compose("ps", "-q", service).strip()
            data = json.loads(self.call("inspect", "--type", "container", container))[0]
            assert data["Config"]["Labels"]["com.docker.compose.project"] == self.project
            manifest = data.get("ImageManifestDescriptor")
            if manifest:
                os_arch = manifest["platform"]
                actual_platform = f'{os_arch["os"]}/{os_arch["architecture"]}'
                actual_image_id = manifest["digest"]
            else:
                runtime_image = json.loads(self.call("image", "inspect", data["Image"]))[0]
                actual_platform = f'{runtime_image["Os"]}/{runtime_image["Architecture"]}'
                actual_image_id = runtime_image["Id"]
            expected_image_id = mock_image_id if service == "mock" else image_id
            assert actual_platform == self.platform, f"{service} ran on {actual_platform}, want {self.platform}"
            assert actual_image_id == expected_image_id, f"unexpected {service} image {actual_image_id}"
            assert data["Config"]["User"] == "10001:10001"
            assert data["State"]["Health"]["Status"] == "healthy"
            assert data["HostConfig"]["ReadonlyRootfs"]
            assert not data["HostConfig"]["PortBindings"], "rehearsal must not publish ports"
            if service == "mock":
                continue
            mount = {entry["Destination"]: entry for entry in data["Mounts"]}
            for path, suffix in (("/data", "rss-data"), ("/credentials", "rss-credentials")):
                assert mount[path]["Type"] == "volume"
                assert mount[path]["Name"] == f"{self.project}_{suffix}", f"unexpected volume for {path}"
            assert mount["/credentials"]["RW"] is (service == "web")
        return image_id

    def stage(self, name, data, go=False):
        response = self.compose(
            "exec", "-T", "mock", "python", "/rehearsal/fixture.py", name,
            go=go, data=json.dumps(data),
        )
        return json.loads(response)

    def restore_python(self):
        # Stop the Go pair before running the Python overlay, keeping the two named volumes mounted.
        self.compose("stop", "worker", "web", go=True)
        self.compose("up", "-d", "--wait", "--wait-timeout", "55", "--no-build", "--pull", "never",
                     "--force-recreate", "web", "worker")
        return self.verify_containers(PYTHON_IMAGE)

    def run(self):
        for image in (PYTHON_IMAGE, self.image):
            self.image_id(image)
        self.check_project_unused()
        print(f"Project {self.project} on {self.platform}", flush=True)
        try:
            python_id = self.up(first=True)
            token = self.compose("exec", "-T", "web", "python", "-c",
                                 "from pathlib import Path; print(Path('/data/bootstrap-token').read_text())").strip()
            before = self.stage("python-start", {"bootstrap": token})
            print(f"Python startup passed ({python_id}), one pending, one failed, one sent", flush=True)
            go_id = self.up(go=True)
            after_go = self.stage("go", before, go=True)
            print(f"Go upgrade passed ({go_id}), preserved data and handed off a new pending event", flush=True)
            assert self.restore_python() == python_id
            final = self.stage("python-return", after_go)
            print("Python rollback passed " + json.dumps(final), flush=True)
        except Exception:
            print(self.compose("ps", "-a", check=False), flush=True)
            print(self.compose("logs", "--tail", "25", "web", "worker", "mock", check=False), flush=True)
            raise
        finally:
            self.compose("down", "--volumes", "--remove-orphans", "--timeout", "10")
            self.check_project_unused()
            print(f"Removed temporary project, network, and volumes for {self.project}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", default="default", help="explicit Docker context")
    parser.add_argument("--go-image", required=True, help="locally built immutable-by-convention rehearsal tag")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), help="platform for all services (defaults to the Go image platform)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="rss-watch-rehearsal-") as temporary:
        Rehearsal(args.context, args.go_image, certificates(Path(temporary)), args.platform).run()


if __name__ == "__main__":
    main()
