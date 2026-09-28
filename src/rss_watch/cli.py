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
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from .config import load_config
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
def source_add(ctx: typer.Context, name: str, url: str, interval: int = 300):
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


def credential_input(credential_file: Path | None) -> tuple[str, str]:
    if credential_file:
        if stat.S_IMODE(credential_file.stat().st_mode) != 0o600:
            raise ValueError("凭据输入文件权限必须为 0600")
        data = json.loads(credential_file.read_text())
        if not isinstance(data, dict):
            raise ValueError("凭据输入必须是 JSON 对象")
        return data["webhook"], data.get("secret", "")
    if not sys.stdin.isatty():
        raise ValueError("隐藏输入需要交互式终端；非交互运行请使用 --credential-file")
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
        show(sources, ["id", "name", "last_error"])
        console.print("\n最近推送错误")
        show(errors, ["id", "last_error"])
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


@app.command()
def run(ctx: typer.Context):
    """运行后台监控（前台进程，供 systemd 托管）。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(run_daemon(service(ctx).config))


def split_words(value: str) -> list[str]:
    return [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]


def select_id() -> int:
    return IntPrompt.ask("请输入 ID")


def source_menu(svc: Service):
    show(svc.source_list(), ["id", "name", "interval", "enabled", "initialized", "last_error"])
    action = Prompt.ask("1 新增  2 修改  3 启用  4 停用  5 删除  0 返回",
                        choices=["0", "1", "2", "3", "4", "5"])
    if action == "0":
        return
    if action == "1":
        svc.source_save(Prompt.ask("名称"), Prompt.ask("RSS/Atom URL"),
                        IntPrompt.ask("抓取间隔（秒）", default=300))
        return
    id = select_id()
    if action == "2":
        rows = [r for r in svc.source_list() if r["id"] == id]
        if not rows:
            raise ValueError("订阅源不存在")
        row = rows[0]
        svc.source_save(Prompt.ask("名称", default=row["name"]), row["url"],
                        IntPrompt.ask("间隔（秒）", default=row["interval"]), id)
    elif action in {"3", "4"}:
        svc.source_enable(id, action == "3")
    elif Confirm.ask("删除此订阅源？历史及已入队消息仍保留", default=False):
        svc.source_delete(id)


def rule_menu(svc: Service):
    show(svc.rule_list())
    action = Prompt.ask("1 新增  2 修改  3 启用  4 停用  5 删除  6 测试  0 返回",
                        choices=[str(n) for n in range(7)])
    if action == "0":
        return
    id = None if action == "1" else select_id()
    if action in {"1", "2"}:
        old = {}
        if id is not None:
            rows = [r for r in svc.rule_list() if r["id"] == id]
            if not rows:
                raise ValueError("规则不存在")
            old = rows[0]
        show(svc.bot_list())
        name = Prompt.ask("规则名称", default=old.get("name", ""))
        bot = IntPrompt.ask("目标机器人 ID", default=old.get("bot_id", 1))
        includes = split_words(Prompt.ask("包含词（逗号分隔）",
                                         default=", ".join(json.loads(old.get("includes", "[]")))))
        excludes = split_words(Prompt.ask("排除词（逗号分隔，可空）",
                                         default=", ".join(json.loads(old.get("excludes", "[]")))))
        mode = Prompt.ask("any 任一 / all 全部", choices=["any", "all"], default=old.get("mode", "any"))
        show(svc.source_list(), ["id", "name"])
        sources = [int(s) for s in split_words(Prompt.ask(
            "源 ID（逗号分隔，空表示全部）",
            default=", ".join(str(s) for s in json.loads(old.get("source_ids", "[]"))),
        ))]
        svc.rule_save(name, includes, excludes, mode, sources, bot, id)
    elif action in {"3", "4"}:
        svc.rule_enable(id, action == "3")
    elif action == "5":
        if Confirm.ask("删除此规则？", default=False):
            svc.rule_delete(id)
    else:
        hits = svc.rule_test(id, Prompt.ask("样本标题"), Prompt.ask("样本摘要", default=""))
        console.print(f"命中：{', '.join(hits)}" if hits else "未命中")


def bot_menu(svc: Service):
    show(svc.bot_list())
    action = Prompt.ask("1 新增  2 替换凭据和名称  3 删除  4 测试消息  0 返回",
                        choices=[str(n) for n in range(5)])
    if action == "0":
        return
    id = None if action == "1" else select_id()
    if action in {"1", "2"}:
        name = Prompt.ask("机器人名称")
        webhook, secret = credential_input(None)
        svc.bot_save(name, webhook, secret, id)
    elif action == "3":
        if Confirm.ask("删除此机器人？", default=False):
            svc.bot_delete(id)
    else:
        console.print(f"测试已入队：{svc.bot_test(id)}（需后台运行）")


@app.command()
def menu(ctx: typer.Context):
    """中文交互式管理菜单，退出菜单不会停止后台服务。"""
    svc = service(ctx)
    while True:
        console.print("\nRSS Watch\n1 订阅源管理\n2 关键词规则\n3 飞书机器人\n"
                      "4 命中及推送历史\n5 失败任务与重试\n6 服务状态\n0 退出")
        try:
            action = Prompt.ask("选择", choices=[str(n) for n in range(7)])
            if action == "0":
                return
            if action == "1":
                source_menu(svc)
            elif action == "2":
                rule_menu(svc)
            elif action == "3":
                bot_menu(svc)
            elif action == "4":
                show(svc.history(), ["id", "state", "payload", "last_error"])
            elif action == "5":
                show(svc.history(state="failed"), ["id", "payload", "last_error"])
                event = Prompt.ask("输入事件 ID 重试，留空返回", default="")
                if event:
                    svc.retry(event)
            else:
                show_status(svc.status())
        except (ValueError, OSError, sqlite3.Error, KeyError) as error:
            show_error(error)
        except (EOFError, KeyboardInterrupt):
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
