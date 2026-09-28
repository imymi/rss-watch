"""Install a verifiable global command without replacing unrelated executables."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import stat
import tempfile
from pathlib import Path

MARKER = b"# rss-watch-managed-entry-v1 "
# Exact fingerprint of the convenience command installed during the 2026-09-16 session.
LEGACY_SHA256 = "e5aa085d67d32724844939db66faeef962c8549517da23d357ed3b0e736212cb"


def read_entry(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise ValueError("入口属主或权限不可验证")
        return stream.read()


def owned(path: Path, target: Path, config: Path) -> bool:
    try:
        raw = read_entry(path)
        if (str(target) == "/opt/rss-watch" and str(config) == "/etc/rss-watch/config.toml"
                and hashlib.sha256(raw).hexdigest() == LEGACY_SHA256):
            return True
        first, marker, body = raw.split(b"\n", 2)
        if first != b"#!/usr/bin/env bash" or not marker.startswith(MARKER):
            return False
        value = json.loads(marker[len(MARKER):])
        return (value.get("target") == str(target) and value.get("config") == str(config)
                and value.get("sha256") == hashlib.sha256(body).hexdigest())
    except (OSError, ValueError, AttributeError):
        return False


def check(path: Path, target: Path, config: Path):
    if not path.is_absolute() or path.name != "rss-watch" or ".." in path.parts:
        raise ValueError("全局入口必须是绝对路径且文件名为 rss-watch")
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError("全局入口或其父目录经过符号链接，拒绝替换")
    if path.exists() and not owned(path, target, config):
        raise ValueError(f"已有命令不属于此安装或已被修改，保留并中止：{path}")


def install(path: Path, target: Path, config: Path, user: str):
    check(path, target, config)
    template = Path(__file__).with_name("rss-watch-entry.sh").read_text()
    values = {"CLI": str(target / "current/.venv/bin/rss-watch"), "CONFIG": str(config),
              "CHECK": str(target / "current/deploy/check.sh"), "USER": user}
    for key, value in values.items():
        template = template.replace("@@" + key + "@@", shlex.quote(value))
    first, body = template.split("\n", 1)
    metadata = {"target": str(target), "config": str(config),
                "sha256": hashlib.sha256(body.encode()).hexdigest()}
    content = first + "\n" + MARKER.decode() + json.dumps(metadata) + "\n" + body
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".rss-watch-entry-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o755)
            os.fsync(stream.fileno())
        check(path, target, config)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("user")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            check(args.path, args.target, args.config)
        else:
            install(args.path, args.target, args.config, args.user)
    except (OSError, ValueError) as error:
        parser.exit(1, f"全局命令配置失败：{error}\n")
