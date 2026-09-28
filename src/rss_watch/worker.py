from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import signal
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .config import Config, file_lock, read_credential
from .db import Database
from .domain import match, message, parse_feed

log = logging.getLogger("rss_watch")
MAX_BODY = 5 * 1024 * 1024
REQUEST_DEADLINE = 30
RETRY_DELAYS = (30, 120, 600, 1800)


def signature(timestamp: str, secret: str) -> str:
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def retry_after(value: str | None, now: float) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0


class DeliveryError(Exception):
    def __init__(self, safe_message: str, transient: bool, delay: float = 0):
        super().__init__(safe_message)
        self.transient = transient
        self.delay = delay


async def send_feishu(client: httpx.AsyncClient, credential: dict, text: str):
    payload = {"msg_type": "text", "content": {"text": text}}
    if credential["secret"]:
        timestamp = str(int(time.time()))
        payload.update(timestamp=timestamp, sign=signature(timestamp, credential["secret"]))
    try:
        async with asyncio.timeout(REQUEST_DEADLINE):
            # Do not follow webhook redirects or log response text (may echo secrets).
            async with client.stream(
                "POST", credential["webhook"], json=payload, follow_redirects=False
            ) as response:
                delay = retry_after(response.headers.get("Retry-After"), time.time())
                if response.status_code == 429 or response.status_code >= 500:
                    raise DeliveryError(f"飞书 HTTP {response.status_code}", True, delay)
                if not 200 <= response.status_code < 300:
                    raise DeliveryError(f"飞书 HTTP {response.status_code}", False)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 64 * 1024:
                        raise DeliveryError("飞书响应体过大", True)
                try:
                    result = json.loads(body)
                    code = result.get("code", result.get("StatusCode"))
                except (ValueError, AttributeError):
                    raise DeliveryError("飞书响应不是有效 JSON 对象", True) from None
                if type(code) is not int:
                    raise DeliveryError("飞书响应缺少有效业务状态码", True)
                if code == 0:
                    return
                # Rate limits and internal errors only; 9499 is a bad request.
                if code in {11232, 99991400, 5000, 99991500}:
                    raise DeliveryError(f"飞书业务错误 {code}", True, delay)
                raise DeliveryError(f"飞书业务错误 {code}（请检查机器人配置）", False)
    except (httpx.HTTPError, TimeoutError):
        raise DeliveryError("飞书网络连接失败或超时", True) from None


class Worker:
    def __init__(self, config: Config, client: httpx.AsyncClient):
        self.config = config
        self.db = Database(config.database)
        self.client = client
        self.stopping = asyncio.Event()

    async def fetch(self, source: dict):
        try:
            headers = {"User-Agent": "rss-watch/0.1 (+RSS keyword monitor)",
                       "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"}
            if source["initialized"]:
                if source["etag"]:
                    headers["If-None-Match"] = source["etag"]
                if source["modified"]:
                    headers["If-Modified-Since"] = source["modified"]
            async with asyncio.timeout(REQUEST_DEADLINE):
                async with self.client.stream(
                    "GET", source["url"], headers=headers, follow_redirects=True
                ) as response:
                    if response.status_code == 304:
                        if not source["initialized"]:
                            raise ValueError("首次抓取不能使用 304 建立基线")
                        self.commit_feed(source, None, response.headers, time.time())
                        return
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BODY:
                            raise ValueError("订阅响应超过 5 MiB")
                    articles = await asyncio.to_thread(parse_feed, bytes(body), str(response.url))
                    self.commit_feed(source, articles, response.headers, time.time())
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            if isinstance(error, httpx.HTTPStatusError):
                reason = f"抓取 HTTP {error.response.status_code}"
            elif isinstance(error, (httpx.HTTPError, TimeoutError)):
                reason = "抓取网络连接失败或超时"
            else:
                reason = "订阅解析失败或响应超限"
            self.fail_source(source, reason)

    def commit_feed(self, source: dict, articles: list[dict] | None, headers, now: float):
        with self.db.transaction() as db:
            current = db.execute(
                "SELECT * FROM sources WHERE id=? AND revision=? AND deleted=0 AND enabled=1",
                (source["id"], source["revision"]),
            ).fetchone()
            # Discard an in-flight result if CLI disabled, deleted or edited the source.
            if current is None:
                return
            rules = [dict(row) for row in db.execute(
                "SELECT r.* FROM rules r JOIN bots b ON b.id=r.bot_id "
                "WHERE r.enabled=1 AND b.deleted=0"
            )]
            for article in articles or []:
                inserted = db.execute(
                    """INSERT OR IGNORE INTO articles
                       (source_id,fingerprint,discovered,title,summary,link,published)
                       VALUES (?,?,?,?,?,?,?)""",
                    (source["id"], article["fingerprint"], now, article["title"],
                     article["summary"], article["link"], article["published"]),
                )
                if not inserted.rowcount or not current["initialized"]:
                    continue
                grouped: dict[int, list] = {}
                for rule in rules:
                    scope = json.loads(rule["source_ids"])
                    if scope and source["id"] not in scope:
                        continue
                    hits = match(rule, article["title"], article["summary"])
                    if hits:
                        grouped.setdefault(rule["bot_id"], []).append({
                            "id": rule["id"], "name": rule["name"], "keywords": hits,
                        })
                for bot_id, matches in grouped.items():
                    event = str(uuid.uuid4())
                    db.execute(
                        """INSERT INTO deliveries
                           (id,article_id,bot_id,payload,matches,created,updated)
                           VALUES (?,?,?,?,?,?,?)""",
                        (event, inserted.lastrowid, bot_id,
                         message(event, current["name"], article, matches),
                         json.dumps(matches, ensure_ascii=False), now, now),
                    )
            # Validators and articles commit together, so rollback cannot cause lost items.
            etag = headers.get("ETag", current["etag"] if articles is None else None)
            modified = headers.get("Last-Modified", current["modified"] if articles is None else None)
            db.execute(
                """UPDATE sources SET initialized=1,etag=?,modified=?,last_success=?,
                   next_fetch=?,failures=0,last_error=NULL WHERE id=?""",
                (etag, modified, now, now + current["interval"], source["id"]),
            )

    def fail_source(self, source: dict, reason: str):
        now = time.time()
        with self.db.transaction() as db:
            db.execute(
                """UPDATE sources SET failures=failures+1,last_error=?,next_fetch=?
                   WHERE id=? AND revision=? AND deleted=0 AND enabled=1""",
                (reason, now + min(3600, source["interval"] * 2 ** min(source["failures"], 10)),
                 source["id"], source["revision"]),
            )
        log.warning("源 %s：%s", source["id"], reason)

    def recover(self):
        with self.db.transaction() as db:
            db.execute(
                """UPDATE deliveries SET state=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,
                   last_error='上次发送中断，可能已被接收',updated=? WHERE state='sending'""",
                (time.time(),),
            )

    async def deliver_one(self) -> bool:
        now = time.time()
        with self.db.transaction() as db:
            row = db.execute(
                """SELECT d.*,b.credential_ref FROM deliveries d JOIN bots b ON b.id=d.bot_id
                   WHERE d.state='pending' AND d.next_attempt<=? AND b.next_send<=?
                   AND b.deleted=0 ORDER BY d.next_attempt,d.created LIMIT 1""", (now, now)
            ).fetchone()
            if row is None:
                return False
            delivery = dict(row)
            attempt = delivery["attempts"] + 1
            db.execute(
                "UPDATE deliveries SET state='sending',attempts=?,updated=?,next_attempt=? WHERE id=?",
                (attempt, now, now + RETRY_DELAYS[min(attempt - 1, 3)], delivery["id"]),
            )
            db.execute("UPDATE bots SET next_send=? WHERE id=?", (now + 1, delivery["bot_id"]))
        try:
            try:
                credential = read_credential(self.config, delivery["credential_ref"])
            except (OSError, ValueError, KeyError, TypeError):
                raise DeliveryError("凭据不可读、权限不是 0600 或格式无效", False) from None
            await send_feishu(self.client, credential, delivery["payload"])
        except DeliveryError as error:
            now = time.time()
            retry = error.transient and attempt < 5
            delay = max(RETRY_DELAYS[min(attempt - 1, 3)], error.delay)
            with self.db.transaction() as db:
                db.execute(
                    """UPDATE deliveries SET state=?,next_attempt=?,updated=?,last_error=?
                       WHERE id=?""",
                    ("pending" if retry else "failed", now + delay, now, str(error), delivery["id"]),
                )
                # Honor remote rate limiting across all queued messages for this bot.
                db.execute("UPDATE bots SET next_send=max(next_send,?) WHERE id=?",
                           (now + max(1, error.delay), delivery["bot_id"]))
            log.warning("推送 %s：%s", delivery["id"], error)
        else:
            with self.db.transaction() as db:
                db.execute(
                    "UPDATE deliveries SET state='sent',updated=?,last_error=NULL WHERE id=?",
                    (time.time(), delivery["id"]),
                )
                db.execute("UPDATE bots SET next_send=max(next_send,?) WHERE id=?",
                           (time.time() + 1, delivery["bot_id"]))
        return True

    async def delivery_loop(self):
        while not self.stopping.is_set():
            if not await self.deliver_one():
                await asyncio.sleep(0.25)

    async def run(self):
        self.recover()
        active: dict[int, asyncio.Task] = {}
        sender = asyncio.create_task(self.delivery_loop())
        next_cleanup = 0.0
        try:
            while not self.stopping.is_set():
                if sender.done():
                    sender.result()
                for id, task in list(active.items()):
                    if task.done():
                        task.result()
                        del active[id]
                now = time.time()
                self.db.heartbeat()
                if now >= next_cleanup:
                    self.db.cleanup(now)
                    next_cleanup = now + 86400
                for source in self.db.rows(
                    "SELECT * FROM sources WHERE enabled=1 AND deleted=0 AND next_fetch<=? "
                    "ORDER BY next_fetch,id", (now,)
                ):
                    if len(active) >= 5:
                        break
                    if source["id"] not in active:
                        active[source["id"]] = asyncio.create_task(self.fetch(source))
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=1)
                except TimeoutError:
                    pass
        finally:
            # An interrupted delivery remains 'sending' and is recovered on next startup.
            sender.cancel()
            for task in active.values():
                task.cancel()
            await asyncio.gather(sender, *active.values(), return_exceptions=True)
            self.db.heartbeat(False)


async def run_daemon(config: Config):
    with file_lock(config.data_dir / "daemon.lock", blocking=False):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=10), max_redirects=5,
            limits=httpx.Limits(max_connections=10),
        ) as client:
            worker = Worker(config, client)
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, worker.stopping.set)
            log.info("RSS Watch 启动：%s", datetime.now(timezone.utc).isoformat())
            try:
                await worker.run()
            finally:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(signum)
