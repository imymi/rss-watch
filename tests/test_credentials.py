import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from threading import Event

import pytest

from rss_watch.config import Config, read_credential, write_credential
from rss_watch.db import Database

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/lifecycle"


def credential_files(service):
    return {path.name for path in service.config.credentials_dir.iterdir()}


def bot_ref(service, bot):
    return service.db.rows("SELECT credential_ref FROM bots WHERE id=?", (bot,))[0]["credential_ref"]


def test_rotation_cleans_only_the_replaced_unused_credential(service):
    bot = service.bot_save("old", WEBHOOK, "old-secret")
    old_ref = bot_ref(service, bot)
    orphan = write_credential(service.config, WEBHOOK, "unrelated")
    with service.db.transaction() as db:
        db.execute("INSERT INTO bots(name,credential_ref,deleted) VALUES (?,?,1)",
                   ("deleted", old_ref))
    assert service.bot_save("new", WEBHOOK, "new-secret", bot) == bot
    new_ref = bot_ref(service, bot)
    assert new_ref != old_ref
    assert credential_files(service) == {new_ref, orphan}
    assert service.bot_list() == [{"id": bot, "name": "new"}]
    assert read_credential(service.config, new_ref)["secret"] == "new-secret"
    assert stat.S_IMODE((service.config.credentials_dir / new_ref).stat().st_mode) == 0o600


def test_delete_cleans_credentials_but_keeps_sent_history(service, tmp_path):
    bot = service.bot_save("bot", WEBHOOK)
    event = service.bot_test(bot)
    with pytest.raises(ValueError, match="未完成"):
        service.bot_delete(bot)
    assert credential_files(service) == {bot_ref(service, bot)}
    with service.db.transaction() as db:
        db.execute("UPDATE deliveries SET state='sent' WHERE id=?", (event,))
    service.bot_delete(bot)
    assert not service.bot_list()
    assert credential_files(service) == set()
    assert service.history()[0]["id"] == event
    target = tmp_path / "backup"
    service.backup(target)
    assert list((target / "credentials").iterdir()) == []
    assert Database(target / "rss-watch.sqlite3").rows("SELECT * FROM deliveries")[0]["id"] == event


@pytest.mark.parametrize("action", ["update", "delete"])
def test_shared_active_credential_is_kept_until_last_reference(service, action):
    bot = service.bot_save("first", WEBHOOK)
    old_ref = bot_ref(service, bot)
    with service.db.transaction() as db:
        other = db.execute("INSERT INTO bots(name,credential_ref) VALUES (?,?)",
                           ("second", old_ref)).lastrowid
    if action == "update":
        service.bot_save("updated", WEBHOOK, "new-secret", bot)
        remaining = {bot_ref(service, bot)}
    else:
        service.bot_delete(bot)
        remaining = set()
    assert credential_files(service) == {old_ref, *remaining}
    assert read_credential(service.config, old_ref)["webhook"] == WEBHOOK
    service.bot_delete(other)
    assert credential_files(service) == remaining


@pytest.mark.parametrize("action", ["add", "update", "delete"])
@pytest.mark.parametrize("failure", ["write", "commit"])
def test_transaction_failure_preserves_old_and_removes_new(service, monkeypatch, action, failure):
    bot = service.bot_save("original", WEBHOOK, "original-secret")
    old_rows = service.db.rows("SELECT * FROM bots")
    old_files = credential_files(service)
    created = []

    def track_credential(*args):
        ref = write_credential(*args)
        created.append(ref)
        return ref

    monkeypatch.setattr("rss_watch.service.write_credential", track_credential)
    if failure == "write":
        operation = "INSERT" if action == "add" else "UPDATE"
        with service.db.transaction() as db:
            db.execute(f"""CREATE TRIGGER reject_mutation BEFORE {operation} ON bots
                           BEGIN SELECT RAISE(ABORT, 'forced failure'); END""")
        error = sqlite3.IntegrityError
    else:
        connect = sqlite3.connect

        class FailedCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("forced failure")

        monkeypatch.setattr("rss_watch.db.sqlite3.connect", lambda *args, **kwargs: connect(
            *args, **kwargs, factory=FailedCommit,
        ))
        error = sqlite3.OperationalError
    with pytest.raises(error, match="forced failure"):
        if action == "delete":
            service.bot_delete(bot)
        else:
            service.bot_save("replacement", WEBHOOK, "replacement-secret",
                             bot if action == "update" else None)
    assert len(created) == (0 if action == "delete" else 1)
    assert service.db.rows("SELECT * FROM bots") == old_rows
    assert credential_files(service) == old_files
    assert read_credential(service.config, bot_ref(service, bot))["secret"] == "original-secret"


def test_failed_reference_check_does_not_delete_credentials(service, monkeypatch):
    bot = service.bot_save("original", WEBHOOK)
    old_ref = bot_ref(service, bot)
    rows = service.db.rows

    def failed_rows(*args):
        raise sqlite3.OperationalError("reference check failed")

    with monkeypatch.context() as patch:
        patch.setattr(service.db, "rows", failed_rows)
        with pytest.raises(sqlite3.OperationalError, match="reference check failed"):
            service.bot_save("updated", WEBHOOK, "new-secret", bot)
    new_ref = rows("SELECT credential_ref FROM bots WHERE id=?", (bot,))[0]["credential_ref"]
    assert new_ref != old_ref
    assert credential_files(service) == {old_ref, new_ref}
    assert read_credential(service.config, new_ref)["secret"] == "new-secret"


def test_error_after_commit_does_not_remove_the_active_new_credential(service, monkeypatch):
    bot = service.bot_save("original", WEBHOOK)
    old_ref = bot_ref(service, bot)
    transaction = service.db.transaction

    @contextmanager
    def error_after_commit():
        with transaction() as db:
            yield db
        raise sqlite3.OperationalError("transaction exit failed")

    monkeypatch.setattr(service.db, "transaction", error_after_commit)
    with pytest.raises(sqlite3.OperationalError, match="transaction exit failed"):
        service.bot_save("updated", WEBHOOK, "new-secret", bot)
    new_ref = bot_ref(service, bot)
    assert new_ref != old_ref
    assert credential_files(service) == {old_ref, new_ref}
    assert read_credential(service.config, new_ref)["secret"] == "new-secret"


@pytest.mark.parametrize("action", ["update", "delete"])
def test_cleanup_rejects_path_escape(service, tmp_path, action):
    outside = tmp_path / "outside.json"
    outside.write_text("must survive")
    with service.db.transaction() as db:
        bot = db.execute("INSERT INTO bots(name,credential_ref) VALUES (?,?)",
                         ("invalid", "../outside.json")).lastrowid
    before = service.db.rows("SELECT * FROM bots")
    with pytest.raises(ValueError, match="凭据引用无效"):
        if action == "update":
            service.bot_save("replacement", WEBHOOK, id=bot)
        else:
            service.bot_delete(bot)
    assert outside.read_text() == "must survive"
    assert service.db.rows("SELECT * FROM bots") == before
    assert credential_files(service) == set()


def test_cleanup_unlinks_symlink_without_following_target(service, tmp_path):
    bot = service.bot_save("bot", WEBHOOK)
    path = service.config.credentials_dir / bot_ref(service, bot)
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    service.bot_delete(bot)
    assert credential_files(service) == set()
    assert outside.is_file()


def test_backup_contains_only_distinct_active_credentials(service, tmp_path):
    bot = service.bot_save("active", WEBHOOK, "active-secret")
    ref = bot_ref(service, bot)
    stale = write_credential(service.config, WEBHOOK, "deleted-secret")
    orphan = write_credential(service.config, WEBHOOK, "orphan-secret")
    with service.db.transaction() as db:
        db.executemany("INSERT INTO bots(name,credential_ref,deleted) VALUES (?,?,?)", [
            ("shared active", ref, 0), ("deleted", stale, 1),
            ("deleted missing file", "missing.json", 1),
        ])
    target = tmp_path / "backup"
    service.backup(target)
    assert {path.name for path in (target / "credentials").iterdir()} == {ref}
    assert credential_files(service) == {ref, stale, orphan}
    assert (target / "credentials" / ref).read_bytes() == (
        service.config.credentials_dir / ref
    ).read_bytes()
    restored = Config(target, target / "credentials")
    assert read_credential(restored, ref)["secret"] == "active-secret"
    active = Database(restored.database).rows("SELECT * FROM bots WHERE deleted=0")
    assert len(active) == 2 and {row["credential_ref"] for row in active} == {ref}


@pytest.mark.parametrize("invalid", ["missing", "permissions", "symlink", "path_escape"])
def test_backup_fails_closed_on_invalid_active_credential(service, tmp_path, invalid):
    bot = service.bot_save("bot", WEBHOOK)
    path = service.config.credentials_dir / bot_ref(service, bot)
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    outside.chmod(0o600)
    if invalid == "missing":
        path.unlink()
    elif invalid == "permissions":
        path.chmod(0o644)
    elif invalid == "symlink":
        path.unlink()
        path.symlink_to(outside)
    else:
        with service.db.transaction() as db:
            db.execute("UPDATE bots SET credential_ref=? WHERE id=?", ("../outside.json", bot))
    target = tmp_path / "backup"
    with pytest.raises((OSError, ValueError)):
        service.backup(target)
    assert not target.exists()
    assert read_credential(Config(tmp_path, tmp_path), "outside.json")["webhook"] == WEBHOOK


@pytest.mark.parametrize("action", ["update", "delete"])
def test_backup_lock_protects_snapshot_credentials(service, tmp_path, monkeypatch, action):
    bot = service.bot_save("bot", WEBHOOK, "snapshot-secret")
    ref = bot_ref(service, bot)
    snapshot_ready, release_snapshot, mutation_started = Event(), Event(), Event()
    backup = service.db.backup

    def paused_backup(destination):
        backup(destination)
        snapshot_ready.set()
        assert release_snapshot.wait(5)

    def mutate():
        mutation_started.set()
        if action == "update":
            service.bot_save("updated", WEBHOOK, "new-secret", bot)
        else:
            service.bot_delete(bot)

    monkeypatch.setattr(service.db, "backup", paused_backup)
    target = tmp_path / "backup"
    with ThreadPoolExecutor(max_workers=2) as pool:
        backup_future = pool.submit(service.backup, target)
        try:
            assert snapshot_ready.wait(5)
            mutation_future = pool.submit(mutate)
            assert mutation_started.wait(5)
            with pytest.raises(TimeoutError):
                mutation_future.result(timeout=0.1)
            assert credential_files(service) == {ref}
        finally:
            release_snapshot.set()
        backup_future.result(timeout=5)
        mutation_future.result(timeout=5)
    restored = Config(target, target / "credentials")
    assert read_credential(restored, ref)["secret"] == "snapshot-secret"
    assert not (service.config.credentials_dir / ref).exists()
