"""Opt-in integration test; always uses an isolated Compose project and fresh volumes."""
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RSS_WATCH_DOCKER_TEST") != "1",
                                reason="requires Docker; set RSS_WATCH_DOCKER_TEST=1")
ROOT = Path(__file__).parents[1]


def assert_container_licenses(compose):
    # Query the installed distributions in the final image, including vendored materials.
    result = compose("exec", "-T", "web", "python", "-c", """
import hashlib
import json
from importlib.metadata import distribution, distributions

project = distribution("rss-watch")
metadata = project.metadata
assert tuple(map(int, metadata["Metadata-Version"].split("."))) >= (2, 4)
assert metadata.get_all("License-Expression") == ["MIT"]
assert sorted(metadata.get_all("License-File", [])) == ["LICENSE", "THIRD_PARTY_NOTICES.md"]
project_files = {}
inventory = {}
for dist in distributions():
    name = dist.metadata["Name"]
    materials = {}
    for path in dist.files or ():
        if (".dist-info/licenses/" in str(path)
                or path.name.lower().startswith(("license", "copying", "notice", "authors"))):
            installed = dist.locate_file(path)
            content = installed.read_bytes()
            assert content, f"Empty license material: {installed}"
            materials[str(path)] = str(installed)
            if name == "rss-watch":
                project_files[str(path)] = hashlib.sha256(content).hexdigest()
    assert any(p.rsplit("/", 1)[-1].lower().startswith(("license", "copying"))
               for p in materials), f"No license material for {name}"
    # Modern License-File paths are relative to dist-info/licenses; older wheels use dist-info.
    for declared in dist.metadata.get_all("License-File", []):
        assert any(p.endswith((f".dist-info/licenses/{declared}", f".dist-info/{declared}"))
                   for p in materials), f"Missing declared license for {name}: {declared}"
    inventory[name] = {"version": dist.version, "files": materials}
print(json.dumps({"project_files": project_files, "inventory": inventory}))
""")
    report = json.loads(result.stdout)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        matches = [digest for path, digest in report["project_files"].items()
                   if path.endswith(f".dist-info/licenses/{name}")]
        assert matches == [hashlib.sha256((ROOT / name).read_bytes()).hexdigest()]
    for package, suffixes in {
        "typer": ["typer/_click/LICENSE.txt"],
        "markdown-it-py": ["/licenses/LICENSE", "/licenses/LICENSE.markdown-it"],
        "Pygments": ["/licenses/LICENSE", "/licenses/AUTHORS"],
        "tzdata": ["/licenses/LICENSE", "/licenses/licenses/LICENSE_APACHE"],
    }.items():
        paths = report["inventory"][package]["files"]
        for suffix in suffixes:
            assert any(path.endswith(suffix) for path in paths), (package, suffix, paths)
    print("Final image license inventory: " + json.dumps(report["inventory"], ensure_ascii=False))


def test_compose_health_read_only_credentials_and_persistence():
    project = "rss-watch-test-" + uuid.uuid4().hex[:8]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, RSS_WATCH_PORT=str(port), RSS_WATCH_BIND="127.0.0.1",
               RSS_WATCH_VERSION=project)
    image = os.environ.get("RSS_WATCH_DOCKER_IMAGE")
    # Published-image checks use the production Compose file and never build locally.
    temporary = tempfile.TemporaryDirectory(prefix=project)
    files = ["-f", str(ROOT / "compose.yaml")]
    if image:
        override = Path(temporary.name) / "image.json"
        override.write_text(json.dumps({"services": {
            name: {"image": image} for name in ("web", "worker")
        }}))
        files.extend(["-f", str(override)])
    else:
        files.extend(["-f", str(ROOT / "compose.build.yaml")])

    def compose(*args, check=True):
        result = subprocess.run(["docker", "compose", "-p", project, *files, *args],
                                cwd=ROOT, env=env, check=False, capture_output=True,
                                text=True, timeout=600)
        if check and result.returncode:
            pytest.fail(f"Docker Compose {args} failed:\n{result.stdout}\n{result.stderr}",
                        pytrace=False)
        return result

    def csrf(client, path):
        return re.search(r'name="csrf" value="([a-f0-9]+)"', client.get(path).text)[1]

    try:
        if image:
            compose("pull")
            compose("up", "--no-build", "-d", "--wait", "--wait-timeout", "120")
        else:
            compose("up", "--build", "-d", "--wait", "--wait-timeout", "120")
        assert_container_licenses(compose)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", follow_redirects=True) as client:
            bootstrap = compose("exec", "-T", "web", "cat", "/data/bootstrap-token").stdout.strip()
            response = client.post("/setup", data={"csrf": csrf(client, "/setup"),
                                                  "token": bootstrap, "username": "ci-admin",
                                                  "password": "ci-password-for-test-123"})
            assert response.status_code == 200
            response = client.post("/login", data={"csrf": csrf(client, "/login"),
                                                  "username": "ci-admin",
                                                  "password": "ci-password-for-test-123"})
            assert response.status_code == 200
            headers = {"x-csrf-token": client.get("/api/session/csrf").json()["csrf"]}
            assert client.post("/api/settings", headers=headers, json={
                "fetch_paused": True, "delivery_paused": True, "default_interval": 120,
            }).status_code == 200
            assert client.post("/api/sources", headers=headers, json={
                "name": "持久化测试", "url": "https://example.org/feed"
            }).status_code == 200
            compose("exec", "-T", "worker", "rss-watch-container", "check-worker")
            denied = compose("exec", "-T", "worker", "python", "-c",
                             "from pathlib import Path; Path('/credentials/cannot-write').write_text('x')",
                             check=False)
            assert denied.returncode != 0
            compose("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "120")
            assert client.get("/api/sources").json()[0]["name"] == "持久化测试"
            settings = client.get("/api/settings").json()
            assert settings["default_interval"] == 120 and settings["fetch_paused"]
            compose("exec", "-T", "worker", "rss-watch-container", "check-worker")
    finally:
        try:
            compose("down", "--volumes", "--remove-orphans", check=False)
        finally:
            temporary.cleanup()
