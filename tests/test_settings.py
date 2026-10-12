import asyncio
import os
import sqlite3
import time

import httpx
import pytest

from rss_watch.db import Database
from rss_watch.settings import read_settings, save_settings
from rss_watch.web import automatic_backup
from rss_watch.worker import Worker

from .conftest import rss
from .test_daemon import wait_until


def test_migration_preserves_v1_data_and_keeps_rollback(configured):
    svc, bot, source, rule = configured
    event = svc.bot_test(bot)
    with svc.db.transaction() as db:
        for table in ("settings", "admins", "sessions", "audit"):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=1")
    migrated = Database(svc.config.database)
    assert migrated.rows("SELECT id FROM sources")[0]["id"] == source
    assert migrated.rows("SELECT id FROM rules")[0]["id"] == rule
    assert migrated.rows("SELECT id FROM deliveries")[0]["id"] == event
    snapshots = list(svc.config.data_dir.glob("migration-v1-*.sqlite3"))
    assert len(snapshots) == 1
    with sqlite3.connect(snapshots[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    with migrated.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    Database(svc.config.database)
    assert len(list(svc.config.data_dir.glob("migration-v1-*.sqlite3"))) == 1


@pytest.mark.parametrize("values", [
    {"fetch_concurrency": 0}, {"max_attempts": True}, {"retry_delays": "1,0"},
    {"timezone": "Not/AZone"}, {"https_proxy": "file:///etc/passwd"},
    {"no_proxy": "host/path"}, {"no_such_setting": 1}, {"fetch_paused": "yes"},
])
def test_invalid_settings_are_atomic(service, values):
    with pytest.raises(ValueError):
        save_settings(service.db, {"default_interval": 120, **values})
    assert read_settings(service.db)["default_interval"] == 300


async def test_live_pause_resume_and_custom_concurrency(configured):
    svc, bot, _, _ = configured
    save_settings(svc.db, {"fetch_paused": True, "delivery_paused": True, "fetch_concurrency": 1})
    event = svc.bot_test(bot)
    received = []
    def handler(request):
        received.append(request.method)
        return httpx.Response(200, content=rss()) if request.method == "GET" else httpx.Response(200, json={"code": 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = Worker(svc.config, client)
        task = asyncio.create_task(worker.run())
        try:
            await wait_until(lambda: svc.status()["后台运行"])
            assert not received
            save_settings(svc.db, {"fetch_paused": False, "delivery_paused": False})
            await wait_until(lambda: svc.history(state="sent"))
            await wait_until(lambda: svc.source_list()[0]["initialized"])
            assert svc.history()[0]["id"] == event
        finally:
            worker.stopping.set()
            await task


async def test_configured_retry_count_and_delay(configured):
    svc, bot, _, _ = configured
    save_settings(svc.db, {"max_attempts": 1, "retry_delays": "7", "send_interval": 2})
    svc.bot_test(bot)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
        assert await Worker(svc.config, client).deliver_one()
    row = svc.history()[0]
    assert row["state"] == "failed" and row["attempts"] == 1
    assert row["next_attempt"] >= row["updated"] + 7


async def test_network_setting_change_waits_for_client_recreation(service):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        worker = Worker(service.config, client)
        task = asyncio.create_task(worker.run())
        await wait_until(lambda: service.status()["后台运行"])
        save_settings(service.db, {"request_deadline": 120})
        try:
            await asyncio.wait_for(task, 3)
        finally:
            worker.stopping.set()
            if not task.done():
                await task
    runtime = {r["key"]: r["value"] for r in service.db.rows("SELECT * FROM runtime")}
    assert runtime["settings_revision"] != runtime["settings_applied"]
    assert not service.status()["后台运行"]


def test_automatic_backup_retention_and_disable(service):
    save_settings(service.db, {"backup_keep": 2})
    for _ in range(3):
        with service.db.transaction() as db:
            db.execute("DELETE FROM runtime WHERE key='last_auto_backup'")
        automatic_backup(service)
    root = service.config.data_dir / "backups"
    assert len(list(root.glob("auto-*"))) == 2
    save_settings(service.db, {"backup_hours": 0})
    with service.db.transaction() as db:
        db.execute("DELETE FROM runtime WHERE key='last_auto_backup'")
    automatic_backup(service)
    assert len(list(root.glob("auto-*"))) == 2


def test_configurable_retention_keeps_failed_events(configured):
    svc, bot, _, _ = configured
    first, second = svc.bot_test(bot), svc.bot_test(bot)
    with svc.db.transaction() as db:
        db.execute("UPDATE deliveries SET state='sent',updated=? WHERE id=?",
                   (time.time() - 3 * 86400, first))
        db.execute("UPDATE deliveries SET state='failed',updated=? WHERE id=?",
                   (time.time() - 3 * 86400, second))
    svc.db.cleanup(time.time(), retention_days=1)
    assert [r["id"] for r in svc.history()] == [second]


def test_health_rejects_fifo_credentials_without_blocking(configured):
    svc, bot, _, _ = configured
    ref = svc.db.rows("SELECT credential_ref FROM bots WHERE id=?", (bot,))[0]["credential_ref"]
    path = svc.config.credentials_dir / ref
    path.unlink()
    os.mkfifo(path, mode=0o600)
    assert svc.health() == {"database_ok": True, "credential_errors": [bot]}
