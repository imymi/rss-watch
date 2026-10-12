import json
import os
import subprocess
import sys
import time

import pytest

from rss_watch.archives import create_backup, restore_backup
from rss_watch.config import Config
from rss_watch.domain import parse_feed
from rss_watch.maintenance import data_lock
from rss_watch.service import Service
from rss_watch.worker import Worker

from .conftest import rss


def test_restore_coordinates_with_a_real_running_daemon(configured, tmp_path):
    svc, bot, source, _ = configured
    worker = Worker(svc.config, None)
    worker.commit_feed(svc.source_list()[0], parse_feed(rss(("old", "AI 基线"))), {}, time.time())
    svc.source_enable(source, False)  # This process test never makes outbound requests.
    snapshot = create_backup(svc)
    svc.source_save("修改后的名称", "https://example.com/feed", 60, source)
    path = tmp_path / "config.toml"
    path.write_text(f"data_dir={json.dumps(str(svc.config.data_dir))}\n"
                    f"credentials_dir={json.dumps(str(svc.config.credentials_dir))}\n")
    process = subprocess.Popen([sys.executable, "-m", "rss_watch.cli", "--config", str(path), "run"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=dict(os.environ, PYTHONUNBUFFERED="1"))
    try:
        deadline = time.monotonic() + 8
        while not svc.status()["后台运行"]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        restore_backup(svc, snapshot)
        assert svc.source_list()[0]["name"] == "技术源"
        assert svc.source_list()[0]["initialized"]
        assert svc.db.rows("SELECT fingerprint FROM articles")
        assert not svc.status()["后台运行"]
        assert process.poll() is None  # Maintenance leaves the daemon waiting, not crashing.
        with pytest.raises(ValueError, match="维护模式"):
            svc.bot_test(bot)
        with data_lock(svc.config, exclusive=True):
            (svc.config.data_dir / "maintenance").unlink()
        deadline = time.monotonic() + 8
        while not svc.status()["后台运行"]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=8)
    assert process.returncode == 0, (stdout, stderr)


def test_failed_restore_rolls_back_and_keeps_maintenance(configured, monkeypatch):
    svc, _, source, _ = configured
    snapshot = create_backup(svc)
    svc.source_save("必须保留", "https://example.com/feed", 60, source)
    from rss_watch import archives
    copy = archives.copy_snapshot
    first = True

    def fail_once(*args):
        nonlocal first
        if first:
            first = False
            raise OSError("simulated interrupted restore")
        return copy(*args)
    monkeypatch.setattr(archives, "copy_snapshot", fail_once)
    with pytest.raises(OSError):
        restore_backup(svc, snapshot)
    assert svc.source_list()[0]["name"] == "必须保留"
    assert (svc.config.data_dir / "maintenance").exists()
    assert list((svc.config.data_dir / "backups").glob("pre-restore-*"))


def test_restore_v1_backup_into_v2(configured):
    svc, _, _, _ = configured
    snapshot = create_backup(svc)
    imported = Service(Config(snapshot, snapshot / "credentials"))
    with imported.db.transaction() as db:
        for table in ("settings", "admins", "sessions", "audit"):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=1")
    manifest = json.loads((snapshot / "manifest.json").read_text())
    manifest["schema_version"] = 1
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    restore_backup(svc, snapshot)
    assert svc.source_list() and svc.rule_list() and svc.bot_list()
    with svc.db.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
