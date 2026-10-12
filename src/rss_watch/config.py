from __future__ import annotations

import fcntl
import json
import os
import stat
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    data_dir: Path
    credentials_dir: Path

    @property
    def database(self) -> Path:
        return self.data_dir / "rss-watch.sqlite3"

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.credentials_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


def load_config(path: Path) -> Config:
    with path.open("rb") as f:
        data = tomllib.load(f)
    unknown = set(data) - {"data_dir", "credentials_dir"}
    if unknown:
        raise ValueError("配置包含未知字段")
    config = Config(Path(data["data_dir"]), Path(data["credentials_dir"]))
    if not config.data_dir.is_absolute() or not config.credentials_dir.is_absolute():
        raise ValueError("data_dir 和 credentials_dir 必须是绝对路径")
    config.prepare()
    return config


@contextmanager
def file_lock(path: Path, *, blocking: bool = True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ValueError("后台服务已运行，不能重复启动") from None
        yield
    finally:
        os.close(fd)


def validate_url(url: str, *, webhook: bool = False) -> str:
    if not isinstance(url, str) or any(char.isspace() for char in url):
        raise ValueError("URL 格式无效")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("URL 格式无效") from None
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("URL 必须使用 http 或 https")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("URL 不允许包含用户名、密码或片段")
    if webhook and (
        parsed.scheme != "https"
        or parsed.hostname != "open.feishu.cn"
        or port not in {None, 443}
        or not parsed.path.startswith("/open-apis/bot/v2/hook/")
        or not parsed.path.removeprefix("/open-apis/bot/v2/hook/")
        or parsed.query
    ):
        raise ValueError("请使用飞书群自定义机器人 HTTPS webhook")
    return url


def write_credential(config: Config, webhook: str, secret: str) -> str:
    validate_url(webhook, webhook=True)
    if not isinstance(secret, str):
        raise ValueError("签名密钥必须为字符串")
    ref = f"{uuid.uuid4().hex}.json"
    fd = os.open(config.credentials_dir / ref, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"webhook": webhook, "secret": secret}, f)
        f.flush()
        os.fsync(f.fileno())
    return ref


def credential_path(config: Config, ref: str) -> Path:
    if Path(ref).name != ref or not ref.endswith(".json"):
        raise ValueError("凭据引用无效")
    return config.credentials_dir / ref


@contextmanager
def open_credential(config: Config, ref: str):
    fd = os.open(credential_path(config, ref), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("凭据文件必须是权限为 0600 的普通文件")
        yield f


def read_credential(config: Config, ref: str) -> dict:
    with open_credential(config, ref) as f:
        credential = json.load(f)
    validate_url(credential["webhook"], webhook=True)
    if not isinstance(credential.get("secret"), str):
        raise ValueError("签名密钥格式无效")
    return credential
