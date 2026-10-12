#!/usr/bin/env python3
"""deploy/backup_helper.py - Standalone deployment helper for rss-watch.

Provides offline/fallback online backup, backup verification, and legacy directory migration
using only the Python standard library.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def credential_path(creds_dir: Path, ref: str) -> Path:
    if not isinstance(ref, str) or Path(ref).name != ref or not ref.endswith(".json"):
        raise ValueError("凭据引用无效")
    return creds_dir / ref


@contextmanager
def open_credential(creds_dir: Path, ref: str):
    # Nonblocking open lets fstat reject FIFOs without waiting for a writer.
    fd = os.open(credential_path(creds_dir, ref), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("凭据文件必须是权限为 0600 的普通文件")
        yield source


def backup_database_and_credentials(data_dir: Path, creds_dir: Path, dest: Path) -> None:
    src_db = data_dir / "rss-watch.sqlite3"
    if not src_db.is_file():
        print(f"FATAL: 数据库文件不存在: {src_db}", file=sys.stderr)
        sys.exit(1)

    # Freeze administrative credential changes across the database snapshot and file copies.
    with file_lock(data_dir / "admin.lock"):
        _backup_database_and_credentials(data_dir, creds_dir, dest)


def _backup_database_and_credentials(data_dir: Path, creds_dir: Path, dest: Path) -> None:
    src_db = data_dir / "rss-watch.sqlite3"
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    dst_db = dest / "rss-watch.sqlite3"

    # 使用 SQLite 在线备份 API 进行原子一致性快照复制
    try:
        with sqlite3.connect(f"file:{src_db}?mode=ro", uri=True) as src:
            v = src.execute("PRAGMA user_version").fetchone()
            if not v or v[0] not in (1, 2):
                print(
                    f"FATAL: 数据库版本校验失败 (user_version={v[0] if v else 'None'}, 期望 1 或 2)",
                    file=sys.stderr,
                )
                sys.exit(1)
            with sqlite3.connect(dst_db) as dst:
                src.backup(dst)
    except Exception as exc:
        print(f"FATAL: SQLite 在线备份执行异常: {exc}", file=sys.stderr)
        sys.exit(1)

    dst_db.chmod(0o600)
    target_creds = dest / "credentials"
    target_creds.mkdir(mode=0o700, exist_ok=True)

    with sqlite3.connect(dst_db) as db:
        db.row_factory = sqlite3.Row
        bots = db.execute("SELECT id, name, credential_ref FROM bots WHERE deleted=0").fetchall()
        copied = set()
        for row in bots:
            ref = row["credential_ref"]
            if ref in copied:
                continue
            bot_id = row["id"]
            bot_name = row["name"]
            try:
                with open_credential(creds_dir, ref) as source:
                    fd = os.open(
                        target_creds / ref, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(fd, "wb") as output:
                        shutil.copyfileobj(source, output)
            except FileNotFoundError:
                print(
                    f"FATAL: 数据库引用的凭据物理文件不存在: {ref} (机器人 #{bot_id} {bot_name})",
                    file=sys.stderr,
                )
                sys.exit(1)
            except (OSError, ValueError) as exc:
                print(
                    f"FATAL: 凭据备份失败: {ref} (机器人 #{bot_id} {bot_name}): {exc}",
                    file=sys.stderr,
                )
                sys.exit(1)
            copied.add(ref)

    manifest_file = dest / "manifest.json"
    manifest_data = {
        "schema_version": v[0],
        "type": "online_backup_fallback",
        "data_dir": str(data_dir.resolve()),
        "credentials_dir": str(creds_dir.resolve()),
    }
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    manifest_file.chmod(0o600)
    print(f"备份成功完成: {dest}")


def verify_backup(dest: Path) -> None:
    db_file = dest / "rss-watch.sqlite3"
    target_creds = dest / "credentials"
    if not db_file.is_file():
        print(f"FATAL: 备份验证失败，缺少数据库文件: {db_file}", file=sys.stderr)
        sys.exit(1)
    if not target_creds.is_dir():
        print(f"FATAL: 备份验证失败，缺少凭据目录: {target_creds}", file=sys.stderr)
        sys.exit(1)

    try:
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        v = conn.execute("PRAGMA user_version").fetchone()
        if not v or v[0] not in (1, 2):
            print(
                f"FATAL: 备份数据库版本校验失败 (user_version={v[0] if v else 'None'}, 期望 1 或 2)",
                file=sys.stderr,
            )
            conn.close()
            sys.exit(1)
        bots = conn.execute("SELECT id, name, credential_ref FROM bots WHERE deleted=0").fetchall()
        conn.close()
    except Exception as exc:
        print(f"FATAL: 连接备份数据库校验失败: {exc}", file=sys.stderr)
        sys.exit(1)

    for bot in bots:
        ref = bot["credential_ref"]
        bot_id = bot["id"]
        bot_name = bot["name"]
        try:
            with open_credential(target_creds, ref):
                pass
        except FileNotFoundError:
            print(
                f"FATAL: 备份验证失败，缺少机器人 #{bot_id} ({bot_name}) 凭据文件: {ref}",
                file=sys.stderr,
            )
            sys.exit(1)
        except (OSError, ValueError) as exc:
            print(
                f"FATAL: 备份凭据校验失败: {ref} (机器人 #{bot_id} {bot_name}): {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Recovery copies every top-level *.json, so require exactly the active refs.
    active_refs = {bot["credential_ref"] for bot in bots}
    try:
        actual_refs = {
            path.name for path in target_creds.iterdir() if path.name.endswith(".json")
        }
    except OSError as exc:
        print(f"FATAL: 备份凭据目录校验失败: {exc}", file=sys.stderr)
        sys.exit(1)
    if actual_refs != active_refs:
        print(
            "FATAL: 备份凭据集合与活动机器人引用不一致 "
            f"(额外: {sorted(actual_refs - active_refs)}, 缺少: {sorted(active_refs - actual_refs)})",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"备份校验全部通过: {dest}")


def restore_if_migrated(data_dir: Path, creds_dir: Path, backup_dir: Path) -> None:
    """Installer rollback only: caller must have successfully stopped the new service."""
    database = data_dir / "rss-watch.sqlite3"
    snapshot = backup_dir / "rss-watch.sqlite3"
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        old_version = db.execute("PRAGMA user_version").fetchone()[0]
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        new_version = db.execute("PRAGMA user_version").fetchone()[0]
    if old_version not in (1, 2) or new_version not in (1, 2):
        raise ValueError("数据库版本不兼容，拒绝自动回滚")
    if old_version == new_version:
        return
    verify_backup(backup_dir)
    # Freeze new CLI writes as well as credential changes; the service is already stopped.
    with file_lock(data_dir / "data.lock"), file_lock(data_dir / "admin.lock"):
        for lock in ("data.lock", "admin.lock"):
            if os.geteuid() == 0:
                owner = data_dir.stat()
                os.chown(data_dir / lock, owner.st_uid, owner.st_gid)
        with sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True) as source:
            refs = source.execute("SELECT DISTINCT credential_ref FROM bots WHERE deleted=0")
            owner = creds_dir.stat()
            for (ref,) in refs:
                fd, name = tempfile.mkstemp(prefix=".rollback-", dir=creds_dir)
                temporary = Path(name)
                try:
                    with os.fdopen(fd, "wb") as output:
                        if os.geteuid() == 0:
                            os.fchown(output.fileno(), owner.st_uid, owner.st_gid)
                        with open_credential(backup_dir / "credentials", ref) as credential:
                            shutil.copyfileobj(credential, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, credential_path(creds_dir, ref))
                finally:
                    temporary.unlink(missing_ok=True)
            with sqlite3.connect(database) as target:
                source.backup(target)
    print(f"已恢复升级前数据库 schema {old_version} 与活动凭据")


def migrate_legacy_files(target_dir: Path, legacy_dir: Path) -> None:
    """Migrate all files/directories from target_dir to legacy_dir, except releases, current, and metadata."""
    legacy_dir.mkdir(parents=True, exist_ok=True)
    preserved = {"releases", "current", ".prev_service_unit.bak", ".prev_install_mode"}
    for item in target_dir.iterdir():
        if item.name in preserved:
            continue
        dest_item = legacy_dir / item.name
        if dest_item.exists():
            if dest_item.is_dir() and not dest_item.is_symlink():
                shutil.rmtree(dest_item)
            else:
                dest_item.unlink()
        shutil.move(str(item), str(dest_item))
    print(f"旧版本单目录文件已归档至: {legacy_dir}")


def restore_legacy_files(legacy_dir: Path, target_dir: Path) -> None:
    """Restore all files/directories from legacy_dir back into target_dir."""
    if not legacy_dir.is_dir():
        print(f"WARN: 待恢复的旧目录不存在: {legacy_dir}", file=sys.stderr)
        return
    for item in legacy_dir.iterdir():
        dest_item = target_dir / item.name
        if dest_item.exists():
            if dest_item.is_dir() and not dest_item.is_symlink():
                shutil.rmtree(dest_item)
            else:
                dest_item.unlink()
        shutil.move(str(item), str(dest_item))
    try:
        legacy_dir.rmdir()
    except OSError:
        pass
    print(f"已恢复旧版本单目录文件至: {target_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="rss-watch deployment helper")
    subparsers = parser.add_subparsers(dest="action", required=True)

    backup_parser = subparsers.add_parser("backup", help="Perform fallback online backup")
    backup_parser.add_argument("data_dir", type=Path)
    backup_parser.add_argument("creds_dir", type=Path)
    backup_parser.add_argument("dest", type=Path)

    verify_parser = subparsers.add_parser("verify", help="Verify backup integrity and credentials")
    verify_parser.add_argument("dest", type=Path)

    rollback_parser = subparsers.add_parser("restore-if-migrated", help="Restore schema on failed upgrade")
    rollback_parser.add_argument("data_dir", type=Path)
    rollback_parser.add_argument("creds_dir", type=Path)
    rollback_parser.add_argument("backup_dir", type=Path)

    migrate_parser = subparsers.add_parser(
        "migrate-legacy", help="Move legacy single-directory files to archive"
    )
    migrate_parser.add_argument("target_dir", type=Path)
    migrate_parser.add_argument("legacy_dir", type=Path)

    restore_parser = subparsers.add_parser(
        "restore-legacy", help="Restore legacy single-directory files from archive"
    )
    restore_parser.add_argument("legacy_dir", type=Path)
    restore_parser.add_argument("target_dir", type=Path)

    args = parser.parse_args()

    if args.action == "backup":
        backup_database_and_credentials(args.data_dir, args.creds_dir, args.dest)
    elif args.action == "verify":
        verify_backup(args.dest)
    elif args.action == "restore-if-migrated":
        restore_if_migrated(args.data_dir, args.creds_dir, args.backup_dir)
    elif args.action == "migrate-legacy":
        migrate_legacy_files(args.target_dir, args.legacy_dir)
    elif args.action == "restore-legacy":
        restore_legacy_files(args.legacy_dir, args.target_dir)


if __name__ == "__main__":
    main()
