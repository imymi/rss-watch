"""Record actual account creation, never infer ownership from an account name."""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import stat
import tempfile
from pathlib import Path


def record(target: Path, config: Path, unit: Path, user_created: bool, group_created: bool):
    target, config = target.resolve(), config.resolve()
    path = config.parent / "install-receipt.json"
    previous = {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600:
                previous = json.load(stream)
        if previous.get("project") != "rss-watch" or previous.get("config_file") != str(config):
            previous = {}
    except (OSError, ValueError, AttributeError):
        previous = {}
    user = group = {}
    try:
        p = pwd.getpwnam("rss-watch")
        old = previous.get("user", {})
        user = {
            "uid": p.pw_uid,
            "gid": p.pw_gid,
            "home": p.pw_dir,
            "shell": p.pw_shell,
            "created": user_created
            or (
                old.get("created") is True
                and old.get("uid") == p.pw_uid
                and old.get("gid") == p.pw_gid
                and old.get("home") == p.pw_dir
                and old.get("shell") == p.pw_shell
            ),
        }
    except KeyError:
        pass
    try:
        g = grp.getgrnam("rss-watch")
        old = previous.get("group", {})
        group = {
            "gid": g.gr_gid,
            "created": group_created or (old.get("created") is True and old.get("gid") == g.gr_gid),
        }
    except KeyError:
        pass
    value = {
        "project": "rss-watch",
        "version": 1,
        "target_dir": str(target),
        "config_file": str(config),
        "user": user,
        "group": group,
    }
    if unit.is_file():
        value["unit_sha256"] = hashlib.sha256(unit.read_bytes()).hexdigest()
    fd, name = tempfile.mkstemp(prefix=".install-receipt-", dir=config.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("unit", type=Path)
    parser.add_argument("--user-created", action="store_true")
    parser.add_argument("--group-created", action="store_true")
    args = parser.parse_args()
    record(args.target, args.config, args.unit, args.user_created, args.group_created)
