import json
import os
import pty
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import entry_helper
from deploy import install_receipt as receipt
from deploy import uninstall_helper as uninstall
from rss_watch.config import Config
from rss_watch.service import Service

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/uninstall.sh"


@pytest.fixture
def installation(tmp_path):
    root = tmp_path.resolve()
    target, data, creds = root / "app", root / "custom-data", root / "custom-creds"
    (target / "src/rss_watch").mkdir(parents=True)
    (target / "pyproject.toml").write_text('[project]\nname="rss-watch"\nversion="0.1.0"\n')
    shutil.copytree(
        ROOT / "deploy", target / "deploy", ignore=shutil.ignore_patterns("__pycache__")
    )
    config = root / "configuration/config.toml"
    config.parent.mkdir()
    config.write_text(
        f"data_dir={json.dumps(str(data))}\ncredentials_dir={json.dumps(str(creds))}\n"
    )
    cfg = Config(data, creds)
    cfg.prepare()
    svc = Service(cfg)
    svc.bot_save("test", "https://open.feishu.cn/open-apis/bot/v2/hook/test", "NOT-REAL-SECRET")
    svc.backup(data / "backups/upgrade-test")
    unit = root / "systemd/rss-watch.service"
    unit.parent.mkdir()
    unit.write_text(
        f"[Service]\nUser=rss-watch\nWorkingDirectory={target}\n"
        f"ExecStart={shlex.quote(str(target / '.venv/bin/rss-watch'))} "
        f"--config {shlex.quote(str(config))} run\n"
    )
    state = root / "systemctl.json"
    state.write_text(json.dumps({"calls": [], "active": True}))
    ctl = root / "systemctl"
    ctl.write_text(f"""#!{sys.executable}
import json,sys
from pathlib import Path
path=Path({str(state)!r})
s=json.loads(path.read_text()); cmd=sys.argv[1]; s['calls'].append(cmd)
rc=0
if cmd=='show':
    rc=s.get('query_failure',0)
    print('LoadState='+('loaded' if Path({str(unit)!r}).exists() else 'not-found'))
    print('ActiveState='+('active' if s['active'] else 'inactive'))
    print('FragmentPath='+{str(unit)!r})
elif cmd=='stop':
    rc=s.get('stop_failure',0)
    if not rc and not s.get('stubborn'): s['active']=False
elif cmd=='disable': rc=s.get('disable_failure',0)
elif cmd=='daemon-reload': rc=s.get('reload_failure',0)
else: rc=93
path.write_text(json.dumps(s)); sys.exit(rc)
""")
    ctl.chmod(0o755)
    env = dict(
        os.environ,
        RSS_WATCH_TEST_MOCK_ENV="1",
        RSS_WATCH_TARGET_DIR=str(target),
        RSS_WATCH_CONFIG=str(config),
        RSS_WATCH_SYSTEMD_UNIT=str(unit),
        RSS_WATCH_SYSTEMCTL=str(ctl),
        RSS_WATCH_PYTHON_BIN=sys.executable,
    )
    return SimpleNamespace(
        root=root,
        target=target,
        data=data,
        creds=creds,
        config=config,
        unit=unit,
        state=state,
        env=env,
        svc=svc,
    )


def run(i, *args, script=SCRIPT, **kwargs):
    return subprocess.run(
        [str(script), *args], env=i.env, capture_output=True, text=True, timeout=15, **kwargs
    )


def calls(i):
    return json.loads(i.state.read_text())["calls"]


def state_change(i, **changes):
    value = json.loads(i.state.read_text())
    value.update(changes)
    i.state.write_text(json.dumps(value))


def tree(root):
    return {
        str(p.relative_to(root)): (
            p.lstat().st_mode,
            p.lstat().st_mtime_ns,
            p.read_bytes() if p.is_file() and not p.is_symlink() else None,
        )
        for p in root.rglob("*")
    }


def test_default_preserves_data_backup_credentials_and_account(installation):
    i = installation
    before = tree(i.data), tree(i.creds), i.config.read_bytes()
    p = run(i)
    assert p.returncode == 0, p.stderr
    assert not i.target.exists() and not i.unit.exists()
    assert before == (tree(i.data), tree(i.creds), i.config.read_bytes())
    assert "保留服务账户" in p.stdout
    assert calls(i) == ["show", "stop", "show", "disable", "daemon-reload"]


def test_purge_uses_real_configuration_and_removes_backups(installation):
    i = installation
    unrelated = i.root / "other-data"
    unrelated.mkdir()
    (unrelated / "important").write_text("keep")
    i.env["RSS_WATCH_DATA_DIR"] = str(unrelated)  # Must never override actual configuration.
    i.env["RSS_WATCH_CREDS_DIR"] = str(unrelated)
    p = run(i, "--purge", "--yes")
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert all(not p.exists() for p in (i.target, i.config, i.data, i.creds, i.unit))
    assert (unrelated / "important").read_text() == "keep"
    assert str(i.data) in p.stdout and str(i.creds) in p.stdout
    assert "保留服务账户" in p.stdout


@pytest.mark.parametrize("purge", [False, True])
def test_dry_run_does_not_change_files_or_services(installation, purge):
    i = installation
    before = tree(i.root)
    p = run(i, "--dry-run", *(["--purge"] if purge else []))
    assert p.returncode == 0, p.stderr
    assert "预览" in p.stdout and calls(i) == []
    assert before == tree(i.root)


@pytest.mark.parametrize("answer,success", [("no\n", False), ("rss-watch\n", True)])
def test_real_terminal_confirmation(installation, answer, success):
    i = installation
    master, slave = pty.openpty()
    try:
        p = subprocess.Popen(
            [str(SCRIPT), "--purge"],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=i.env,
        )
        os.write(master, answer.encode())
        out, err = p.communicate(timeout=15)
    finally:
        os.close(master)
        os.close(slave)
    assert (p.returncode == 0) is success, (out, err)
    if not success:
        assert i.target.exists() and i.config.exists() and not calls(i)
    else:
        assert not i.target.exists() and not i.data.exists()


def test_noninteractive_purge_needs_explicit_yes(installation):
    i = installation
    p = run(i, "--purge", input="rss-watch\n")
    assert p.returncode != 0 and "--purge --yes" in p.stderr
    assert not calls(i) and i.target.exists()


def test_help_and_missing_argument(installation):
    i = installation
    for args in (("--help",), ("--config",), ("--yes",)):
        p = run(i, *args)
        assert p.returncode == (0 if args == ("--help",) else 2)
    assert calls(i) == []


def test_repeat_default_then_purge_then_purge_again(installation):
    i = installation
    for args in ((), (), ("--purge", "--yes"), ("--purge", "--yes")):
        p = run(i, *args)
        assert p.returncode == 0, (args, p.stdout, p.stderr)


@pytest.mark.parametrize(
    "change",
    [{"stop_failure": 1}, {"stubborn": True}, {"query_failure": 1}, {"disable_failure": 1}],
)
def test_service_failures_prevent_deletion(installation, change):
    i = installation
    state_change(i, **change)
    p = run(i, "--purge", "--yes")
    assert p.returncode != 0
    assert all(p.exists() for p in (i.target, i.config, i.unit, i.data, i.creds))


def test_reload_failure_is_not_reported_as_success(installation):
    i = installation
    state_change(i, reload_failure=1)
    p = run(i)
    assert p.returncode != 0 and "daemon-reload" in p.stderr
    assert i.target.exists() and i.data.exists()


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/etc/",
        "/etc/.",
        "/usr/local",
        "/var/lib",
        "/tmp/../etc",
        "/etc/passwd",
        "relative/path",
    ],
)
def test_dangerous_paths_rejected_before_service_calls(installation, path):
    i = installation
    p = run(i, "--target", path, "--dry-run")
    assert p.returncode != 0 and not calls(i)
    assert i.target.exists()


def test_parent_symlink_and_external_target_symlink_rejected(installation):
    i = installation
    outside = i.root / "external"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    link = i.root / "link"
    link.symlink_to(outside)
    for path in (link, link / "child"):
        p = run(i, "--target", str(path))
        assert p.returncode != 0 and "符号链接" in p.stderr
    assert not calls(i) and (outside / "keep").exists()


def test_nested_symlink_is_unlinked_without_touching_target(installation):
    i = installation
    outside = i.root / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (i.target / "external-link").symlink_to(outside)
    p = run(i)
    assert p.returncode == 0, p.stderr
    assert (outside / "keep").read_text() == "keep"


def test_invalid_configuration_never_falls_back(installation):
    i = installation
    i.config.write_text("data_dir = [broken")
    p = run(i, "--purge", "--yes")
    assert p.returncode != 0 and "不会回退" in p.stderr
    assert not calls(i) and i.data.exists()


def test_shared_data_is_preserved_with_nonzero_result(installation):
    i = installation
    (i.data / "other-project.txt").write_text("keep")
    p = run(i, "--purge", "--yes")
    assert p.returncode != 0 and "无法确认目录专属归属" in p.stdout
    assert (i.data / "other-project.txt").exists() and i.config.exists() and i.creds.exists()
    assert not i.target.exists()


def test_unrelated_target_and_unit_are_not_removed(installation):
    i = installation
    (i.target / "pyproject.toml").write_text('[project]\nname="unrelated"\n')
    p = run(i)
    assert p.returncode != 0 and not calls(i)
    (i.target / "pyproject.toml").write_text('[project]\nname="rss-watch"\n')
    i.unit.write_text("[Service]\nExecStart=/usr/bin/unrelated\nUser=root\n")
    p = run(i)
    assert p.returncode != 0 and not calls(i)
    assert i.target.exists() and i.unit.exists()


def test_from_inside_installed_directory(installation):
    i = installation
    p = run(i, script=i.target / "deploy/uninstall.sh", cwd=i.target)
    assert p.returncode == 0, p.stderr
    assert not i.target.exists() and i.data.exists()


def test_current_layout_is_supported(installation):
    i = installation
    release = i.target / "releases/v1"
    release.mkdir(parents=True)
    for item in list(i.target.iterdir()):
        if item.name != "releases":
            shutil.move(str(item), release / item.name)
    (i.target / "current").symlink_to(release)
    i.unit.write_text(
        i.unit.read_text().replace(str(i.target / ".venv"), str(i.target / "current/.venv"))
    )
    p = run(i, script=i.target / "current/deploy/uninstall.sh", cwd=release)
    assert p.returncode == 0, p.stderr
    assert not i.target.exists()


def test_default_protects_data_nested_inside_installation(installation):
    i = installation
    nested = i.target / "private-data"
    shutil.move(str(i.data), nested)
    i.config.write_text(
        f"data_dir={json.dumps(str(nested))}\ncredentials_dir={json.dumps(str(i.creds))}\n"
    )
    before = tree(nested)
    p = run(i)
    assert p.returncode == 0, p.stderr
    assert tree(nested) == before and i.config.exists()
    assert not (i.target / "src").exists()
    again = run(i)
    assert again.returncode == 0, again.stderr
    assert tree(nested) == before
    purge = run(i, "--purge", "--yes")
    assert purge.returncode == 0, purge.stderr
    assert not i.target.exists()


def fake_accounts(monkeypatch):
    user = SimpleNamespace(
        pw_name="rss-watch",
        pw_uid=987,
        pw_gid=986,
        pw_dir="/var/lib/rss-watch",
        pw_shell="/usr/sbin/nologin",
    )
    group = SimpleNamespace(gr_name="rss-watch", gr_gid=986, gr_mem=[])
    monkeypatch.setattr(uninstall.pwd, "getpwnam", lambda _: user)
    monkeypatch.setattr(uninstall.pwd, "getpwall", lambda: [user])
    monkeypatch.setattr(uninstall.grp, "getgrnam", lambda _: group)
    return user, group


def test_only_installer_created_accounts_can_be_removed(installation, monkeypatch):
    i = installation
    user, group = fake_accounts(monkeypatch)
    plan = uninstall.Plan(
        i.target, i.config, i.unit, i.config.parent / "install-receipt.json", i.data, i.creds, True
    )
    assert uninstall.removable_accounts(plan) == []
    receipt.record(i.target, i.config, i.unit, True, True)
    plan.receipt = uninstall.read_receipt(plan.receipt_path, i.config, i.target, True)
    assert uninstall.removable_accounts(plan) == ["userdel", "groupdel"]
    group.gr_mem = ["another-user"]
    assert uninstall.removable_accounts(plan) == ["userdel"]
    user.pw_uid = 999
    assert uninstall.removable_accounts(plan) == []


def test_receipt_does_not_claim_preexisting_accounts(installation, monkeypatch):
    i = installation
    fake_accounts(monkeypatch)
    receipt.record(i.target, i.config, i.unit, False, False)
    path = i.config.parent / "install-receipt.json"
    value = json.loads(path.read_text())
    assert not value["user"]["created"] and not value["group"]["created"]
    receipt.record(i.target, i.config, i.unit, True, True)
    receipt.record(i.target, i.config, i.unit, False, False)
    assert json.loads(path.read_text())["user"]["created"] is True
    path.chmod(0o666)
    assert uninstall.read_receipt(path, i.config, i.target, True) == {}


def test_unrelated_sqlite_database_is_retained(installation):
    i = installation
    shutil.rmtree(i.data)
    i.data.mkdir()
    with sqlite3.connect(i.data / "rss-watch.sqlite3") as db:
        db.execute("CREATE TABLE another_application(value TEXT)")
    before = tree(i.data)
    p = run(i, "--purge", "--yes")
    assert p.returncode == 1 and "无法确认" in p.stdout
    assert tree(i.data) == before and i.config.exists()


def test_configured_data_symlink_aborts_before_changes(installation):
    i = installation
    link = i.root / "linked-data"
    link.symlink_to(i.data, target_is_directory=True)
    i.config.write_text(
        f"data_dir={json.dumps(str(link))}\ncredentials_dir={json.dumps(str(i.creds))}\n"
    )
    before = tree(i.root)
    p = run(i, "--purge", "--yes")
    assert p.returncode == 1 and calls(i) == []
    assert tree(i.root) == before


def test_upgrade_does_not_reclaim_changed_account_identity(installation, monkeypatch):
    i = installation
    user, _ = fake_accounts(monkeypatch)
    receipt.record(i.target, i.config, i.unit, True, True)
    user.pw_dir = "/some/other/home"
    receipt.record(i.target, i.config, i.unit, False, False)
    value = json.loads((i.config.parent / "install-receipt.json").read_text())
    assert value["user"]["created"] is False


@pytest.mark.parametrize("flags", [[], ["--purge", "--yes"]])
def test_owned_global_entry_is_removed(installation, flags):
    i = installation
    path = i.target.parent / "bin/rss-watch"
    entry_helper.install(path, i.target, i.config, "rss-watch")
    p = run(i, *flags)
    assert p.returncode == 0, p.stderr
    assert not path.exists()


def test_global_entry_survives_dry_run_and_stop_failure(installation):
    i = installation
    path = i.target.parent / "bin/rss-watch"
    entry_helper.install(path, i.target, i.config, "rss-watch")
    before = path.read_bytes()
    assert run(i, "--dry-run").returncode == 0
    assert path.read_bytes() == before
    state_change(i, stop_failure=1)
    assert run(i).returncode == 1
    assert path.read_bytes() == before


def test_uninstaller_preserves_unrelated_global_command(installation):
    i = installation
    path = i.target.parent / "bin/rss-watch"
    path.parent.mkdir()
    path.write_text("another application")
    p = run(i)
    assert p.returncode == 0, p.stderr
    assert path.read_text() == "another application"
