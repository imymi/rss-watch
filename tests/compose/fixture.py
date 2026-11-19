"""Feed/Feishu fixture and acceptance client, run only on the internal network."""

import base64
import hashlib
import hmac
import io
import json
import re
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import zipfile
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

WEB = "http://web:8080"
MOCK = "http://127.0.0.1:8081"
FEED = "http://mock:8081/feed.xml"
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/rss-watch-rehearsal"
SECRET = "rehearsal-only-signature"
PASSWORD = "temporary-rehearsal-password"
USER = "temporary-admin"
TITLES = [
    "REHEARSAL baseline",
    "REHEARSAL sent-by-python",
    "REHEARSAL queued-before-upgrade",
    "REHEARSAL queued-before-rollback",
]
CSRF = re.compile(r'name="csrf" value="([a-f0-9]{64})"')
BACKUP = re.compile(r'href="/operations/backups/(manual-[^/]+)/download"')


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode()


def run_mock():
    gate = threading.Lock()
    state = {"phase": 0, "ledger": []}

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, data=b"", kind="application/json", etag=None):
            self.send_response(status)
            if etag:
                self.send_header("ETag", etag)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            with gate:
                phase = state["phase"]
                ledger = list(state["ledger"])
            if self.path == "/ledger":
                self.reply(200, json_bytes(ledger))
            elif self.path == "/feed.xml":
                etag = f'"fixture-{phase}"'
                if self.headers.get("If-None-Match") == etag:
                    self.reply(304, etag=etag)
                    return
                entries = "".join(
                    f'<item><guid isPermaLink="false">fixture-{i}</guid><title>{title}</title></item>'
                    for i, title in enumerate(TITLES[:phase + 1])
                )
                xml = f'<rss version="2.0"><channel><title>Fixture</title>{entries}</channel></rss>'
                self.reply(200, xml.encode(), "application/rss+xml", etag)
            else:
                self.reply(404)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/advance":
                with gate:
                    assert payload["phase"] == state["phase"] + 1
                    state["phase"] = payload["phase"]
                self.reply(200, b"{}")
                return
            if self.path != "/open-apis/bot/v2/hook/rss-watch-rehearsal":
                self.reply(404)
                return
            text = payload["content"]["text"]
            event = re.search(r"^事件：(.+)$", text, re.M)
            key = (payload.get("timestamp", "") + "\n" + SECRET).encode()
            expected = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
            signed = bool(payload.get("timestamp")) and hmac.compare_digest(payload.get("sign", ""), expected)
            code = 0 if signed and event and "测试消息" not in text else 9499
            with gate:
                state["ledger"].append({"event": event[1] if event else "", "signed": signed, "code": code})
            self.reply(200, json_bytes({"code": code}))

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("0.0.0.0", 8081), Handler)
    https = ThreadingHTTPServer(("0.0.0.0", 443), Handler)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain("/rehearsal/server.pem", "/rehearsal/server.key")
    https.socket = ssl_context.wrap_socket(https.socket, server_side=True)
    threading.Thread(target=https.serve_forever, daemon=True).start()
    http.serve_forever()


class Client:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.csrf = None

    def request(self, path, method="GET", data=None, host=WEB, form=False):
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            data = urlencode(data).encode() if form else json_bytes(data)
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        with self.opener.open(Request(host + path, headers=headers, method=method, data=data), timeout=10) as response:
            return response.read()

    def api(self, path, method="GET", data=None):
        return json.loads(self.request("/api/" + path, method, data))

    def form(self, path, fields, read_token=False):
        csrf = CSRF.search(self.request(path).decode())[1] if read_token else self.csrf
        return self.request(path, "POST", {**fields, "csrf": csrf}, form=True)

    def login(self):
        self.form("/login", {"username": USER, "password": PASSWORD}, read_token=True)
        self.csrf = self.api("session/csrf")["csrf"]
        assert "待发送" in self.request("/").decode(), "dashboard page unavailable"

    def refresh(self, phase):
        self.request("/advance", "POST", {"phase": phase}, host=MOCK)
        self.api("sources/1", "PUT", {"name": "临时订阅", "interval": 60})

    def delivery(self, title, desired):
        def find():
            return next((d for d in self.api("history?state=" + desired)
                         if title in d["payload"]), None)
        return wait_for(title + " " + desired, find)

    def pause_delivery(self, paused):
        self.api("settings", "POST", {"delivery_paused": paused})
        def applied():
            status = self.api("status")
            return status["后台运行"] and not status["settings_pending"] and status["settings"]["delivery_paused"] is paused
        wait_for("worker applied delivery pause", applied)


def wait_for(label, condition):
    end = time.monotonic() + 25
    while time.monotonic() < end:
        result = condition()
        if result:
            return result
        time.sleep(0.15)
    raise AssertionError(f"timeout waiting for {label}")


def backup_state(client):
    assert client.api("health") == {"database_ok": True, "credential_errors": []}
    before = set(BACKUP.findall(client.request("/operations").decode()))
    page = client.form("/operations/backup", {}).decode()
    created = set(BACKUP.findall(page)) - before
    assert len(created) == 1
    name = created.pop()
    archive = client.request(f"/operations/backups/{name}/download")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped, tempfile.TemporaryDirectory() as tmp:
        files = set(zipped.namelist())
        credentials = sorted(p for p in files if p.startswith("credentials/"))
        assert len(credentials) == 1
        credential = zipped.read(credentials[0])
        assert json.loads(credential) == {"webhook": HOOK, "secret": SECRET}
        assert json.loads(zipped.read("manifest.json"))["schema_version"] == 2
        database = Path(tmp, "snapshot.sqlite3")
        database.write_bytes(zipped.read("rss-watch.sqlite3"))
        with sqlite3.connect(database) as db:
            assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert db.execute("PRAGMA user_version").fetchone() == (2,)
            fingerprints = [list(item) for item in db.execute(
                "SELECT source_id, fingerprint FROM articles ORDER BY source_id, fingerprint"
            )]
    return {
        "credentials": [credentials[0], hashlib.sha256(credential).hexdigest()],
        "fingerprints": fingerprints,
        "sources": [{key: source[key] for key in ("id", "name", "url", "interval", "enabled", "initialized", "deleted")}
                    for source in client.api("sources")],
        "rules": client.api("rules"),
        "bots": client.api("bots"),
        "deliveries": sorted(client.api("history?limit=1000"), key=lambda item: item["id"]),
    }


def ledger(client):
    entries = json.loads(client.request("/ledger", host=MOCK))
    assert all(row["signed"] for row in entries), "the simulated receiver rejected a signature"
    return entries


def assert_expected(client, expected):
    actual = backup_state(client)
    for field in expected:
        assert actual[field] == expected[field], f"{field} did not survive the image change"


def assert_delivered_once(client, event):
    accepted = [row for row in ledger(client) if row["event"] == event and row["code"] == 0]
    assert len(accepted) == 1, "delivery missing or duplicated"


def python_start(client, data):
    client.form("/setup", {"username": USER, "password": PASSWORD, "token": data["bootstrap"]}, read_token=True)
    client.login()
    client.api("bots", "POST", {"name": "模拟飞书", "webhook": HOOK, "secret": SECRET})
    client.api("rules", "POST", {"name": "临时规则", "includes": ["REHEARSAL"], "bot_id": 1})
    client.api("sources", "POST", {"name": "临时订阅", "url": FEED, "interval": 60})
    wait_for("feed baseline", lambda: client.api("sources")[0]["initialized"])
    assert not client.api("history"), "baseline queued a notification"
    client.refresh(1)
    sent = client.delivery(TITLES[1], "sent")
    failed = client.api("bots/1/test", "POST", {})["event_id"]
    assert wait_for("permanent failure", lambda: client.api("history?state=failed"))
    client.pause_delivery(True)
    client.refresh(2)
    pending = client.delivery(TITLES[2], "pending")
    assert pending["attempts"] == 0
    assert_delivered_once(client, sent["id"])
    state = backup_state(client)
    assert sorted(row["state"] for row in state["deliveries"]) == ["failed", "pending", "sent"]
    assert [row["event"] for row in ledger(client)] == [sent["id"], failed]
    return {"state": state, "python_sent": sent["id"], "failed": failed, "python_pending": pending["id"]}


def go_stage(client, data):
    client.login()
    assert client.api("status")["settings"]["delivery_paused"]
    assert_expected(client, data["state"])
    client.pause_delivery(False)
    assert client.delivery(TITLES[2], "sent")["id"] == data["python_pending"]
    assert_delivered_once(client, data["python_pending"])
    client.pause_delivery(True)
    client.refresh(3)
    pending = client.delivery(TITLES[3], "pending")
    assert pending["attempts"] == 0
    state = backup_state(client)
    assert sorted(row["state"] for row in state["deliveries"]) == ["failed", "pending", "sent", "sent"]
    assert len(state["fingerprints"]) == 4
    return {**data, "state": state, "go_pending": pending["id"]}


def python_return(client, data):
    client.login()
    assert client.api("status")["settings"]["delivery_paused"]
    assert_expected(client, data["state"])
    client.pause_delivery(False)
    assert client.delivery(TITLES[3], "sent")["id"] == data["go_pending"]
    # Serve the same four entries with a new validator so Python must deduplicate them.
    client.refresh(4)
    wait_for("Python refetch after rollback", lambda: client.api("sources")[0]["etag"] == '"fixture-4"')
    final = backup_state(client)
    assert sorted(row["state"] for row in final["deliveries"]) == ["failed", "sent", "sent", "sent"]
    for key in ("credentials", "sources", "rules", "bots", "fingerprints"):
        assert final[key] == data["state"][key]
    assert [row["event"] for row in ledger(client)] == [
        data["python_sent"], data["failed"], data["python_pending"], data["go_pending"],
    ]
    return {"sources": 1, "active_credentials": 1, "fingerprints": 4, "sent": 3, "failed": 1}


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "serve":
        run_mock()
    else:
        stage = {"python-start": python_start, "go": go_stage, "python-return": python_return}[action]
        print(json.dumps(stage(Client(), json.load(sys.stdin))))
