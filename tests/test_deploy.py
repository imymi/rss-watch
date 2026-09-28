import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from deploy import entry_helper
from rss_watch.config import Config, write_credential
from rss_watch.db import Database
from rss_watch.service import Service

INSTALL_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "install.sh"
CHECK_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "check.sh"
BACKUP_HELPER = Path(__file__).resolve().parent.parent / "deploy" / "backup_helper.py"
REAL_UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")


def make_mock_systemctl(
    tmp_path: Path,
    active_sequence: list[int],
    restart_exit_code: int = 0,
    start_exit_code: int = 0,
    stop_exit_code: int = 0,
):
    state_file = tmp_path / "systemctl_state.json"
    state_file.write_text(json.dumps({
        "calls": [],
        "seq": active_sequence,
        "seq_idx": 0,
        "restart_exit": restart_exit_code,
        "start_exit": start_exit_code,
        "stop_exit": stop_exit_code,
    }))
    script = tmp_path / "mock_systemctl.sh"
    script.write_text(f"""#!/usr/bin/env bash
STATE_FILE="{state_file}"
CMD="$1"
shift || true
python3 -c '
import sys, json
cmd = sys.argv[1]
with open("{state_file}", "r") as f:
    data = json.load(f)
data["calls"].append([cmd, sys.argv[2:]])
if cmd == "is-active":
    idx = data["seq_idx"]
    seq = data["seq"]
    ret = seq[idx] if idx < len(seq) else seq[-1]
    data["seq_idx"] = idx + 1
    with open("{state_file}", "w") as f:
        json.dump(data, f)
    sys.exit(ret)
elif cmd == "restart":
    ret = data.get("restart_exit", 0)
    with open("{state_file}", "w") as f:
        json.dump(data, f)
    sys.exit(ret)
elif cmd == "start":
    ret = data.get("start_exit", 0)
    with open("{state_file}", "w") as f:
        json.dump(data, f)
    sys.exit(ret)
elif cmd == "stop":
    ret = data.get("stop_exit", 0)
    with open("{state_file}", "w") as f:
        json.dump(data, f)
    sys.exit(ret)
with open("{state_file}", "w") as f:
    json.dump(data, f)
sys.exit(0)
' "$CMD" "$@"
""")
    script.chmod(0o755)
    return script, state_file


def test_real_uv_install_symlink_and_cli_execution(tmp_path):
    """使用真实 uv 在固定版本目录中构建环境，通过符号链接切换，并实际执行 CLI，验证无 bad interpreter。"""
    target_dir = tmp_path / "opt_rss_watch"
    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config_file = tmp_path / "config.toml"

    mock_systemctl, state_file = make_mock_systemctl(tmp_path, [0, 0])

    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target_dir),
        RSS_WATCH_CONFIG=str(config_file),
        RSS_WATCH_DATA_DIR=str(data_dir),
        RSS_WATCH_CREDS_DIR=str(creds_dir),
        RSS_WATCH_SYSTEMCTL=str(mock_systemctl),
        RSS_WATCH_UV_BIN=REAL_UV,
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )

    # 1. 首次安装
    proc = subprocess.run([str(INSTALL_SCRIPT)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    global_entry = target_dir.parent / "bin/rss-watch"
    assert global_entry.is_file() and os.access(global_entry, os.X_OK)
    assert "rss-watch-managed-entry-v1" in global_entry.read_text()
    entry_help = subprocess.run([global_entry, "--help"], capture_output=True, text=True)
    assert entry_help.returncode == 0 and "中文管理" in entry_help.stdout
    original_entry = global_entry.read_bytes()
    assert (target_dir / "current").is_symlink()
    release_path = (target_dir / "current").resolve()
    assert (release_path / "pyproject.toml").is_file()

    # 实际执行 current 符号链接下的 CLI 入口，验证其 shebang 和解释器路径有效，未出现 bad interpreter (126)
    cli_bin = target_dir / "current" / ".venv" / "bin" / "rss-watch"
    assert cli_bin.is_file()
    cli_res = subprocess.run([str(cli_bin), "--config", str(config_file), "--help"], capture_output=True, text=True)
    assert cli_res.returncode == 0, f"CLI execution failed with code {cli_res.returncode}:\n{cli_res.stderr}"
    assert "RSS 关键词监控与飞书推送" in cli_res.stdout

    # 2. 二次升级，验证幂等性与版本切换平滑有效
    proc2 = subprocess.run([str(INSTALL_SCRIPT)], capture_output=True, text=True, env=env)
    assert proc2.returncode == 0, f"STDOUT:\n{proc2.stdout}\nSTDERR:\n{proc2.stderr}"
    assert global_entry.read_bytes() == original_entry
    new_release_path = (target_dir / "current").resolve()
    assert (new_release_path / "pyproject.toml").is_file()

    # 再次实际执行 CLI
    cli_res2 = subprocess.run([str(cli_bin), "--config", str(config_file), "--help"], capture_output=True, text=True)
    assert cli_res2.returncode == 0, f"CLI execution failed after upgrade with code {cli_res2.returncode}:\n{cli_res2.stderr}"


def test_backup_fails_when_credential_missing_aborts_install(tmp_path):
    """P1 回归测试：数据库引用了机器人凭据但物理文件丢失时，备份必须失败并中止升级。"""
    target_dir = tmp_path / "opt_rss_watch"
    target_dir.mkdir(parents=True)
    marker = target_dir / "preserve_marker.txt"
    marker.write_text("v1-original")

    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config = Config(data_dir, creds_dir)
    config.prepare()

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    # 在数据库中插入机器人，但故意不创建该凭据物理文件
    svc = Service(config)
    missing_ref = "non_existent_credential.json"
    with svc.db.transaction() as db:
        db.execute("INSERT INTO bots(name, credential_ref) VALUES (?, ?)", ("缺失凭据的机器人", missing_ref))

    mock_systemctl, _ = make_mock_systemctl(tmp_path, [0])

    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target_dir),
        RSS_WATCH_CONFIG=str(config_file),
        RSS_WATCH_DATA_DIR=str(data_dir),
        RSS_WATCH_CREDS_DIR=str(creds_dir),
        RSS_WATCH_SYSTEMCTL=str(mock_systemctl),
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )

    proc = subprocess.run([str(INSTALL_SCRIPT)], capture_output=True, text=True, env=env)
    assert proc.returncode != 0
    # 必须排除 SyntaxError，并断言具体缺失原因与机器人信息
    assert "SyntaxError" not in proc.stderr
    assert "SyntaxError" not in proc.stdout
    assert f"数据库引用的凭据物理文件不存在: {missing_ref}" in proc.stderr or "缺少机器人" in proc.stderr
    assert "缺失凭据的机器人" in proc.stderr
    all_output = proc.stdout + proc.stderr
    assert "升级已中止以保护现有数据" in all_output or "备份失败" in all_output
    # 确认旧目录完全保留未被改动
    assert marker.is_file() and marker.read_text() == "v1-original"


def test_restart_non_zero_triggers_rollback(tmp_path):
    """P1 回归测试：systemctl restart 返回非零退出码（如 42）时，必须自动捕获并执行符号链接回滚与旧服务拉起。"""
    target_dir = tmp_path / "opt_rss_watch"
    releases_dir = target_dir / "releases"
    v1_dir = releases_dir / "v1_release"
    v1_dir.mkdir(parents=True)
    (v1_dir / "pyproject.toml").write_text("[project]\nname = 'rss-watch'\nversion = '0.1.0'\n")
    (v1_dir / "release_tag.txt").write_text("v1.0")
    current_link = target_dir / "current"
    current_link.symlink_to(v1_dir)

    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')
    global_entry = tmp_path / "bin/rss-watch"
    entry_helper.install(global_entry, target_dir, config_file, "rss-watch")
    original_entry = global_entry.read_bytes()

    # Mock systemctl:
    # 1. was_active: 0 (服务此前处于运行状态)
    # 2. restart 返回 42（模拟启动命令直接失败）
    # 3. 回滚后 start 返回 0，is-active 返回 0（旧服务成功恢复）
    mock_systemctl, state_file = make_mock_systemctl(
        tmp_path,
        active_sequence=[0, 0],
        restart_exit_code=42,
        start_exit_code=0,
    )

    # 能够通过自检的 mock uv
    mock_uv = tmp_path / "mock_uv.sh"
    current_python = sys.executable
    mock_uv.write_text(f"""#!/usr/bin/env bash
mkdir -p .venv/bin
cat << 'EOF' > .venv/bin/python
#!/usr/bin/env bash
exec "{current_python}" "$@"
EOF
cat << 'EOF' > .venv/bin/rss-watch
#!/usr/bin/env bash
exec "{current_python}" -m rss_watch.cli "$@"
EOF
chmod +x .venv/bin/python .venv/bin/rss-watch
exit 0
""")
    mock_uv.chmod(0o755)

    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target_dir),
        RSS_WATCH_CONFIG=str(config_file),
        RSS_WATCH_DATA_DIR=str(data_dir),
        RSS_WATCH_CREDS_DIR=str(creds_dir),
        RSS_WATCH_SYSTEMCTL=str(mock_systemctl),
        RSS_WATCH_UV_BIN=str(mock_uv),
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )

    proc = subprocess.run([str(INSTALL_SCRIPT)], capture_output=True, text=True, env=env)
    assert proc.returncode != 0
    all_output = proc.stdout + proc.stderr
    assert "新版本服务启动失败！正在自动回滚至旧版本" in all_output
    assert "已成功回滚至旧版本" in all_output
    assert global_entry.read_bytes() == original_entry

    # 符号链接必须已回滚至 v1_dir！
    assert current_link.resolve() == v1_dir
    assert (current_link / "release_tag.txt").read_text() == "v1.0"

    # 确认调用了 start 恢复旧服务
    calls = json.loads(state_file.read_text())["calls"]
    commands = [c[0] for c in calls]
    assert "start" in commands


def test_rollback_failure_reports_fatal_error(tmp_path):
    """P1 回归测试：新服务启动失败回滚后，若拉起旧版本亦失败，必须检测到并输出致命错误，不能谎称恢复成功。"""
    target_dir = tmp_path / "opt_rss_watch"
    releases_dir = target_dir / "releases"
    v1_dir = releases_dir / "v1_release"
    v1_dir.mkdir(parents=True)
    (v1_dir / "pyproject.toml").write_text("[project]\nname = 'rss-watch'\nversion = '0.1.0'\n")
    current_link = target_dir / "current"
    current_link.symlink_to(v1_dir)

    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    # Mock systemctl:
    # 1. was_active: 0
    # 2. restart 返回 1
    # 3. 回滚时 start 失败（返回 1），is-active 也为 1
    mock_systemctl, _ = make_mock_systemctl(
        tmp_path,
        active_sequence=[0, 1],
        restart_exit_code=1,
        start_exit_code=1,
    )

    mock_uv = tmp_path / "mock_uv.sh"
    current_python = sys.executable
    mock_uv.write_text(f"""#!/usr/bin/env bash
mkdir -p .venv/bin
cat << 'EOF' > .venv/bin/python
#!/usr/bin/env bash
exec "{current_python}" "$@"
EOF
cat << 'EOF' > .venv/bin/rss-watch
#!/usr/bin/env bash
exec "{current_python}" -m rss_watch.cli "$@"
EOF
chmod +x .venv/bin/python .venv/bin/rss-watch
exit 0
""")
    mock_uv.chmod(0o755)

    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target_dir),
        RSS_WATCH_CONFIG=str(config_file),
        RSS_WATCH_DATA_DIR=str(data_dir),
        RSS_WATCH_CREDS_DIR=str(creds_dir),
        RSS_WATCH_SYSTEMCTL=str(mock_systemctl),
        RSS_WATCH_UV_BIN=str(mock_uv),
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )

    proc = subprocess.run([str(INSTALL_SCRIPT)], capture_output=True, text=True, env=env)
    assert proc.returncode != 0
    all_output = proc.stdout + proc.stderr
    assert "回滚后拉起旧版本服务亦失败" in all_output
    # 严禁出现虚假的恢复成功提示
    assert "且旧版本服务已恢复正常运行" not in all_output


def test_dependency_failure_preserves_old_version(tmp_path):
    target_dir = tmp_path / "opt_rss_watch"
    releases_dir = target_dir / "releases"
    v1_dir = releases_dir / "v1_release"
    v1_dir.mkdir(parents=True)
    (v1_dir / "pyproject.toml").write_text("[project]\nname = 'rss-watch'\nversion = '0.1.0'\n")
    marker = v1_dir / "old_installed_code.txt"
    marker.write_text("original-content")
    current_link = target_dir / "current"
    current_link.symlink_to(v1_dir)

    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config_file = tmp_path / "config.toml"

    mock_systemctl, _ = make_mock_systemctl(tmp_path, [0, 0])

    failing_uv = tmp_path / "failing_uv.sh"
    failing_uv.write_text("#!/bin/sh\necho 'simulated uv dependency resolution failure' >&2\nexit 1\n")
    failing_uv.chmod(0o755)

    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target_dir),
        RSS_WATCH_CONFIG=str(config_file),
        RSS_WATCH_DATA_DIR=str(data_dir),
        RSS_WATCH_CREDS_DIR=str(creds_dir),
        RSS_WATCH_SYSTEMCTL=str(mock_systemctl),
        RSS_WATCH_UV_BIN=str(failing_uv),
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )

    proc = subprocess.run([str(INSTALL_SCRIPT)], capture_output=True, text=True, env=env)
    assert proc.returncode != 0
    assert "依赖安装失败" in proc.stderr or "环境构建或校验失败" in proc.stderr
    assert marker.is_file()
    assert marker.read_text() == "original-content"
    assert current_link.resolve() == v1_dir
    assert (current_link / "old_installed_code.txt").read_text() == "original-content"


def test_check_fails_on_invalid_credential_webhook(tmp_path):
    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config = Config(data_dir, creds_dir)
    config.prepare()

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    svc = Service(config)
    ref = "bad_bot.json"
    (creds_dir / ref).write_text(json.dumps({"webhook": "not-a-url", "secret": ""}))
    (creds_dir / ref).chmod(0o600)
    with svc.db.transaction() as db:
        db.execute("INSERT INTO bots(name, credential_ref) VALUES (?, ?)", ("坏机器人", ref))
    svc.db.heartbeat(running=True)

    env = dict(os.environ, RSS_WATCH_TEST_MOCK_ENV="1")
    proc = subprocess.run([str(CHECK_SCRIPT), "--config", str(config_file)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode != 0
    all_output = proc.stdout + proc.stderr
    assert "[FAIL]" in all_output
    assert "凭据校验失败" in all_output or "webhook" in all_output.lower()


def test_check_fails_on_bad_permission_credential(tmp_path):
    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config = Config(data_dir, creds_dir)
    config.prepare()

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    svc = Service(config)
    ref = write_credential(config, "https://open.feishu.cn/open-apis/bot/v2/hook/test-token", "")
    # Violate 0600 permission (set to 0644)
    (creds_dir / ref).chmod(0o644)
    with svc.db.transaction() as db:
        db.execute("INSERT INTO bots(name, credential_ref) VALUES (?, ?)", ("权限不合规", ref))
    svc.db.heartbeat(running=True)

    env = dict(os.environ, RSS_WATCH_TEST_MOCK_ENV="1")
    proc = subprocess.run([str(CHECK_SCRIPT), "--config", str(config_file)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode != 0
    all_output = proc.stdout + proc.stderr
    assert "[FAIL]" in all_output
    assert "0600" in all_output


def test_check_fails_on_stale_heartbeat(tmp_path):
    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config = Config(data_dir, creds_dir)
    config.prepare()

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    db = Database(config.database)
    # Heartbeat from 100 seconds ago
    with db.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO runtime VALUES ('heartbeat', ?)", (str(time.time() - 100),))
        conn.execute("INSERT OR REPLACE INTO runtime VALUES ('running', '1')")

    env = dict(os.environ, RSS_WATCH_TEST_MOCK_ENV="1")
    proc = subprocess.run([str(CHECK_SCRIPT), "--config", str(config_file)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode != 0
    all_output = proc.stdout + proc.stderr
    assert "心跳超时" in all_output or "心跳" in all_output


def test_check_succeeds_on_healthy_environment(tmp_path):
    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config = Config(data_dir, creds_dir)
    config.prepare()

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    svc = Service(config)
    ref = write_credential(config, "https://open.feishu.cn/open-apis/bot/v2/hook/test-valid-bot", "my-secret")
    with svc.db.transaction() as db:
        db.execute("INSERT INTO bots(name, credential_ref) VALUES (?, ?)", ("健康机器人", ref))
    svc.db.heartbeat(running=True)

    env = dict(os.environ, RSS_WATCH_TEST_MOCK_ENV="1")
    proc = subprocess.run([str(CHECK_SCRIPT), "--config", str(config_file)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert "[PASS]" in proc.stdout
    assert "自检全部通过" in proc.stdout


def test_legacy_upgrade_dependency_failure_preserves_old_cli(tmp_path):
    """P1 回归测试：旧版单目录安装遇到新版本依赖构建失败时，保留原路径与原虚拟环境，原 CLI 仍可执行。"""
    target_dir = tmp_path / "opt_rss_watch"
    target_dir.mkdir(parents=True)
    (target_dir / "pyproject.toml").write_text("[project]\nname = 'rss-watch'\nversion = '0.1.0'\n")
    legacy_src = target_dir / "src" / "legacy_module.py"
    legacy_src.parent.mkdir(parents=True)
    legacy_src.write_text("# legacy source file\n")

    # 创建旧版单目录下的虚拟环境与 CLI 脚本
    legacy_bin = target_dir / ".venv" / "bin"
    legacy_bin.mkdir(parents=True)
    legacy_cli = legacy_bin / "rss-watch"
    legacy_cli.write_text("#!/usr/bin/env bash\necho 'legacy-cli-v1-ok'\nexit 0\n")
    legacy_cli.chmod(0o755)

    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    mock_systemctl, state_file = make_mock_systemctl(tmp_path, [0])

    failing_uv = tmp_path / "failing_uv.sh"
    failing_uv.write_text("#!/bin/sh\necho 'simulated uv dependency resolution failure' >&2\nexit 1\n")
    failing_uv.chmod(0o755)

    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target_dir),
        RSS_WATCH_CONFIG=str(config_file),
        RSS_WATCH_DATA_DIR=str(data_dir),
        RSS_WATCH_CREDS_DIR=str(creds_dir),
        RSS_WATCH_SYSTEMCTL=str(mock_systemctl),
        RSS_WATCH_UV_BIN=str(failing_uv),
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )

    proc = subprocess.run([str(INSTALL_SCRIPT)], capture_output=True, text=True, env=env)
    assert proc.returncode != 0
    assert "依赖安装失败" in proc.stderr or "环境构建或校验失败" in proc.stderr

    # 验证未提前生成 current 软链接
    assert not (target_dir / "current").exists()
    # 验证旧版源码与 .venv 未被移走
    assert legacy_src.is_file()
    assert legacy_cli.is_file()
    # 验证原 CLI 仍可正常执行！
    cli_proc = subprocess.run([str(legacy_cli)], capture_output=True, text=True)
    assert cli_proc.returncode == 0
    assert "legacy-cli-v1-ok" in cli_proc.stdout

    # 验证旧服务未被意外停止（stop 命令未被调用）
    calls = json.loads(state_file.read_text())["calls"]
    commands = [c[0] for c in calls]
    assert "stop" not in commands


def test_legacy_migration_startup_failure_restores_old_service(tmp_path):
    """P1 回归测试：旧版单目录安装在切换后新服务启动失败时，原单目录环境及 systemd 服务配置完整恢复。"""
    target_dir = tmp_path / "opt_rss_watch"
    target_dir.mkdir(parents=True)
    (target_dir / "pyproject.toml").write_text("[project]\nname = 'rss-watch'\nversion = '0.1.0'\n")
    legacy_src = target_dir / "src" / "legacy_module.py"
    legacy_src.parent.mkdir(parents=True)
    legacy_src.write_text("# legacy source code\n")

    # 旧版 CLI
    legacy_bin = target_dir / ".venv" / "bin"
    legacy_bin.mkdir(parents=True)
    legacy_cli = legacy_bin / "rss-watch"
    legacy_cli.write_text("#!/usr/bin/env bash\necho 'legacy-service-v1-running'\nexit 0\n")
    legacy_cli.chmod(0o755)

    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    # 模拟旧版 systemd service 单元文件（指向单目录原路径）
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    unit_file = systemd_dir / "rss-watch.service"
    unit_file.write_text(f"""[Unit]
Description=RSS Watch Legacy Service
[Service]
WorkingDirectory={target_dir}
ExecStart={legacy_cli} --config {config_file} run
""")

    # Mock systemctl:
    # 1. 升级前 is-active: 返回 0 (旧服务运行中)
    # 2. stop: 返回 0
    # 3. restart (新版本启动): 失败 (返回 1)
    # 4. start (回滚拉起旧版本): 成功 (返回 0)
    # 5. is-active (校验旧版本拉起): 返回 0
    mock_systemctl, state_file = make_mock_systemctl(
        tmp_path,
        active_sequence=[0, 0],
        restart_exit_code=1,
        start_exit_code=0,
    )

    # 模拟新版本 uv 正常构建
    mock_uv = tmp_path / "mock_uv.sh"
    current_python = sys.executable
    mock_uv.write_text(f"""#!/usr/bin/env bash
mkdir -p .venv/bin
cat << 'EOF' > .venv/bin/python
#!/usr/bin/env bash
exec "{current_python}" "$@"
EOF
cat << 'EOF' > .venv/bin/rss-watch
#!/usr/bin/env bash
exec "{current_python}" -m rss_watch.cli "$@"
EOF
chmod +x .venv/bin/python .venv/bin/rss-watch
exit 0
""")
    mock_uv.chmod(0o755)

    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target_dir),
        RSS_WATCH_CONFIG=str(config_file),
        RSS_WATCH_DATA_DIR=str(data_dir),
        RSS_WATCH_CREDS_DIR=str(creds_dir),
        RSS_WATCH_SYSTEMCTL=str(mock_systemctl),
        RSS_WATCH_SYSTEMD_UNIT=str(unit_file),
        RSS_WATCH_UV_BIN=str(mock_uv),
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )

    proc = subprocess.run([str(INSTALL_SCRIPT)], capture_output=True, text=True, env=env)
    assert proc.returncode != 0
    all_output = proc.stdout + proc.stderr
    assert "检测到旧版单目录迁移失败，正在回滚恢复旧版单目录环境与服务配置" in all_output
    assert "已成功回滚至旧版本单目录环境，且旧服务已恢复正常运行！" in all_output

    # 验证 current 软链接已被清理（单目录不应存在 current 软链接）
    assert not (target_dir / "current").exists()
    # 验证旧版本目录已被移回原位
    assert legacy_src.is_file()
    assert legacy_cli.is_file()
    cli_proc = subprocess.run([str(legacy_cli)], capture_output=True, text=True)
    assert cli_proc.returncode == 0
    assert "legacy-service-v1-running" in cli_proc.stdout

    # 验证 systemd 单元文件内容恢复为旧版单目录配置
    assert f"WorkingDirectory={target_dir}" in unit_file.read_text()
    assert f"ExecStart={legacy_cli}" in unit_file.read_text()

    # 验证调用了 start 恢复旧服务
    calls = json.loads(state_file.read_text())["calls"]
    commands = [c[0] for c in calls]
    assert "start" in commands


def test_fallback_backup_succeeds_without_old_cli_and_is_restorable(tmp_path):
    """P1 回归测试：完整数据库及凭据在没有旧 CLI 时，兜底备份必须成功且可完整恢复。"""
    target_dir = tmp_path / "opt_rss_watch"
    target_dir.mkdir(parents=True)
    # 明确不提供任何旧 CLI

    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config = Config(data_dir, creds_dir)
    config.prepare()

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    # 配置合法数据库与合规凭据
    svc = Service(config)
    ref = write_credential(config, "https://open.feishu.cn/open-apis/bot/v2/hook/fallback-test-bot", "secret-key-123")
    with svc.db.transaction() as db:
        db.execute("INSERT INTO bots(name, credential_ref) VALUES (?, ?)", ("兜底测试机器人", ref))

    mock_systemctl, _ = make_mock_systemctl(tmp_path, [0])

    # 模拟 uv 正常构建
    mock_uv = tmp_path / "mock_uv.sh"
    current_python = sys.executable
    mock_uv.write_text(f"""#!/usr/bin/env bash
mkdir -p .venv/bin
cat << 'EOF' > .venv/bin/python
#!/usr/bin/env bash
exec "{current_python}" "$@"
EOF
cat << 'EOF' > .venv/bin/rss-watch
#!/usr/bin/env bash
exec "{current_python}" -m rss_watch.cli "$@"
EOF
chmod +x .venv/bin/python .venv/bin/rss-watch
exit 0
""")
    mock_uv.chmod(0o755)

    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target_dir),
        RSS_WATCH_CONFIG=str(config_file),
        RSS_WATCH_DATA_DIR=str(data_dir),
        RSS_WATCH_CREDS_DIR=str(creds_dir),
        RSS_WATCH_SYSTEMCTL=str(mock_systemctl),
        RSS_WATCH_UV_BIN=str(mock_uv),
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )

    proc = subprocess.run([str(INSTALL_SCRIPT)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert "未找到可执行的旧版本 CLI，使用 Python 独立模块执行兜底在线备份" in proc.stdout
    assert "备份成功并已校验" in proc.stdout
    assert "SyntaxError" not in proc.stderr

    # 检查备份产物
    backups = list((data_dir / "backups").glob("upgrade-*"))
    assert len(backups) == 1
    backup_dir = backups[0]

    backup_db = backup_dir / "rss-watch.sqlite3"
    backup_cred = backup_dir / "credentials" / ref
    manifest = backup_dir / "manifest.json"

    assert backup_db.is_file()
    assert (backup_db.stat().st_mode & 0o777) == 0o600
    assert backup_cred.is_file()
    assert (backup_cred.stat().st_mode & 0o777) == 0o600
    assert json.loads(manifest.read_text())["schema_version"] == 1

    # 验证备份完全可恢复且内容一致
    restore_dir = tmp_path / "restored_data"
    restore_dir.mkdir()
    restored_db_file = restore_dir / "rss-watch.sqlite3"
    shutil.copyfile(backup_db, restored_db_file)

    conn = sqlite3.connect(restored_db_file)
    conn.row_factory = sqlite3.Row
    bots = conn.execute("SELECT name, credential_ref FROM bots").fetchall()
    conn.close()
    assert len(bots) == 1
    assert bots[0]["name"] == "兜底测试机器人"
    assert bots[0]["credential_ref"] == ref

    cred_data = json.loads(backup_cred.read_text())
    assert cred_data["webhook"] == "https://open.feishu.cn/open-apis/bot/v2/hook/fallback-test-bot"
    assert cred_data["secret"] == "secret-key-123"


def test_backup_helper_direct_execution(tmp_path):
    """单元测试：验证 deploy/backup_helper.py 各子命令独立运行的完整性与安全性。"""
    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config = Config(data_dir, creds_dir)
    config.prepare()

    svc = Service(config)
    ref = write_credential(config, "https://open.feishu.cn/open-apis/bot/v2/hook/helper-test", "secret-xyz")
    with svc.db.transaction() as db:
        db.execute("INSERT INTO bots(name, credential_ref) VALUES (?, ?)", ("Helper测试机器人", ref))

    dest = tmp_path / "direct_backup"
    # 执行 backup
    proc_b = subprocess.run(
        [sys.executable, str(BACKUP_HELPER), "backup", str(data_dir), str(creds_dir), str(dest)],
        capture_output=True, text=True
    )
    assert proc_b.returncode == 0
    assert "备份成功完成" in proc_b.stdout

    # 执行 verify
    proc_v = subprocess.run(
        [sys.executable, str(BACKUP_HELPER), "verify", str(dest)],
        capture_output=True, text=True
    )
    assert proc_v.returncode == 0
    assert "备份校验全部通过" in proc_v.stdout

    # 测试 migrate-legacy 与 restore-legacy
    sim_target = tmp_path / "sim_target"
    sim_target.mkdir()
    (sim_target / "file1.txt").write_text("content1")
    (sim_target / ".hidden_file").write_text("hidden")
    (sim_target / ".venv").mkdir()
    (sim_target / ".venv" / "item.txt").write_text("in_venv")
    (sim_target / "releases").mkdir()

    legacy_archive = sim_target / "releases" / "legacy_1"
    proc_m = subprocess.run(
        [sys.executable, str(BACKUP_HELPER), "migrate-legacy", str(sim_target), str(legacy_archive)],
        capture_output=True, text=True
    )
    assert proc_m.returncode == 0
    assert not (sim_target / "file1.txt").exists()
    assert not (sim_target / ".hidden_file").exists()
    assert not (sim_target / ".venv").exists()
    assert (legacy_archive / "file1.txt").is_file()
    assert (legacy_archive / ".hidden_file").is_file()
    assert (legacy_archive / ".venv" / "item.txt").is_file()

    # restore
    proc_r = subprocess.run(
        [sys.executable, str(BACKUP_HELPER), "restore-legacy", str(legacy_archive), str(sim_target)],
        capture_output=True, text=True
    )
    assert proc_r.returncode == 0
    assert (sim_target / "file1.txt").is_file()
    assert (sim_target / ".hidden_file").is_file()
    assert (sim_target / ".venv" / "item.txt").is_file()


def test_legacy_upgrade_executed_from_within_target_dir_succeeds(tmp_path):
    """P1 回归测试：直接在旧安装目录内部运行 deploy/install.sh 执行升级，验证成功升级且不复制嵌套 releases。"""
    project_root = INSTALL_SCRIPT.parent.parent
    target_dir = tmp_path / "opt_rss_watch"
    target_dir.mkdir(parents=True)

    # 完整拷贝项目源码到 target_dir，模拟生产环境直接在旧目录拉取源码并就地升级的场景
    shutil.copyfile(project_root / "pyproject.toml", target_dir / "pyproject.toml")
    shutil.copytree(project_root / "src", target_dir / "src")
    shutil.copytree(project_root / "deploy", target_dir / "deploy")

    # 标记文件
    (target_dir / "legacy_marker.txt").write_text("v1-original")

    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    # 旧版单目录虚拟环境
    legacy_bin = target_dir / ".venv" / "bin"
    legacy_bin.mkdir(parents=True)
    legacy_cli = legacy_bin / "rss-watch"
    legacy_cli.write_text(f"""#!/usr/bin/env bash
if [[ "$1" == "--config" ]] && [[ "$3" == "backup" ]]; then
    exec "{sys.executable}" "{BACKUP_HELPER}" backup "{data_dir}" "{creds_dir}" "$4"
fi
echo 'old-cli-v1'
exit 0
""")
    legacy_cli.chmod(0o755)

    # 配置合法数据库与凭据
    config = Config(data_dir, creds_dir)
    config.prepare()
    svc = Service(config)
    ref = write_credential(config, "https://open.feishu.cn/open-apis/bot/v2/hook/in-place-test", "secret-key")
    with svc.db.transaction() as db:
        db.execute("INSERT INTO bots(name, credential_ref) VALUES (?, ?)", ("就地升级机器人", ref))

    mock_systemctl, state_file = make_mock_systemctl(tmp_path, [0, 0])

    mock_uv = tmp_path / "mock_uv.sh"
    current_python = sys.executable
    mock_uv.write_text(f"""#!/usr/bin/env bash
mkdir -p .venv/bin
cat << 'EOF' > .venv/bin/python
#!/usr/bin/env bash
exec "{current_python}" "$@"
EOF
cat << 'EOF' > .venv/bin/rss-watch
#!/usr/bin/env bash
exec "{current_python}" -m rss_watch.cli "$@"
EOF
chmod +x .venv/bin/python .venv/bin/rss-watch
exit 0
""")
    mock_uv.chmod(0o755)

    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target_dir),
        RSS_WATCH_CONFIG=str(config_file),
        RSS_WATCH_DATA_DIR=str(data_dir),
        RSS_WATCH_CREDS_DIR=str(creds_dir),
        RSS_WATCH_SYSTEMCTL=str(mock_systemctl),
        RSS_WATCH_UV_BIN=str(mock_uv),
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )

    # 关键：从目标目录内部执行 ./deploy/install.sh，cwd=target_dir
    script_in_target = target_dir / "deploy" / "install.sh"
    proc = subprocess.run([str(script_in_target)], cwd=str(target_dir), capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    all_output = proc.stdout + proc.stderr
    assert "rss-watch 部署/升级成功并已正常启动！" in all_output

    # 符号链接与版本目录结构校验
    current_link = target_dir / "current"
    assert current_link.is_symlink()
    assert (current_link / ".venv" / "bin" / "rss-watch").is_file()

    # 核心验证：严禁出现把 releases 嵌套复制进自身的问题！
    assert not (current_link / "releases").exists()

    # 验证新版 CLI 能正常执行
    cli_proc = subprocess.run([str(current_link / ".venv" / "bin" / "rss-watch"), "--help"],
                              capture_output=True, text=True)
    assert cli_proc.returncode == 0


def test_legacy_upgrade_executed_from_within_target_dir_startup_failure_rolls_back(tmp_path):
    """P1 回归测试：直接在旧安装目录内部运行 deploy/install.sh，启动失败后必须成功回滚，杜绝 helper 丢失退出码 2。"""
    project_root = INSTALL_SCRIPT.parent.parent
    target_dir = tmp_path / "opt_rss_watch"
    target_dir.mkdir(parents=True)

    # 完整拷贝项目源码到 target_dir
    shutil.copyfile(project_root / "pyproject.toml", target_dir / "pyproject.toml")
    shutil.copytree(project_root / "src", target_dir / "src")
    shutil.copytree(project_root / "deploy", target_dir / "deploy")

    # 标记文件
    marker = target_dir / "legacy_marker.txt"
    marker.write_text("legacy-preserved-token-42")

    # 旧版单目录 CLI
    legacy_bin = target_dir / ".venv" / "bin"
    legacy_bin.mkdir(parents=True)
    legacy_cli = legacy_bin / "rss-watch"
    legacy_cli.write_text("#!/usr/bin/env bash\necho 'legacy-cli-restored-successfully'\nexit 0\n")
    legacy_cli.chmod(0o755)

    data_dir = tmp_path / "data"
    creds_dir = tmp_path / "creds"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'data_dir = "{data_dir}"\ncredentials_dir = "{creds_dir}"\n')

    # 旧版 systemd 单元文件
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    unit_file = systemd_dir / "rss-watch.service"
    unit_file.write_text(f"""[Unit]
Description=RSS Watch Legacy Service
[Service]
WorkingDirectory={target_dir}
ExecStart={legacy_cli} --config {config_file} run
""")

    # Mock systemctl:
    # 1. 升级前 is-active: 返回 0 (旧服务活跃)
    # 2. stop: 返回 0
    # 3. restart: 返回 1 (模拟新版本启动失败)
    # 4. start: 返回 0 (回滚拉起旧版本)
    # 5. is-active: 返回 0 (确认旧服务恢复)
    mock_systemctl, state_file = make_mock_systemctl(
        tmp_path,
        active_sequence=[0, 0],
        restart_exit_code=1,
        start_exit_code=0,
    )

    mock_uv = tmp_path / "mock_uv.sh"
    current_python = sys.executable
    mock_uv.write_text(f"""#!/usr/bin/env bash
mkdir -p .venv/bin
cat << 'EOF' > .venv/bin/python
#!/usr/bin/env bash
exec "{current_python}" "$@"
EOF
cat << 'EOF' > .venv/bin/rss-watch
#!/usr/bin/env bash
exec "{current_python}" -m rss_watch.cli "$@"
EOF
chmod +x .venv/bin/python .venv/bin/rss-watch
exit 0
""")
    mock_uv.chmod(0o755)

    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target_dir),
        RSS_WATCH_CONFIG=str(config_file),
        RSS_WATCH_DATA_DIR=str(data_dir),
        RSS_WATCH_CREDS_DIR=str(creds_dir),
        RSS_WATCH_SYSTEMCTL=str(mock_systemctl),
        RSS_WATCH_SYSTEMD_UNIT=str(unit_file),
        RSS_WATCH_UV_BIN=str(mock_uv),
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )

    # 关键：从 target_dir 内部执行 install.sh
    script_in_target = target_dir / "deploy" / "install.sh"
    proc = subprocess.run([str(script_in_target)], cwd=str(target_dir), capture_output=True, text=True, env=env)

    # 严格验证：不能因 Python 找不到 helper 报退出码 2！
    assert proc.returncode != 0
    assert proc.returncode != 2, f"install.sh 因 helper 不存在异常退出 2: {proc.stderr}"
    assert "FileNotFoundError" not in proc.stderr
    all_output = proc.stdout + proc.stderr
    assert "检测到旧版单目录迁移失败，正在回滚恢复旧版单目录环境与服务配置" in all_output
    assert "已成功回滚至旧版本单目录环境，且旧服务已恢复正常运行！" in all_output

    # 验证 current 软链接已被清除
    assert not (target_dir / "current").exists()

    # 验证旧版本单目录已被完整移回原处
    assert marker.is_file() and marker.read_text() == "legacy-preserved-token-42"
    assert legacy_cli.is_file()
    cli_proc = subprocess.run([str(legacy_cli)], capture_output=True, text=True)
    assert cli_proc.returncode == 0
    assert "legacy-cli-restored-successfully" in cli_proc.stdout

    # 验证旧版 systemd 单元文件被恢复
    assert f"WorkingDirectory={target_dir}" in unit_file.read_text()
    assert f"ExecStart={legacy_cli}" in unit_file.read_text()

    # 验证 systemctl start 被调用以恢复原服务
    calls = json.loads(state_file.read_text())["calls"]
    commands = [c[0] for c in calls]
    assert "start" in commands


# ==============================================================================
# 卸载专项测试迁移至 tests/test_uninstall.py，使用完整配置与有状态 systemd 替身。
