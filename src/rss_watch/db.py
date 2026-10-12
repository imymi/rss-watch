from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
 interval INTEGER NOT NULL DEFAULT 300 CHECK(interval >= 60),
 enabled INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
 initialized INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
 etag TEXT, modified TEXT, next_fetch REAL NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0, last_success REAL, last_error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS source_url ON sources(url) WHERE deleted=0;
CREATE TABLE IF NOT EXISTS bots (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, credential_ref TEXT NOT NULL,
 deleted INTEGER NOT NULL DEFAULT 0, next_send REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rules (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, includes TEXT NOT NULL,
 excludes TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('any','all')),
 source_ids TEXT NOT NULL, bot_id INTEGER NOT NULL REFERENCES bots(id),
 enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS articles (
 id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
 fingerprint TEXT NOT NULL, discovered REAL NOT NULL,
 title TEXT, summary TEXT, link TEXT, published TEXT,
 UNIQUE(source_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS deliveries (
 id TEXT PRIMARY KEY, article_id INTEGER REFERENCES articles(id),
 bot_id INTEGER NOT NULL REFERENCES bots(id),
 payload TEXT NOT NULL, matches TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','sending','sent','failed')),
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
 created REAL NOT NULL, updated REAL NOT NULL, last_error TEXT,
 UNIQUE(article_id, bot_id)
);
CREATE INDEX IF NOT EXISTS delivery_due ON deliveries(state, next_attempt);
CREATE INDEX IF NOT EXISTS article_age ON articles(discovered);
CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins (
 id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, created REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise ValueError("数据库版本不兼容，请使用匹配版本的软件")
            if version == SCHEMA_VERSION:
                return
            # Keep a rollback snapshot before the additive v1 -> v2 migration.
            if version == 1:
                snapshot = path.with_name(f"migration-v1-{time.time_ns()}.sqlite3")
                target = sqlite3.connect(snapshot)
                try:
                    connection.backup(target)
                finally:
                    target.close()
                snapshot.chmod(0o600)
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA
                + f"\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def heartbeat(self, running: bool = True) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO runtime VALUES ('heartbeat', ?)", (str(time.time()),))
            db.execute("INSERT OR REPLACE INTO runtime VALUES ('running', ?)", (str(int(running)),))

    def cleanup(self, now: float, retention_days: int = 90) -> None:
        cutoff = now - retention_days * 86400
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            db.execute("DELETE FROM audit WHERE created < ?", (cutoff,))
            db.execute("DELETE FROM deliveries WHERE state='sent' AND updated < ?", (cutoff,))
            db.execute(
                """UPDATE articles SET title=NULL, summary=NULL, link=NULL, published=NULL
                   WHERE discovered < ? AND title IS NOT NULL AND NOT EXISTS
                   (SELECT 1 FROM deliveries d WHERE d.article_id=articles.id
                    AND d.state IN ('pending','sending','failed'))""", (cutoff,)
            )

    def backup(self, destination: Path) -> None:
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()

