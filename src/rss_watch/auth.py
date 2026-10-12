from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

from .config import file_lock


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 256:
        raise ValueError("密码长度必须为 12 到 256 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if len(password) > 256:
        return False
    try:
        method, salt, expected = stored.split("$")
        if method != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


class Auth:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.directory = service.config.data_dir
        with file_lock(self.directory / "auth.lock"):
            path = self.directory / "session-key"
            if not path.exists():
                with path.open("xb") as output:
                    path.chmod(0o600)
                    output.write(secrets.token_bytes(32))
            self.key = path.read_bytes()
            if not self.admin() and not self.bootstrap_path.exists():
                with self.bootstrap_path.open("x") as output:
                    self.bootstrap_path.chmod(0o600)
                    output.write(secrets.token_urlsafe(32))

    @property
    def bootstrap_path(self):
        return self.directory / "bootstrap-token"

    def admin(self):
        rows = self.db.rows("SELECT * FROM admins WHERE id=1")
        return rows[0] if rows else None

    def csrf(self, token: str):
        return hmac.new(self.key, token.encode(), hashlib.sha256).hexdigest()

    def valid_csrf(self, token: str, submitted: str):
        return bool(token) and hmac.compare_digest(self.csrf(token).encode(), submitted.encode())

    def setup(self, token: str, username: str, password: str):
        username = username.strip()
        if not username or len(username) > 64:
            raise ValueError("管理员名称应为 1 到 64 个字符")
        with file_lock(self.directory / "auth.lock"):
            if self.admin():
                raise ValueError("管理员已创建，请登录")
            if not self.bootstrap_path.exists() or not hmac.compare_digest(
                self.bootstrap_path.read_bytes(), token.encode()
            ):
                raise ValueError("初始化口令无效")
            hashed = hash_password(password)
            with self.db.transaction() as db:
                db.execute("INSERT INTO admins VALUES (1,?,?)", (username, hashed))
            self.bootstrap_path.unlink(missing_ok=True)
        self.audit(username, "创建管理员")

    def throttle(self, address: str):
        key = "login:" + hashlib.sha256(address.encode()).hexdigest()
        now = time.time()
        with self.db.transaction() as db:
            row = db.execute("SELECT value FROM runtime WHERE key=?", (key,)).fetchone()
            attempts = json.loads(row[0]) if row else {"since": now, "count": 0}
            if now - attempts["since"] >= 900:
                attempts = {"since": now, "count": 0}
            if attempts["count"] >= 10:
                raise ValueError("登录尝试过多，请在 15 分钟后重试")
            attempts["count"] += 1
            db.execute("INSERT OR REPLACE INTO runtime VALUES (?,?)", (key, json.dumps(attempts)))
        return key

    def login(self, username: str, password: str, address: str) -> str:
        rate_key = self.throttle(address)
        admin = self.admin()
        if not admin or not hmac.compare_digest(admin["username"].encode(), username.encode()) or not verify_password(
            password, admin["password_hash"]
        ):
            raise ValueError("用户名或密码错误")
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.transaction() as db:
            db.execute("DELETE FROM runtime WHERE key=?", (rate_key,))
            db.execute("DELETE FROM sessions WHERE expires<?", (now,))
            db.execute("INSERT INTO sessions VALUES (?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), now + 43200))
        self.audit(admin["username"], "登录后台")
        return token

    def authenticated(self, token: str) -> bool:
        if not token or len(token) > 128:
            return False
        return bool(self.db.rows("SELECT 1 FROM sessions WHERE token_hash=? AND expires>?",
                                 (hashlib.sha256(token.encode()).hexdigest(), time.time())))

    def logout(self, token: str):
        with self.db.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?",
                       (hashlib.sha256(token.encode()).hexdigest(),))

    def change_password(self, current: str, password: str):
        admin = self.admin()
        if not admin or not verify_password(current, admin["password_hash"]):
            raise ValueError("当前密码错误")
        hashed = hash_password(password)
        with self.db.transaction() as db:
            db.execute("UPDATE admins SET password_hash=? WHERE id=1", (hashed,))
            db.execute("DELETE FROM sessions")
        self.audit(admin["username"], "修改管理员密码")

    def audit(self, actor: str, action: str):
        with self.db.transaction() as db:
            db.execute("INSERT INTO audit(created,actor,action) VALUES (?,?,?)",
                       (time.time(), actor, action))
