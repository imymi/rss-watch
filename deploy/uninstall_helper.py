"""Conservative stdlib-only uninstaller; loaded before its own files are removed."""

from __future__ import annotations

import argparse
import configparser
import errno
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .entry_helper import owned as entry_owned
else:
    from entry_helper import owned as entry_owned

PUBLIC = set(
    "/ /bin /boot /dev /etc /home /lib /lib64 /media /mnt /opt /proc /root /run "
    "/sbin /srv /sys /tmp /usr /usr/bin /usr/lib /usr/local /usr/local/bin "
    "/usr/local/lib /var /var/lib /var/log /var/backups /var/cache /var/tmp "
    "/etc/systemd /etc/systemd/system /run/systemd /run/systemd/system "
    "/private /private/etc /private/var /private/tmp /private/var/tmp "
    "/Users /Applications /Library /System".split()
)
PUBLIC.update(
    str(Path(p.pw_dir))
    for p in pwd.getpwall()
    if p.pw_dir.startswith("/") and p.pw_name != "rss-watch"
)
FORBIDDEN_FILES = {
    "/etc/passwd",
    "/etc/shadow",
    "/etc/group",
    "/etc/sudoers",
    "/private/etc/passwd",
    "/private/etc/group",
    "/private/etc/sudoers",
}


class Refused(ValueError):
    pass


def safe_path(raw: str, *, label: str) -> Path:
    if not isinstance(raw, str) or not raw or not raw.startswith("/"):
        raise Refused(f"{label} 必须是非空绝对路径")
    path = Path(raw)
    if ".." in path.parts or any(ord(c) < 32 for c in raw):
        raise Refused(f"{label} 含有不安全的路径片段")
    for component in reversed([path, *path.parents]):
        if component.is_symlink():
            alias = (
                platform.system() == "Darwin"
                and str(component) in {"/tmp", "/var", "/etc"}
                and str(component.resolve()) == "/private" + str(component)
            )
            if not alias:
                raise Refused(f"{label} 经过符号链接，拒绝操作：{component}")
    path = path.resolve(strict=False)
    if str(path) in PUBLIC or str(path) in FORBIDDEN_FILES or len(path.parts) < 3:
        raise Refused(f"拒绝操作系统公共目录或关键路径：{path}")
    return path


def read_bytes(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Refused(f"不是普通文件：{path}")
        return stream.read()


def read_receipt(path: Path, config: Path, target: Path, test: bool) -> dict:
    if not path.exists():
        return {}
    try:
        info = path.lstat()
        if info.st_uid != (os.geteuid() if test else 0) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError()
        value = json.loads(read_bytes(path))
        if (
            value.get("project") != "rss-watch"
            or value.get("version") != 1
            or value.get("target_dir") != str(target)
            or value.get("config_file") != str(config)
        ):
            raise ValueError()
        return value
    except (OSError, ValueError, AttributeError):
        print("保留服务账户：安装归属记录不可验证，不将其作为删除依据。")
        return {}


def is_project(path: Path) -> bool:
    try:
        project = tomllib.loads(read_bytes(path / "pyproject.toml").decode())
        return (
            project.get("project", {}).get("name") == "rss-watch"
            and (path / "src/rss_watch").is_dir()
            and (path / "deploy/rss-watch.service").is_file()
        )
    except (OSError, ValueError):
        return False


def target_owned(target: Path) -> bool:
    if not target.exists() or (target.is_dir() and not any(target.iterdir())):
        return True
    if is_project(target):
        return True
    current = target / "current"
    if current.is_symlink():
        resolved = current.resolve()
        return resolved.is_relative_to(target / "releases") and is_project(resolved)
    return False


def credential_directory(path: Path) -> bool:
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    for item in path.iterdir():
        if item.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}\.json", item.name):
            return False
        try:
            value = json.loads(read_bytes(item))
            url = urlsplit(value["webhook"])
            if (
                url.scheme != "https"
                or url.hostname != "open.feishu.cn"
                or not url.path.startswith("/open-apis/bot/v2/hook/")
                or not isinstance(value.get("secret"), str)
            ):
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
    return True


def sqlite_header(path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                return False
        # immutable is intentionally conservative: no WAL/SHM creation or checkpoint.
        # If the schema exists only in WAL, require an offline checkpoint before purge.
        with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            return {"sources", "bots", "rules", "articles", "deliveries", "runtime"} <= tables
    except (OSError, sqlite3.Error):
        return False


def data_directory(path: Path) -> bool:
    # Immutable SQLite reads never create/checkpoint WAL or SHM files.
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    items = list(path.iterdir())
    if not items:
        return True
    allowed = {
        "rss-watch.sqlite3",
        "rss-watch.sqlite3-wal",
        "rss-watch.sqlite3-shm",
        "admin.lock",
        "daemon.lock",
        "data.lock",
        "backups",
    }
    for item in items:
        if item.is_symlink():
            return False
        if item.name not in allowed:
            if not re.fullmatch(r"migration-v1-[0-9]+\.sqlite3", item.name) or not sqlite_header(item):
                return False
    if not sqlite_header(path / "rss-watch.sqlite3"):
        return False
    backups = path / "backups"
    if backups.exists():
        if not backups.is_dir():
            return False
        for backup in backups.iterdir():
            if backup.is_symlink() or not backup.is_dir():
                return False
            if set(p.name for p in backup.iterdir()) - {
                "rss-watch.sqlite3",
                "manifest.json",
                "credentials",
            }:
                return False
            try:
                manifest = json.loads(read_bytes(backup / "manifest.json"))
                if (
                    manifest.get("schema_version") not in (1, 2)
                    or not sqlite_header(backup / "rss-watch.sqlite3")
                    or not credential_directory(backup / "credentials")
                ):
                    return False
            except (OSError, ValueError, AttributeError):
                return False
    return True


@dataclass
class Plan:
    target: Path
    config: Path
    unit: Path
    receipt_path: Path
    data: Path | None
    credentials: Path | None
    purge: bool
    receipt: dict = field(default_factory=dict)
    skipped: list[Path] = field(default_factory=list)
    entry: Path | None = None

    @property
    def keep(self) -> list[Path]:
        if not self.purge or self.skipped:
            return [p for p in [self.config, self.receipt_path, self.data, self.credentials] if p]
        return []


def unit_owned(plan: Plan) -> bool:
    if not plan.unit.exists():
        return True
    raw = read_bytes(plan.unit)
    if plan.receipt.get("unit_sha256") == hashlib.sha256(raw).hexdigest():
        return True
    try:
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read_string(raw.decode())
        cmd = shlex.split(unit["Service"]["ExecStart"])
        starts = {
            str(plan.target / ".venv/bin/rss-watch"),
            str(plan.target / "current/.venv/bin/rss-watch"),
        }
        return (
            cmd[0] in starts
            and cmd[1:] == ["--config", str(plan.config), "run"]
            and unit["Service"]["User"] == "rss-watch"
        )
    except (ValueError, KeyError, IndexError, configparser.Error):
        return False


def build_plan(args, env: dict) -> Plan:
    target = safe_path(args.target, label="安装目录")
    config = safe_path(args.config, label="配置文件")
    unit = safe_path(
        env.get("RSS_WATCH_SYSTEMD_UNIT", "/etc/systemd/system/rss-watch.service"), label="服务单元"
    )
    if unit.name != "rss-watch.service":
        raise Refused("服务单元必须是 rss-watch.service")
    if env.get("RSS_WATCH_SERVICE_NAME", "rss-watch") != "rss-watch":
        raise Refused("本卸载器只管理 rss-watch 服务")
    receipt_path = config.parent / "install-receipt.json"
    receipt = read_receipt(receipt_path, config, target, env.get("RSS_WATCH_TEST_MOCK_ENV") == "1")
    data = credentials = None
    if config.exists():
        try:
            values = tomllib.loads(read_bytes(config).decode())
            if set(values) != {"data_dir", "credentials_dir"}:
                raise ValueError()
            data = safe_path(values["data_dir"], label="数据目录")
            credentials = safe_path(values["credentials_dir"], label="凭据目录")
        except (ValueError, TypeError, OSError) as error:
            raise Refused("配置无效或路径不安全，卸载已中止；不会回退到默认路径删除") from error
    elif target.exists() and any(target.iterdir()):
        raise Refused("配置缺失，无法确定需要保留的数据路径；卸载已中止")
    preserved = [p for p in (config, receipt_path, data, credentials) if p]
    if any(target == p or target.is_relative_to(p) for p in (data, credentials) if p):
        raise Refused("安装目录与数据或凭据目录重叠，无法安全区分代码与业务数据")
    if not target_owned(target) and not only_preserved(target, preserved):
        raise Refused("安装目录无法确认为 rss-watch 项目，拒绝递归删除")
    plan = Plan(target, config, unit, receipt_path, data, credentials, args.purge, receipt)
    entry_path = "/usr/local/bin/rss-watch"
    if env.get("RSS_WATCH_TEST_MOCK_ENV") == "1":
        entry_path = env.get("RSS_WATCH_ENTRY_PATH", str(target.parent / "bin/rss-watch"))
    try:
        candidate = safe_path(entry_path, label="全局命令")
        if candidate.exists():
            if entry_owned(candidate, target, config):
                plan.entry = candidate
            else:
                print(f"保留无法验证归属或已修改的全局命令：{candidate}")
    except Refused:
        print("保留经过符号链接或路径不安全的全局命令")
    if not unit_owned(plan):
        raise Refused("服务单元不属于此 rss-watch 安装，拒绝停止或删除")
    if args.purge:
        for path, predicate in ((data, data_directory), (credentials, credential_directory)):
            if path and not predicate(path):
                plan.skipped.append(path)
    return plan


def only_preserved(path: Path, keep: list[Path]) -> bool:
    """Recognize the remainder of a previous default uninstall without deleting extras."""
    if path in keep:
        return True
    if path.is_symlink() or not path.is_dir() or not any(p.is_relative_to(path) for p in keep):
        return False
    return all(only_preserved(child, keep) for child in path.iterdir())


@contextmanager
def parent_fd(path: Path):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parent.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def remove(path: Path, keep: list[Path] = ()) -> None:
    if any(path == p or path.is_relative_to(p) for p in keep):
        return
    try:
        with parent_fd(path) as fd:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if any(p.is_relative_to(path) for p in keep):
                    for child in path.iterdir():
                        remove(child, keep)
                    try:
                        os.rmdir(path.name, dir_fd=fd)
                    except OSError as error:
                        if error.errno != errno.ENOTEMPTY:
                            raise
                else:
                    if not shutil.rmtree.avoids_symlink_attacks:
                        raise Refused("此 Python 不支持安全的目录删除，已中止")
                    shutil.rmtree(path.name, dir_fd=fd)
            else:
                os.unlink(path.name, dir_fd=fd)
    except FileNotFoundError:
        pass


def command(binary: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([binary, *args], text=True, capture_output=True, timeout=60)


def service_state(binary: str) -> dict:
    result = command(
        binary,
        "show",
        "rss-watch.service",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=FragmentPath",
    )
    if result.returncode != 0:
        raise Refused("无法查询 systemd 状态，卸载已中止")
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if state.get("LoadState") not in {"loaded", "not-found", "masked"} or not state.get(
        "ActiveState"
    ):
        raise Refused("systemd 返回未知状态，卸载已中止")
    return state


def stop_service(plan: Plan, binary: str) -> None:
    state = service_state(binary)
    if state["LoadState"] != "not-found":
        if (
            state.get("FragmentPath") != str(plan.unit)
            or not plan.unit.exists()
            or not unit_owned(plan)
        ):
            raise Refused("已加载的服务来源无法确认，卸载已中止")
        if command(binary, "stop", "rss-watch.service").returncode != 0:
            raise Refused("无法停止服务，卸载已安全中止，尚未删除任何文件")
        state = service_state(binary)
    if state["ActiveState"] not in {"inactive", "failed"}:
        raise Refused("停止后服务仍在运行或转换状态，卸载已中止")
    if plan.unit.exists() or state["LoadState"] != "not-found":
        if command(binary, "disable", "rss-watch.service").returncode != 0:
            raise Refused("禁用服务失败，尚未删除项目文件")
    remove(plan.unit)
    if command(binary, "daemon-reload").returncode != 0:
        raise Refused("daemon-reload 失败，已保留项目文件，请排查后重试")


def removable_accounts(plan: Plan) -> list[str]:
    if not plan.purge or plan.skipped:
        return []
    user = plan.receipt.get("user", {})
    group = plan.receipt.get("group", {})
    try:
        actual = pwd.getpwnam("rss-watch")
    except KeyError:
        actual = None
    try:
        actual_group = grp.getgrnam("rss-watch")
    except KeyError:
        actual_group = None
    actions = []
    if (
        user.get("created") is True
        and actual
        and actual.pw_uid > 0
        and user.get("uid") == actual.pw_uid
        and user.get("gid") == actual.pw_gid
        and user.get("home") == actual.pw_dir
        and user.get("shell") == actual.pw_shell
        and Path(actual.pw_shell).name in {"nologin", "false"}
    ):
        actions.append("userdel")
    if (
        group.get("created") is True
        and actual_group
        and actual_group.gr_gid > 0
        and group.get("gid") == actual_group.gr_gid
        and not actual_group.gr_mem
        and not any(
            p.pw_gid == actual_group.gr_gid
            and not (p.pw_name == "rss-watch" and "userdel" in actions)
            for p in pwd.getpwall()
        )
    ):
        actions.append("groupdel")
    return actions


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="卸载 rss-watch；默认保留配置、凭据、数据库、备份和账户"
    )
    parser.add_argument(
        "--config", default=os.environ.get("RSS_WATCH_CONFIG", "/etc/rss-watch/config.toml")
    )
    parser.add_argument(
        "--target", default=os.environ.get("RSS_WATCH_TARGET_DIR", "/opt/rss-watch")
    )
    parser.add_argument("--purge", action="store_true", help="额外清理可确认归属的数据及账户")
    parser.add_argument("--yes", "-y", action="store_true", help="仅与 --purge 一起用于非交互确认")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不修改文件、账户或服务")
    args = parser.parse_args(argv)
    if args.yes and not args.purge:
        parser.error("--yes 必须与 --purge 一起使用")
    env = dict(os.environ)
    test = env.get("RSS_WATCH_TEST_MOCK_ENV") == "1"
    if not args.dry_run and not test and (os.geteuid() != 0 or platform.system() != "Linux"):
        raise Refused("实际卸载需要 Linux 和 root 权限")
    plan = build_plan(args, env)
    accounts = removable_accounts(plan)
    print("[DRY-RUN 预览模式]" if args.dry_run else "RSS Watch 卸载计划")
    print(f"停止并禁用 rss-watch.service；删除单元 {plan.unit}；daemon-reload")
    print(f"删除项目代码、虚拟环境及历史版本：{plan.target}")
    if plan.entry:
        print(f"删除本项目创建的全局命令：{plan.entry}")
    if args.purge and not plan.skipped:
        for path in (plan.config, plan.data, plan.credentials, plan.receipt_path):
            print(f"彻底清理：{path}" if path else "配置已不存在，不猜测自定义数据路径")
    else:
        for path in plan.keep:
            print(f"保留：{path}")
    if plan.data:
        verb = "彻底清理" if args.purge and not plan.skipped else "保留"
        print(f"{verb}备份目录：{plan.data / 'backups'}")
    for path in plan.skipped:
        print(f"无法确认目录专属归属，停止清理并保留配置以便人工检查：{path}")
    print(
        "将删除经安装器记录并核验的账户/组：" + ", ".join(accounts)
        if accounts
        else "保留服务账户及用户组：默认模式、创建记录不可验证、身份变化或组被共用"
    )
    if args.dry_run:
        print("预览结束，未进行任何修改。")
        return 1 if plan.skipped else 0
    if args.purge and not args.yes:
        if not sys.stdin.isatty():
            raise Refused("非交互彻底卸载必须显式传入 --purge --yes")
        if input("输入 rss-watch 确认删除以上项目：").strip() != "rss-watch":
            print("已取消卸载操作，未作任何系统修改。")
            return 1
    stop_service(plan, env.get("RSS_WATCH_SYSTEMCTL", "systemctl"))
    if plan.entry:
        if not entry_owned(plan.entry, plan.target, plan.config):
            raise Refused("全局命令在预检后发生变化，保留文件并中止")
        remove(plan.entry)
    os.chdir("/")
    protection = plan.keep or ([plan.receipt_path] if plan.receipt_path.exists() else [])
    remove(plan.target, protection)
    if args.purge and not plan.skipped:
        for path in (plan.credentials, plan.data, plan.config):
            if path:
                remove(path, protection)
        for action in accounts:
            if action == "groupdel":
                try:
                    grp.getgrnam("rss-watch")
                except KeyError:
                    # Some Linux distributions remove the private group with userdel.
                    continue
            if test:
                print(f"测试模式：模拟 {action} rss-watch，不操作真实账户")
            elif command(action, "rss-watch").returncode != 0:
                raise Refused(f"{action} 失败；保留安装记录，请排查后重试")
        remove(plan.receipt_path)
        remove(plan.target)
        if str(plan.config.parent) not in PUBLIC:
            try:
                plan.config.parent.rmdir()
            except OSError as error:
                if error.errno not in {errno.ENOENT, errno.ENOTEMPTY}:
                    raise
    if plan.skipped:
        print("程序已卸载；数据清理未完成，相关目录和配置已保留。")
        return 1
    print(
        "已彻底卸载并清理可确认归属的文件。" if args.purge else "默认卸载完成（业务数据已保留）。"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Refused, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"卸载未完成：{error}", file=sys.stderr)
        sys.exit(1)
