import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from rss_watch.config import read_credential
from rss_watch.db import Database

HELPER = Path(__file__).parents[1] / "deploy/backup_helper.py"
WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/schema-test"


def helper(*args):
    return subprocess.run([sys.executable, "-I", "-S", str(HELPER), *map(str, args)],
                          capture_output=True, text=True, timeout=10)


def test_standalone_rollback_restores_v1_database_and_rotated_credentials(service, tmp_path):
    bot = service.bot_save("升级前", WEBHOOK, "old-secret")
    ref = service.db.rows("SELECT credential_ref FROM bots")[0]["credential_ref"]
    with service.db.transaction() as db:
        for table in ("settings", "admins", "sessions", "audit"):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=1")
    snapshot = tmp_path / "upgrade-backup"
    result = helper("backup", service.config.data_dir, service.config.credentials_dir, snapshot)
    assert result.returncode == 0, result.stderr
    assert json.loads((snapshot / "manifest.json").read_text())["schema_version"] == 1
    Database(service.config.database)
    service.bot_save("新版本修改", WEBHOOK, "new-secret", bot)
    assert not (service.config.credentials_dir / ref).exists()
    result = helper("restore-if-migrated", service.config.data_dir,
                    service.config.credentials_dir, snapshot)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(service.config.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT name FROM bots WHERE id=?", (bot,)).fetchone()[0] == "升级前"
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='settings'").fetchone()
    assert read_credential(service.config, ref)["secret"] == "old-secret"


def test_standalone_rollback_keeps_data_when_schema_did_not_change(service, tmp_path):
    bot = service.bot_save("原名称", WEBHOOK, "original")
    snapshot = tmp_path / "snapshot"
    service.backup(snapshot)
    service.bot_save("应保留的修改", WEBHOOK, "changed", bot)
    result = helper("restore-if-migrated", service.config.data_dir,
                    service.config.credentials_dir, snapshot)
    assert result.returncode == 0, result.stderr
    assert service.bot_list()[0]["name"] == "应保留的修改"
    ref = service.db.rows("SELECT credential_ref FROM bots")[0]["credential_ref"]
    assert read_credential(service.config, ref)["secret"] == "changed"
