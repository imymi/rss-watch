"""Opt-in integration test; always uses an isolated Compose project and fresh volumes."""
import os
import re
import socket
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RSS_WATCH_DOCKER_TEST") != "1",
                                reason="requires Docker; set RSS_WATCH_DOCKER_TEST=1")
ROOT = Path(__file__).parents[1]


def test_compose_health_read_only_credentials_and_persistence():
    project = "rss-watch-test-" + uuid.uuid4().hex[:8]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, RSS_WATCH_PORT=str(port), RSS_WATCH_BIND="127.0.0.1",
               RSS_WATCH_VERSION=project)

    def compose(*args, check=True):
        return subprocess.run(["docker", "compose", "-p", project, *args],
                              cwd=ROOT, env=env, check=check, capture_output=True,
                              text=True, timeout=600)

    def csrf(client, path):
        return re.search(r'name="csrf" value="([a-f0-9]+)"', client.get(path).text)[1]

    try:
        compose("up", "--build", "-d", "--wait", "--wait-timeout", "120")
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
        compose("down", "--volumes", "--remove-orphans", check=False)
