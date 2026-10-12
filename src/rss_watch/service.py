from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from .config import (
    Config,
    credential_path,
    file_lock,
    open_credential,
    read_credential,
    validate_url,
    write_credential,
)
from .db import SCHEMA_VERSION, Database
from .domain import match, normalize_word
from .maintenance import business_write, data_lock
from .settings import read_settings


def required(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label}不能为空")
    return value


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.database)

    def source_list(self) -> list[dict]:
        return self.db.rows("SELECT * FROM sources WHERE deleted=0 ORDER BY id")

    @business_write
    def source_save(self, name: str, url: str, interval: int | None = None, id: int | None = None):
        name = required(name, "名称")
        validate_url(url)
        settings = read_settings(self.db)
        if interval is None:
            interval = settings["default_interval"]
        if interval < 60:
            raise ValueError("抓取间隔至少为 60 秒")
        with self.db.transaction() as db:
            if id is None:
                if db.execute("SELECT count(*) FROM sources WHERE deleted=0").fetchone()[0] >= settings["max_sources"]:
                    raise ValueError(f"最多支持 {settings['max_sources']} 个订阅源")
                return db.execute(
                    "INSERT INTO sources(name,url,interval) VALUES (?,?,?)", (name, url, interval)
                ).lastrowid
            old = db.execute("SELECT * FROM sources WHERE id=? AND deleted=0", (id,)).fetchone()
            if not old:
                raise ValueError("订阅源不存在")
            if old["url"] != url:
                raise ValueError("URL 是订阅源身份；更换 URL 请删除旧源并新增，以重新建立基线")
            db.execute(
                "UPDATE sources SET name=?,interval=?,revision=revision+1,next_fetch=0 WHERE id=?",
                (name, interval, id),
            )
            return id

    @business_write
    def source_enable(self, id: int, enabled: bool):
        with self.db.transaction() as db:
            result = db.execute(
                """UPDATE sources SET enabled=?,revision=revision+1,next_fetch=0
                   WHERE id=? AND deleted=0""", (int(enabled), id)
            )
            if not result.rowcount:
                raise ValueError("订阅源不存在")

    @business_write
    def source_delete(self, id: int):
        with self.db.transaction() as db:
            result = db.execute(
                "UPDATE sources SET deleted=1,enabled=0,revision=revision+1 WHERE id=? AND deleted=0",
                (id,),
            )
            if not result.rowcount:
                raise ValueError("订阅源不存在")

    def bot_list(self) -> list[dict]:
        return self.db.rows("SELECT id,name FROM bots WHERE deleted=0 ORDER BY id")

    def _remove_unused_credential(self, ref: str):
        # Caller holds admin.lock; a failed reference check must never authorize deletion.
        path = credential_path(self.config, ref)
        if not self.db.rows(
            "SELECT 1 FROM bots WHERE credential_ref=? AND deleted=0 LIMIT 1", (ref,)
        ):
            path.unlink(missing_ok=True)

    @business_write
    def bot_save(self, name: str, webhook: str, secret: str = "", id: int | None = None):
        name = required(name, "名称")
        with file_lock(self.config.data_dir / "admin.lock"):
            ref = old_ref = None
            try:
                with self.db.transaction() as db:
                    if id is not None:
                        old = db.execute(
                            "SELECT credential_ref FROM bots WHERE id=? AND deleted=0", (id,)
                        ).fetchone()
                        if not old:
                            raise ValueError("机器人不存在")
                        old_ref = old["credential_ref"]
                        credential_path(self.config, old_ref)
                    ref = write_credential(self.config, webhook, secret)
                    if id is None:
                        id = db.execute(
                            "INSERT INTO bots(name,credential_ref) VALUES (?,?)", (name, ref)
                        ).lastrowid
                    else:
                        db.execute(
                            "UPDATE bots SET name=?,credential_ref=? WHERE id=?", (name, ref, id)
                        )
            except BaseException:
                if ref is not None:
                    self._remove_unused_credential(ref)
                raise
            if old_ref is not None:
                self._remove_unused_credential(old_ref)
            return id

    @business_write
    def bot_delete(self, id: int):
        with file_lock(self.config.data_dir / "admin.lock"):
            with self.db.transaction() as db:
                if db.execute("SELECT 1 FROM rules WHERE bot_id=?", (id,)).fetchone():
                    raise ValueError("请先删除或改绑引用该机器人的规则")
                if db.execute(
                    "SELECT 1 FROM deliveries WHERE bot_id=? AND state!='sent'", (id,)
                ).fetchone():
                    raise ValueError("该机器人还有未完成或失败任务，不能删除")
                old = db.execute(
                    "SELECT credential_ref FROM bots WHERE id=? AND deleted=0", (id,)
                ).fetchone()
                if not old:
                    raise ValueError("机器人不存在")
                ref = old["credential_ref"]
                credential_path(self.config, ref)
                db.execute("UPDATE bots SET deleted=1 WHERE id=?", (id,))
            self._remove_unused_credential(ref)

    def rule_list(self) -> list[dict]:
        return self.db.rows("SELECT * FROM rules ORDER BY id")

    @business_write
    def rule_save(self, name: str, includes: list[str], excludes: list[str], mode: str,
                  source_ids: list[int], bot_id: int, id: int | None = None):
        name = required(name, "名称")
        if mode not in {"any", "all"}:
            raise ValueError("匹配模式必须是 any 或 all")
        def words(values):
            result = {}
            for value in values:
                word = required(value, "关键词")
                result.setdefault(normalize_word(word), word)
            return list(result.values())
        includes, excludes = words(includes), words(excludes)
        if not includes:
            raise ValueError("至少需要一个包含词")
        source_ids = sorted(set(source_ids))
        with self.db.transaction() as db:
            if not db.execute("SELECT 1 FROM bots WHERE id=? AND deleted=0", (bot_id,)).fetchone():
                raise ValueError("机器人不存在")
            for source_id in source_ids:
                if not db.execute(
                    "SELECT 1 FROM sources WHERE id=? AND deleted=0", (source_id,)
                ).fetchone():
                    raise ValueError("指定的订阅源不存在")
            args = (name, json.dumps(includes, ensure_ascii=False),
                    json.dumps(excludes, ensure_ascii=False), mode, json.dumps(source_ids), bot_id)
            if id is None:
                return db.execute(
                    """INSERT INTO rules(name,includes,excludes,mode,source_ids,bot_id)
                       VALUES (?,?,?,?,?,?)""", args
                ).lastrowid
            if not db.execute(
                """UPDATE rules SET name=?,includes=?,excludes=?,mode=?,source_ids=?,bot_id=?
                   WHERE id=?""", (*args, id)
            ).rowcount:
                raise ValueError("规则不存在")
            return id

    @business_write
    def rule_enable(self, id: int, enabled: bool):
        with self.db.transaction() as db:
            if not db.execute("UPDATE rules SET enabled=? WHERE id=?", (int(enabled), id)).rowcount:
                raise ValueError("规则不存在")

    @business_write
    def rule_delete(self, id: int):
        with self.db.transaction() as db:
            if not db.execute("DELETE FROM rules WHERE id=?", (id,)).rowcount:
                raise ValueError("规则不存在")

    def rule_test(self, id: int, title: str, summary: str):
        rows = self.db.rows("SELECT * FROM rules WHERE id=?", (id,))
        if not rows:
            raise ValueError("规则不存在")
        return match(rows[0], title, summary)

    @business_write
    def bot_test(self, id: int) -> str:
        event = str(uuid.uuid4())
        now = time.time()
        with self.db.transaction() as db:
            if not db.execute("SELECT 1 FROM bots WHERE id=? AND deleted=0", (id,)).fetchone():
                raise ValueError("机器人不存在")
            db.execute(
                """INSERT INTO deliveries(id,bot_id,payload,matches,created,updated)
                   VALUES (?,?,?,?,?,?)""",
                (event, id, f"RSS 命中提醒\n测试消息\n事件：{event}", "[]", now, now),
            )
        return event

    def history(self, limit: int = 30, state: str | None = None):
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        if state and state not in {"pending", "sending", "sent", "failed"}:
            raise ValueError("未知推送状态")
        return self.db.rows(
            "SELECT * FROM deliveries " + ("WHERE state=? " if state else "")
            + "ORDER BY created DESC LIMIT ?", (state, limit) if state else (limit,)
        )

    @business_write
    def retry(self, event_id: str):
        with self.db.transaction() as db:
            if not db.execute(
                """UPDATE deliveries SET state='pending',attempts=0,next_attempt=0,
                   updated=?,last_error=NULL WHERE id=? AND state='failed'""",
                (time.time(), event_id),
            ).rowcount:
                raise ValueError("任务不存在或不是失败状态")

    def status(self):
        runtime = {r["key"]: r["value"] for r in self.db.rows("SELECT * FROM runtime")}
        heartbeat = float(runtime.get("heartbeat", 0))
        return {
            "后台运行": runtime.get("running") == "1" and time.time() - heartbeat < 10,
            "心跳时间": heartbeat or None,
            "队列": self.db.rows("SELECT state,count(*) AS count FROM deliveries GROUP BY state"),
            "源错误": self.db.rows(
                "SELECT id,name,last_error FROM sources WHERE deleted=0 AND last_error IS NOT NULL"
            ),
            "最近推送错误": self.db.rows(
                "SELECT id,last_error FROM deliveries WHERE last_error IS NOT NULL "
                "ORDER BY updated DESC LIMIT 5"
            ),
        }

    def health(self):
        with self.db.connect() as db:
            database = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        errors = []
        for bot in self.db.rows("SELECT id,credential_ref FROM bots WHERE deleted=0"):
            try:
                read_credential(self.config, bot["credential_ref"])
            except (OSError, ValueError, KeyError, TypeError):
                errors.append(bot["id"])
        return {"database_ok": database, "credential_errors": errors}

    def backup(self, destination: Path, *, _locked: bool = False):
        # Administrative lock freezes credential references; daemon writes remain online.
        with (nullcontext() if _locked else data_lock(self.config)), file_lock(self.config.data_dir / "admin.lock"):
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
            try:
                self.db.backup(destination / "rss-watch.sqlite3")
                target = destination / "credentials"
                target.mkdir(mode=0o700)
                refs = Database(destination / "rss-watch.sqlite3").rows(
                    "SELECT DISTINCT credential_ref FROM bots WHERE deleted=0"
                )
                for row in refs:
                    ref = row["credential_ref"]
                    with open_credential(self.config, ref) as source:
                        fd = os.open(target / ref, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as output:
                            shutil.copyfileobj(source, output)
                (destination / "rss-watch.sqlite3").chmod(0o600)
                (destination / "manifest.json").write_text(json.dumps({
                    "schema_version": SCHEMA_VERSION, "created": time.time(),
                    "data_dir": str(self.config.data_dir),
                    "credentials_dir": str(self.config.credentials_dir),
                }, indent=2))
                (destination / "manifest.json").chmod(0o600)
            except BaseException:
                shutil.rmtree(destination)
                raise
