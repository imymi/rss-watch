import json
import os
import pwd
import subprocess
import sys

import pytest

from deploy import entry_helper


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def command_entry(tmp_path):
    target = tmp_path / "project with spaces"
    config = tmp_path / "config with 'quotes'.toml"
    path = tmp_path / "bin/rss-watch"
    stub = f"#!{sys.executable}\nimport json,sys; print(json.dumps(sys.argv[1:]))\n"
    executable(target / "current/.venv/bin/rss-watch", stub)
    executable(target / "current/deploy/check.sh", stub)
    for name in ("systemctl", "journalctl"):
        executable(tmp_path / "bin" / name, stub)
    executable(tmp_path / "bin/sudo", '#!/bin/sh\nif [ "$1" = "-u" ]; then shift 2; fi\n'
               'if [ "$1" = "--" ]; then shift; fi\nexec "$@"\n')
    entry_helper.install(path, target, config, pwd.getpwuid(os.geteuid()).pw_name)
    return path, target, config


@pytest.mark.parametrize("args,suffix", [
    ([], ["menu"]), (["source", "list"], ["source", "list"]),
    (["status"], ["status"]), (["cli-help"], ["--help"]),
    (["rule", "test", "1", "--title", "$(touch should-not-exist)"],
     ["rule", "test", "1", "--title", "$(touch should-not-exist)"]),
])
def test_menu_and_cli_forwarding(command_entry, args, suffix):
    path, _, config = command_entry
    p = subprocess.run([path, *args], capture_output=True, text=True, check=True)
    assert json.loads(p.stdout) == ["--config", str(config), *suffix]


@pytest.mark.parametrize("args,expected", [
    (["check"], None), (["service-status"], ["status", "rss-watch.service", "--no-pager"]),
    (["logs"], ["-u", "rss-watch.service", "-n", "50", "-f"]),
    (["restart"], ["restart", "rss-watch.service"]),
    (["start"], ["start", "rss-watch.service"]),
    (["stop"], ["stop", "rss-watch.service"]),
])
def test_operations_route_to_expected_tools(command_entry, args, expected):
    path, _, config = command_entry
    env = dict(os.environ, PATH=str(path.parent) + os.pathsep + os.environ["PATH"])
    p = subprocess.run([path, *args], capture_output=True, text=True, check=True, env=env)
    assert json.loads(p.stdout) == (expected or ["--config", str(config)])


def test_upgrade_and_foreign_command_protection(command_entry):
    path, target, config = command_entry
    assert entry_helper.owned(path, target, config)
    before = path.read_bytes()
    entry_helper.install(path, target, config, pwd.getpwuid(os.geteuid()).pw_name)
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        entry_helper.install(path, target / "other", config, "rss-watch")
    path.write_text("#!/bin/sh\necho unrelated\n")
    with pytest.raises(ValueError):
        entry_helper.install(path, target, config, "rss-watch")
    assert "unrelated" in path.read_text()


def test_symlink_and_modified_entry_are_not_claimed(command_entry):
    path, target, config = command_entry
    path.write_text(path.read_text() + "# local edit\n")
    assert not entry_helper.owned(path, target, config)
    path.unlink()
    path.symlink_to(config)
    with pytest.raises(ValueError):
        entry_helper.install(path, target, config, "rss-watch")


def test_help_and_reject_extra_service_targets(command_entry):
    path, _, _ = command_entry
    p = subprocess.run([path, "--help"], capture_output=True, text=True, check=True)
    assert "中文管理" in p.stdout
    p = subprocess.run([path, "stop", "ssh.service"], capture_output=True, text=True)
    assert p.returncode == 2
