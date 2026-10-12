from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from .config import load_config, validate_url
from .service import Service
from .worker import run_daemon

app = typer.Typer(help="RSS 关键词监控与飞书推送", no_args_is_help=True, pretty_exceptions_enable=False)
source_app = typer.Typer(help="管理订阅源", no_args_is_help=True)
rule_app = typer.Typer(help="管理匹配规则", no_args_is_help=True)
bot_app = typer.Typer(help="管理飞书机器人", no_args_is_help=True)
delivery_app = typer.Typer(help="管理推送任务", no_args_is_help=True)
app.add_typer(source_app, name="source")
app.add_typer(rule_app, name="rule")
app.add_typer(bot_app, name="bot")
app.add_typer(delivery_app, name="delivery")
console = Console(markup=False, highlight=False)


@app.callback()
def root(ctx: typer.Context, config: Annotated[Path, typer.Option(
    "--config", envvar="RSS_WATCH_CONFIG", help="CLI 与后台使用同一个绝对路径配置文件"
)] = Path("/etc/rss-watch/config.toml")):
    os.umask(0o077)
    ctx.obj = {"path": config}


def service(ctx: typer.Context) -> Service:
    if "service" not in ctx.obj:
        ctx.obj["service"] = Service(load_config(ctx.obj["path"]))
    return ctx.obj["service"]


def show(rows: list[dict], columns: list[str] | None = None, as_json: bool = False):
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        console.print("暂无记录")
        return
    columns = columns or list(rows[0])
    table = Table()
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*(str(row.get(column, "")) for column in columns))
    console.print(table)


@app.command()
def init(ctx: typer.Context,
         data_dir: Path = Path("/var/lib/rss-watch"),
         credentials_dir: Path = Path("/etc/rss-watch/credentials")):
    """首次创建配置；不会覆盖现有文件。"""
    if not data_dir.is_absolute() or not credentials_dir.is_absolute():
        raise ValueError("目录必须是绝对路径")
    path = ctx.obj["path"]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x") as f:
        # JSON quoted strings are valid TOML basic strings for these filesystem paths.
        f.write(f"data_dir = {json.dumps(str(data_dir))}\n"
                f"credentials_dir = {json.dumps(str(credentials_dir))}\n")
    service(ctx)
    console.print(f"初始化完成：{path}")


@source_app.command("list")
def source_list(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")):
    show(service(ctx).source_list(),
         ["id", "name", "interval", "enabled", "initialized", "last_success", "last_error"], as_json)


@source_app.command("add")
def source_add(ctx: typer.Context, name: str, url: str, interval: int | None = None):
    console.print(f"订阅源 ID：{service(ctx).source_save(name, url, interval)}")


@source_app.command("update")
def source_update(ctx: typer.Context, id: int, name: str | None = None, interval: int | None = None):
    svc = service(ctx)
    rows = [r for r in svc.source_list() if r["id"] == id]
    if not rows:
        raise ValueError("订阅源不存在")
    row = rows[0]
    svc.source_save(name if name is not None else row["name"], row["url"],
                    interval if interval is not None else row["interval"], id)
    console.print("已更新")


@source_app.command("enable")
def source_enable(ctx: typer.Context, id: int):
    service(ctx).source_enable(id, True)


@source_app.command("disable")
def source_disable(ctx: typer.Context, id: int):
    service(ctx).source_enable(id, False)


@source_app.command("delete")
def source_delete(ctx: typer.Context, id: int):
    service(ctx).source_delete(id)


@rule_app.command("list")
def rule_list(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")):
    show(service(ctx).rule_list(), as_json=as_json)


@rule_app.command("add")
def rule_add(ctx: typer.Context, name: str,
             bot: Annotated[int, typer.Option(help="目标机器人 ID")],
             include: Annotated[list[str], typer.Option(help="包含词，可重复指定")],
             exclude: Annotated[list[str] | None, typer.Option()] = None,
             source: Annotated[list[int] | None, typer.Option(help="源 ID，可重复；省略为所有源")] = None,
             mode: str = "any"):
    id = service(ctx).rule_save(name, include, exclude or [], mode, source or [], bot)
    console.print(f"规则 ID：{id}")


@rule_app.command("update")
def rule_update(ctx: typer.Context, id: int, name: str,
                bot: Annotated[int, typer.Option()],
                include: Annotated[list[str], typer.Option()],
                exclude: Annotated[list[str] | None, typer.Option()] = None,
                source: Annotated[list[int] | None, typer.Option()] = None,
                mode: str = "any"):
    """完整替换规则内容；省略 exclude/source 会清空，启停状态保持。"""
    service(ctx).rule_save(name, include, exclude or [], mode, source or [], bot, id)
    console.print("已更新")


@rule_app.command("enable")
def rule_enable(ctx: typer.Context, id: int):
    service(ctx).rule_enable(id, True)


@rule_app.command("disable")
def rule_disable(ctx: typer.Context, id: int):
    service(ctx).rule_enable(id, False)


@rule_app.command("delete")
def rule_delete(ctx: typer.Context, id: int):
    service(ctx).rule_delete(id)


@rule_app.command("test")
def rule_test(ctx: typer.Context, id: int, title: str, summary: str = ""):
    hits = service(ctx).rule_test(id, title, summary)
    console.print(f"命中：{', '.join(hits)}" if hits else "未命中")


@bot_app.command("list")
def bot_list(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")):
    show(service(ctx).bot_list(), as_json=as_json)


def credential_input(credential_file: Path | None, *, cancelable: bool = False) -> tuple[str, str]:
    if credential_file:
        if stat.S_IMODE(credential_file.stat().st_mode) != 0o600:
            raise ValueError("凭据输入文件权限必须为 0600")
        data = json.loads(credential_file.read_text())
        if not isinstance(data, dict):
            raise ValueError("凭据输入必须是 JSON 对象")
        return data["webhook"], data.get("secret", "")
    if not sys.stdin.isatty():
        raise ValueError("隐藏输入需要交互式终端；非交互运行请使用 --credential-file")
    if cancelable:
        return (
            menu_text("飞书 webhook（输入隐藏）", password=True,
                      validate=lambda value: validate_url(value, webhook=True)),
            menu_text("签名密钥（留空关闭加签，输入隐藏）", default="", password=True,
                      allow_empty=True),
        )
    return (Prompt.ask("飞书 webhook（输入隐藏）", password=True),
            Prompt.ask("签名密钥（可留空，输入隐藏）", password=True, default=""))


@bot_app.command("add")
def bot_add(ctx: typer.Context, name: str, credential_file: Path | None = None):
    """隐藏输入凭据，或从 JSON 文件读取，避免密钥出现在命令行参数中。"""
    webhook, secret = credential_input(credential_file)
    console.print(f"机器人 ID：{service(ctx).bot_save(name, webhook, secret)}")


@bot_app.command("update")
def bot_update(ctx: typer.Context, id: int, name: str, credential_file: Path | None = None):
    """替换名称和凭据；空密钥表示关闭请求加签。"""
    webhook, secret = credential_input(credential_file)
    service(ctx).bot_save(name, webhook, secret, id)
    console.print("已更新")


@bot_app.command("delete")
def bot_delete(ctx: typer.Context, id: int):
    service(ctx).bot_delete(id)


@bot_app.command("test")
def bot_test(ctx: typer.Context, id: int):
    event = service(ctx).bot_test(id)
    console.print(f"测试消息已入队：{event}（由后台发送，请确保服务运行）")


@app.command()
def history(ctx: typer.Context, limit: int = 30, state: str | None = None,
            as_json: bool = typer.Option(False, "--json"), detail: bool = False):
    columns = None if detail else ["id", "bot_id", "state", "attempts", "last_error", "matches"]
    show(service(ctx).history(limit, state), columns, as_json)


@delivery_app.command("retry")
def delivery_retry(ctx: typer.Context, event_id: str):
    service(ctx).retry(event_id)
    console.print("已重新入队")


def show_status(data: dict, detail: bool = False):
    console.print(f"后台：{'在线' if data['后台运行'] else '离线'}")
    heartbeat = data["心跳时间"]
    console.print("最近心跳：" + (
        datetime.fromtimestamp(heartbeat).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        if heartbeat else "尚无记录"
    ))
    queue = {row["state"]: row["count"] for row in data["队列"]}
    console.print("推送：" + " · ".join(
        f"{label} {queue.get(state, 0)}"
        for state, label in [("pending", "待发送"), ("sending", "发送中"),
                             ("sent", "已发送"), ("failed", "失败")]
    ))
    sources = data["源错误"]
    errors = data["最近推送错误"]
    console.print(f"异常记录：订阅源 {len(sources)} 个 · 最近推送错误 {len(errors)} 条（最多 5 条）")
    if not data["后台运行"]:
        console.print("未检测到有效后台心跳，请检查服务是否启动。")
    if detail:
        console.print("\n订阅源错误")
        show([{"ID": row["id"], "订阅源": row["name"], "最近错误": row["last_error"]}
              for row in sources])
        console.print("\n最近推送错误")
        show([{"事件 ID": row["id"], "最近错误": row["last_error"]} for row in errors])
    elif sources or errors:
        console.print("查看错误详情：rss-watch status --detail")


@app.command()
def status(ctx: typer.Context,
           as_json: bool = typer.Option(False, "--json", help="输出原始 JSON，供脚本使用"),
           detail: bool = typer.Option(False, "--detail", help="展开错误详情")):
    """查看后台在线状态、推送数量和异常摘要。"""
    data = service(ctx).status()
    if as_json:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        show_status(data, detail)


@app.command()
def backup(ctx: typer.Context, destination: Path):
    """在线备份数据库及引用的凭据；目标目录必须不存在。"""
    service(ctx).backup(destination)
    console.print(f"备份完成：{destination}")


@app.command("restore")
def restore(ctx: typer.Context, directory: Path, yes: bool = typer.Option(False, "--yes")):
    """恢复完整备份目录；暂停监控、保存当前数据，并保持维护模式等待检查。"""
    from .archives import restore_backup
    if not yes and not typer.confirm("确认用备份覆盖业务数据？", default=False):
        console.print("已取消")
        return
    rollback = restore_backup(service(ctx), directory)
    console.print(f"恢复完成；恢复前备份：{rollback}。请检查数据后在后台退出维护模式。")


@app.command()
def run(ctx: typer.Context):
    """运行后台监控（前台进程，供 systemd 托管）。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(run_daemon(service(ctx).config))


def split_words(value: str) -> list[str]:
    return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]


class MenuCancelled(Exception):
    """Return to the current menu without saving a partial form."""


def menu_text(label, default=None, *, allow_empty=False, clearable=False,
              password=False, validate=None):
    if clearable:
        label += "（回车保留，输入 - 清空）"
    while True:
        value = Prompt.ask(label, default=default if default is not None else "",
                           password=password, console=console)
        if not password:
            value = value.strip()
        if value == "/取消":
            raise MenuCancelled
        if clearable and value == "-":
            value = ""
        if not value and not allow_empty:
            console.print("此项不能为空，请重新输入；输入 /取消 返回。")
            continue
        try:
            if validate:
                validate(value)
        except ValueError as error:
            show_error(error)
            continue
        return value


def menu_number(label, default=None, minimum=1):
    while True:
        value = menu_text(label, str(default) if default is not None else None)
        try:
            number = int(value)
        except ValueError:
            console.print("请输入整数。")
            continue
        if number < minimum:
            console.print(f"请输入不小于 {minimum} 的整数。")
            continue
        return number


def menu_choice(label, choices, default="0"):
    while True:
        value = menu_text(label, default)
        if value in choices:
            return value
        console.print("无效选择，请输入列出的选项。")


def select_record(rows, label, default=None):
    if not rows:
        console.print(f"暂无可选{label}，请先新增。")
        raise MenuCancelled
    while True:
        id = menu_number(f"{label} ID（0 取消）", default, minimum=0)
        if id == 0:
            raise MenuCancelled
        for row in rows:
            if row["id"] == id:
                return row
        console.print(f"{label} ID 不存在，请从列表中选择。")


def menu_confirm(message):
    return menu_choice(message + "\n1 确认  0 取消", ["0", "1"]) == "1"


def show_sources(rows):
    show([{"ID": row["id"], "订阅源": row["name"], "间隔（秒）": row["interval"],
           "状态": "启用" if row["enabled"] else "停用",
           "首次抓取": "已完成" if row["initialized"] else "等待中",
           "最近错误": row["last_error"] or "—"} for row in rows])


def show_bots(rows):
    show([{"ID": row["id"], "机器人": row["name"]} for row in rows])


def source_menu(svc: Service):
    rows = svc.source_list()
    show_sources(rows)
    action = menu_choice("1 新增  2 修改  3 启用  4 停用  5 删除  0 返回", list("012345"))
    if action == "0":
        return False
    if action == "1":
        name = menu_text("名称")
        url = menu_text("RSS/Atom URL", validate=validate_url)
        interval = menu_number("抓取间隔（秒，至少 60）", 300, minimum=60)
        id = svc.source_save(name, url, interval)
        console.print(f"已新增订阅源「{name}」（ID：{id}）。首次抓取建立基线，不推送历史文章。")
        return
    row = select_record(rows, "订阅源")
    id = row["id"]
    if action == "2":
        console.print("修改时回车保留原值；更换订阅地址请删除旧源后新增。")
        svc.source_save(menu_text("名称", row["name"]), row["url"],
                        menu_number("抓取间隔（秒，至少 60）", row["interval"], minimum=60), id)
        console.print("订阅源已更新。")
    elif action in {"3", "4"}:
        svc.source_enable(id, action == "3")
        console.print(f"订阅源「{row['name']}」已{'启用' if action == '3' else '停用'}。")
    elif menu_confirm(f"删除订阅源「{row['name']}」（ID：{id}）？历史及已入队消息仍保留。"):
        svc.source_delete(id)
        console.print("订阅源已删除。")
    else:
        console.print("已取消删除。")


def rule_menu(svc: Service):
    rows = svc.rule_list()
    bots = svc.bot_list()
    bot_names = {row["id"]: row["name"] for row in bots}
    show([{"ID": row["id"], "规则": row["name"],
           "包含词": "、".join(json.loads(row["includes"])),
           "排除词": "、".join(json.loads(row["excludes"])) or "无",
           "匹配": "任一" if row["mode"] == "any" else "全部",
           "订阅源 ID": ", ".join(map(str, json.loads(row["source_ids"]))) or "全部",
           "机器人": bot_names.get(row["bot_id"], str(row["bot_id"])),
           "状态": "启用" if row["enabled"] else "停用"} for row in rows])
    action = menu_choice("1 新增  2 修改  3 启用  4 停用  5 删除  6 测试  0 返回", list("0123456"))
    if action == "0":
        return False
    old = {} if action == "1" else select_record(rows, "规则")
    id = old.get("id")
    if action in {"1", "2"}:
        if not bots:
            console.print("请先返回主菜单，在「飞书机器人」中新增机器人，再创建规则。")
            return
        console.print("包含词与排除词只匹配标题；多个词用中文或英文逗号分隔。")
        if old:
            console.print("回车保留原值；排除词与订阅源范围可输入 - 清空。")
        name = menu_text("规则名称", old.get("name"))
        show_bots(bots)
        bot = select_record(bots, "目标机器人", old.get("bot_id"))["id"]

        def require_words(value):
            if not split_words(value):
                raise ValueError("至少输入一个包含词。")

        includes = split_words(menu_text(
            "包含词（逗号分隔）", ", ".join(json.loads(old.get("includes", "[]"))),
            validate=require_words,
        ))
        excludes = split_words(menu_text(
            "排除词（逗号分隔，可空）", ", ".join(json.loads(old.get("excludes", "[]"))),
            allow_empty=True, clearable=bool(old),
        ))
        mode = {"1": "any", "2": "all"}[menu_choice(
            "匹配方式：1 任一包含词  2 全部包含词", ["1", "2"],
            "2" if old.get("mode") == "all" else "1",
        )]
        source_rows = svc.source_list()
        show_sources(source_rows)
        available = {row["id"] for row in source_rows}

        def validate_sources(value):
            try:
                ids = {int(part) for part in split_words(value)}
            except ValueError:
                raise ValueError("订阅源 ID 必须是整数，用逗号分隔。") from None
            if ids - available:
                raise ValueError("存在无效订阅源 ID，请从列表中选择；输入 - 可改为全部。")

        sources = [int(part) for part in split_words(menu_text(
            "订阅源 ID（逗号分隔，空表示全部）",
            ", ".join(map(str, json.loads(old.get("source_ids", "[]")))),
            allow_empty=True, clearable=True, validate=validate_sources,
        ))]
        saved_id = svc.rule_save(name, includes, excludes, mode, sources, bot, id)
        console.print(f"规则「{name}」已保存（ID：{saved_id}）。")
    elif action in {"3", "4"}:
        svc.rule_enable(id, action == "3")
        console.print(f"规则「{old['name']}」已{'启用' if action == '3' else '停用'}。")
    elif action == "5":
        if menu_confirm(f"删除规则「{old['name']}」（ID：{id}）？"):
            svc.rule_delete(id)
            console.print("规则已删除。")
        else:
            console.print("已取消删除。")
    else:
        console.print("仅测试标题匹配，不发送消息。")
        hits = svc.rule_test(id, menu_text("样本标题"), "")
        console.print(f"命中：{', '.join(hits)}" if hits else "未命中")


def bot_menu(svc: Service):
    rows = svc.bot_list()
    show_bots(rows)
    action = menu_choice("1 新增  2 替换凭据和名称  3 删除  4 测试消息  0 返回", list("01234"))
    if action == "0":
        return False
    old = {} if action == "1" else select_record(rows, "机器人")
    id = old.get("id")
    if action in {"1", "2"}:
        if old:
            console.print("将替换此机器人的凭据；名称回车保留，签名密钥留空会关闭加签。")
        name = menu_text("机器人名称", old.get("name"))
        webhook, secret = credential_input(None, cancelable=True)
        saved_id = svc.bot_save(name, webhook, secret, id)
        console.print(f"机器人「{name}」已保存（ID：{saved_id}）。")
    elif action == "3":
        if menu_confirm(f"删除机器人「{old['name']}」（ID：{id}）？"):
            svc.bot_delete(id)
            console.print("机器人已删除。")
        else:
            console.print("已取消删除。")
    elif menu_confirm(f"向机器人「{old['name']}」发送一条测试消息？"):
        console.print(f"测试已入队：{svc.bot_test(id)}（由后台发送，请确保服务运行）。")
    else:
        console.print("已取消测试消息。")


def history_menu(svc: Service, *, failed=False):
    rows = svc.history(state="failed" if failed else None)
    states = {"pending": "待发送", "sending": "发送中", "sent": "已发送", "failed": "失败"}
    show([{"序号": index, "状态": states.get(row["state"], row["state"]),
           "机器人 ID": row["bot_id"],
           "创建时间": datetime.fromtimestamp(row["created"]).astimezone().strftime("%m-%d %H:%M"),
           "消息预览": next((line.removeprefix("标题：") for line in row["payload"].splitlines()
                             if line.startswith("标题：")), row["payload"])[:60],
           "尝试次数": row["attempts"],
           "最近错误": row["last_error"] or "—"}
          for index, row in enumerate(rows, 1)])
    if not rows:
        console.print("暂无失败任务。" if failed else "暂无推送历史。")
    console.print("显示最近 30 条。输入序号查看消息详情，r 刷新，0 返回。")
    choice = menu_choice("选择", ["0", "r", *map(str, range(1, len(rows) + 1))])
    if choice == "0":
        return False
    if choice == "r":
        return
    row = rows[int(choice) - 1]
    console.print(f"事件 ID：{row['id']}\n消息内容：\n{row['payload']}")
    if row["last_error"]:
        console.print(f"最近错误：{row['last_error']}")
    if row["state"] == "failed" and menu_confirm("将此失败任务重新入队？后台运行后会再次发送。"):
        svc.retry(row["id"])
        console.print("任务已重新入队。")


def run_submenu(svc, title, operation):
    while True:
        console.print(f"\n{title}\n0 返回上级；填写时输入 /取消 或按 Ctrl+C 取消当前操作。")
        try:
            if operation(svc) is False:
                return
        except (MenuCancelled, KeyboardInterrupt):
            console.print("已取消当前操作。")
        except (ValueError, OSError, sqlite3.Error, KeyError) as error:
            show_error(error)


@app.command()
def menu(ctx: typer.Context):
    """中文交互式管理菜单，退出菜单不会停止后台服务。"""
    svc = service(ctx)
    while True:
        console.print("\nRSS Watch · 中文管理\n1 订阅源管理\n2 关键词规则\n3 飞书机器人\n"
                      "4 命中及推送历史\n5 失败任务与重试\n6 服务状态\n0 退出\n退出菜单不影响后台监控。")
        try:
            action = menu_choice("请选择功能", list("0123456"))
            if action == "0":
                console.print("已退出管理菜单，后台监控不受影响。")
                return
            if action == "1":
                run_submenu(svc, "订阅源管理", source_menu)
            elif action == "2":
                run_submenu(svc, "关键词规则", rule_menu)
            elif action == "3":
                run_submenu(svc, "飞书机器人", bot_menu)
            elif action == "4":
                run_submenu(svc, "命中及推送历史", history_menu)
            elif action == "5":
                run_submenu(svc, "失败任务与重试", lambda svc: history_menu(svc, failed=True))
            else:
                show_status(svc.status(), detail=True)
        except MenuCancelled:
            console.print("已返回主菜单。")
        except (ValueError, OSError, sqlite3.Error, KeyError) as error:
            show_error(error)
        except (EOFError, KeyboardInterrupt):
            console.print("\n已退出管理菜单，后台监控不受影响。")
            return


def show_error(error: Exception):
    if isinstance(error, sqlite3.IntegrityError):
        console.print("操作失败：记录重复或关联约束不满足")
    elif isinstance(error, sqlite3.Error):
        console.print("数据库操作失败，请检查路径、权限或数据库状态")
    elif isinstance(error, (OSError, KeyError)):
        console.print("配置或文件访问失败，请检查文件格式、路径及权限")
    elif isinstance(error, ValueError):
        # Only our validation errors contain user-facing text. JSON/TOML errors may echo input.
        if type(error) is ValueError:
            console.print(str(error))
        else:
            console.print("输入或配置格式错误")
    else:
        console.print(f"操作失败（{type(error).__name__}），请检查配置或联系维护者")


def main():
    try:
        app()
    except Exception as error:
        show_error(error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
