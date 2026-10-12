from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

from .config import Config, credential_path, file_lock, read_credential
from .db import SCHEMA_VERSION, Database
from .maintenance import data_lock
from .settings import save_settings, validate_settings

MAX_ARCHIVE = 100 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024


def backup_root(service):
    root = service.config.data_dir / "backups"
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def create_backup(service, prefix="manual", *, locked=False):
    name = f"{prefix}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    destination = backup_root(service) / name
    service.backup(destination, _locked=locked)
    return destination


def list_backups(service):
    return [p for p in sorted(backup_root(service).iterdir(), reverse=True)
            if p.is_dir() and not p.is_symlink() and (p / "manifest.json").is_file()]


def find_backup(service, name):
    if Path(name).name != name or name not in {p.name for p in list_backups(service)}:
        raise ValueError("备份不存在")
    return backup_root(service) / name


def zip_backup(directory: Path, destination: Path):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("rss-watch.sqlite3", "manifest.json"):
            archive.write(directory / name, name)
        for path in sorted((directory / "credentials").glob("*.json")):
            archive.write(path, "credentials/" + path.name)
    destination.chmod(0o600)


def verify_directory(directory: Path):
    path = directory / "rss-watch.sqlite3"
    if not path.is_file() or path.is_symlink():
        raise ValueError("备份缺少数据库")
    with sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True) as db:
        db.row_factory = sqlite3.Row
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, SCHEMA_VERSION):
            raise ValueError("备份数据库版本不兼容")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute(
            "PRAGMA foreign_key_check"
        ).fetchone():
            raise ValueError("备份数据库完整性校验失败")
        # Check the required v1 data shape before allowing any live changes.
        for table, columns in {
            "sources": {"id", "url", "initialized", "revision", "deleted"},
            "articles": {"source_id", "fingerprint"},
            "deliveries": {"id", "state", "attempts", "payload", "bot_id"},
            "rules": {"id", "bot_id", "includes", "source_ids"},
            "bots": {"id", "deleted", "credential_ref"},
            "runtime": {"key", "value"},
        }.items():
            actual = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual:
                raise ValueError("备份数据库结构无效")
        refs = {row[0] for row in db.execute("SELECT credential_ref FROM bots WHERE deleted=0")}
        if version == SCHEMA_VERSION:
            validate_settings({row[0]: json.loads(row[1]) for row in db.execute(
                "SELECT key,value FROM settings"
            )})
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema_version") != version:
        raise ValueError("备份清单与数据库版本不一致")
    credentials = directory / "credentials"
    if {p.name for p in credentials.iterdir()} != refs:
        raise ValueError("备份凭据集合与活动机器人不一致")
    config = Config(directory, credentials)
    for ref in refs:
        read_credential(config, ref)
    return version


@contextmanager
def unpack_archive(config, archive_path):
    with tempfile.TemporaryDirectory(prefix="restore-", dir=config.data_dir) as temporary:
        directory = Path(temporary)
        (directory / "credentials").mkdir(mode=0o700)
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > 4096 or sum(e.file_size for e in entries) > MAX_EXPANDED:
                raise ValueError("备份文件数量或解压大小超过限制")
            names = set()
            for entry in entries:
                name = entry.filename
                if entry.is_dir():
                    if name != "credentials/":
                        raise ValueError("备份包含不允许的目录")
                    continue
                valid = name in {"rss-watch.sqlite3", "manifest.json"}
                if name.startswith("credentials/"):
                    ref = name.removeprefix("credentials/")
                    credential_path(config, ref)
                    valid = entry.file_size <= 64 * 1024
                mode = entry.external_attr >> 16
                if not valid or name in names or stat.S_ISLNK(mode) or entry.flag_bits & 1:
                    raise ValueError("备份包含不允许或重复的文件")
                if name == "manifest.json" and entry.file_size > 65536:
                    raise ValueError("备份清单过大")
                names.add(name)
                path = directory / name
                with archive.open(entry) as source, path.open("xb") as output:
                    path.chmod(0o600)
                    shutil.copyfileobj(source, output)
        verify_directory(directory)
        yield directory


def copy_snapshot(service, directory):
    database = directory / "rss-watch.sqlite3"
    with sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True) as source:
        refs = source.execute("SELECT DISTINCT credential_ref FROM bots WHERE deleted=0").fetchall()
    for row in refs:
        ref = row[0]
        fd, name = tempfile.mkstemp(prefix=".restore-", dir=service.config.credentials_dir)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write((directory / "credentials" / ref).read_bytes())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, service.config.credentials_dir / ref)
        finally:
            temporary.unlink(missing_ok=True)
    fd = os.open(service.config.credentials_dir, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    # SQLite's backup API replaces the database transactionally, including WAL coordination.
    with sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True) as source:
        with sqlite3.connect(service.config.database, timeout=30) as target:
            source.backup(target)


def restore_backup(service, directory):
    with file_lock(service.config.data_dir / "restore.lock", blocking=False):
        return _restore_backup(service, directory)


def _restore_backup(service, directory):
    verify_directory(directory)
    marker = service.config.data_dir / "maintenance"
    marker.touch(mode=0o600, exist_ok=True)
    with data_lock(service.config, exclusive=True):
        rollback = create_backup(service, "pre-restore", locked=True)
        marker.write_text(json.dumps({"rollback": rollback.name, "started": time.time()}))
        admin = service.db.rows("SELECT * FROM admins")
        old_refs = {row["credential_ref"] for row in service.db.rows(
            "SELECT credential_ref FROM bots WHERE deleted=0"
        )}
        try:
            copy_snapshot(service, directory)
            service.db = Database(service.config.database)
            with service.db.transaction() as db:
                if admin:
                    db.execute("DELETE FROM admins")
                    db.execute("INSERT INTO admins VALUES (1,?,?)",
                               (admin[0]["username"], admin[0]["password_hash"]))
                db.execute("DELETE FROM sessions")
                db.execute("DELETE FROM runtime")
            save_settings(service.db, {"fetch_paused": True, "delivery_paused": True})
            new_refs = {row["credential_ref"] for row in service.db.rows(
                "SELECT credential_ref FROM bots WHERE deleted=0"
            )}
            for ref in old_refs - new_refs:
                credential_path(service.config, ref).unlink(missing_ok=True)
        except BaseException:
            copy_snapshot(service, rollback)
            service.db = Database(service.config.database)
            raise
    # Keep maintenance active until the administrator has checked the restored data.
    return rollback
