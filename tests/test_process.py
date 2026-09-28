import json
import os
import subprocess
import sys
import time

from rss_watch.config import Config
from rss_watch.service import Service


def test_real_cli_daemon_lock_sigterm_restart_and_backup(tmp_path):
    path = tmp_path / "config.toml"
    data, creds = tmp_path / "data", tmp_path / "credentials"
    path.write_text(f"data_dir = {json.dumps(str(data))}\ncredentials_dir = {json.dumps(str(creds))}\n")
    command = [sys.executable, "-m", "rss_watch.cli", "--config", str(path)]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    config = Config(data, creds)
    config.prepare()
    svc = Service(config)
    credential = tmp_path / "input.json"
    credential.write_text(json.dumps({
        "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test", "secret": "",
    }))
    credential.chmod(0o600)

    def wait_for_heartbeat():
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if svc.status()["后台运行"]:
                return
            time.sleep(0.05)
        raise AssertionError("daemon did not become ready")

    for iteration in range(2):
        process = subprocess.Popen([*command, "run"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, text=True)
        try:
            wait_for_heartbeat()
            other = subprocess.run([*command, "run"], capture_output=True, text=True,
                                   env=env, timeout=8)
            assert other.returncode == 1 and "已运行" in other.stdout
            added = subprocess.run([*command, "bot", "add", f"bot-{iteration}",
                                    "--credential-file", str(credential)],
                                   capture_output=True, text=True, env=env, timeout=8)
            assert added.returncode == 0
            # No sources or queued messages: this real-process test never makes network calls.
            result = subprocess.run([*command, "backup", str(tmp_path / f"backup-{iteration}")],
                                    capture_output=True, text=True, env=env, timeout=8)
            assert result.returncode == 0, result.stdout
        finally:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
        assert process.returncode == 0, (stdout, stderr)
        assert svc.status()["后台运行"] is False
    assert len(svc.bot_list()) == 2
