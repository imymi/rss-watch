from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .archives import (
    MAX_ARCHIVE,
    create_backup,
    find_backup,
    list_backups,
    restore_backup,
    unpack_archive,
    zip_backup,
)
from .auth import Auth
from .config import Config, file_lock
from .maintenance import active as maintenance_active
from .maintenance import data_lock
from .service import Service
from .settings import FIELDS, read_settings, save_settings

log = logging.getLogger("rss_watch")
ASSETS = Path(__file__).parent / "ui"
NAV = [("/", "总览"), ("/sources", "订阅源"), ("/rules", "关键词规则"),
       ("/bots", "飞书机器人"), ("/history", "推送历史"), ("/settings", "系统设置"),
       ("/operations", "运维与备份")]


class BodyLimit:
    """Bound both declared and streamed bodies before multipart spooling."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > MAX_ARCHIVE + 1024 * 1024:
                raise HTTPException(413, "上传文件超过 100 MiB 限制")
            return message

        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = MAX_ARCHIVE + 1024 * 1024 + 1
        if declared > MAX_ARCHIVE + 1024 * 1024:
            return await JSONResponse({"detail": "上传文件超过 100 MiB 限制"}, 413)(
                scope, receive, send
            )
        await self.app(scope, limited_receive, send)


def payload_words(value):
    if isinstance(value, list):
        if any(not isinstance(word, str) for word in value):
            raise ValueError("关键词格式无效")
        return value
    if not isinstance(value, str):
        raise ValueError("关键词应为字符串数组或逐行文本")
    return [word.strip() for word in value.splitlines() if word.strip()]


def save_entity(service, kind, values, id=None):
    name = str(values.get("name", ""))
    if len(name) > 200:
        raise ValueError("名称不能超过 200 个字符")
    if kind == "sources":
        url = str(values.get("url", ""))
        if len(url) > 2048:
            raise ValueError("订阅地址不能超过 2048 个字符")
        if id:
            old = next((r for r in service.source_list() if r["id"] == id), None)
            if not old:
                raise ValueError("订阅源不存在")
            url = old["url"]
        interval = values.get("interval")
        return service.source_save(name, url, int(interval) if interval else None, id)
    if kind == "bots":
        webhook = str(values.get("webhook", ""))
        secret = str(values.get("secret", ""))
        if len(webhook) > 2048 or len(secret) > 2048:
            raise ValueError("Webhook 或签名密钥过长")
        if id and not webhook:
            # Blank credentials preserve existing values; no credentials are sent to the browser.
            from .maintenance import business_write

            @business_write
            def rename(svc):
                with svc.db.transaction() as db:
                    if not name.strip():
                        raise ValueError("名称不能为空")
                    if not db.execute("UPDATE bots SET name=? WHERE id=? AND deleted=0",
                                      (name.strip(), id)).rowcount:
                        raise ValueError("机器人不存在")
                return id
            if secret:
                raise ValueError("替换签名密钥时请同时填写 Webhook")
            return rename(service)
        return service.bot_save(name, webhook, secret, id)
    if kind == "rules":
        sources = values.get("source_ids", [])
        if isinstance(sources, str):
            sources = [p.strip() for p in sources.split(",") if p.strip()]
        return service.rule_save(name, payload_words(values.get("includes", "")),
                                 payload_words(values.get("excludes", "")),
                                 str(values.get("mode", "any")), [int(s) for s in sources],
                                 int(values.get("bot_id", 0)), id)
    raise ValueError("未知管理类型")


def status_data(service):
    value = service.status()
    runtime = {r["key"]: r["value"] for r in service.db.rows("SELECT * FROM runtime")}
    value.update({"maintenance": maintenance_active(service.config),
                  "settings_pending": runtime.get("settings_revision", "")
                  != runtime.get("settings_applied", ""),
                  "settings": read_settings(service.db),
                  "last_backup": runtime.get("last_auto_backup"),
                  "backup_error": runtime.get("auto_backup_error")})
    return value


def automatic_backup(service):
    with data_lock(service.config):
        _automatic_backup(service)


def _automatic_backup(service):
    if maintenance_active(service.config):
        return
    settings = read_settings(service.db)
    if not settings["backup_hours"]:
        return
    rows = service.db.rows("SELECT value FROM runtime WHERE key='last_auto_backup'")
    last = float(rows[0]["value"]) if rows else 0
    if time.time() - last < settings["backup_hours"] * 3600:
        return
    create_backup(service, "auto")
    with service.db.transaction() as db:
        db.execute("INSERT OR REPLACE INTO runtime VALUES ('last_auto_backup',?)",
                   (str(time.time()),))
        db.execute("DELETE FROM runtime WHERE key='auto_backup_error'")
    backups = [p for p in list_backups(service) if p.name.startswith("auto-")]
    import shutil
    for path in backups[settings["backup_keep"]:]:
        shutil.rmtree(path)


async def backup_loop(service):
    with file_lock(service.config.data_dir / "backup-scheduler.lock", blocking=False):
        while True:
            try:
                await run_in_threadpool(automatic_backup, service)
            except (OSError, ValueError, sqlite3.Error):
                log.warning("自动备份失败，请检查数据目录和机器人凭据")
                if not maintenance_active(service.config):
                    with service.db.transaction() as db:
                        db.execute("INSERT OR REPLACE INTO runtime VALUES ('auto_backup_error',?)",
                                   ("自动备份失败，请检查数据目录和机器人凭据",))
            await asyncio.sleep(30)


def create_app(config: Config, *, schedule_backups: bool = True) -> FastAPI:
    config.prepare()
    service = Service(config)
    auth = Auth(service)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(backup_loop(service)) if schedule_backups else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="RSS Watch", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.auth = auth
    templates = Jinja2Templates(directory=str(ASSETS))
    templates.env.filters["unjson"] = json.loads
    templates.env.filters["localtime"] = lambda v, tz="Asia/Shanghai": (
        datetime.fromtimestamp(float(v), ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S")
        if v else "—"
    )
    app.mount("/static", StaticFiles(directory=str(ASSETS / "static")), name="static")
    secure = os.environ.get("RSS_WATCH_SECURE_COOKIES", "0") == "1"

    def page(request, view, **context):
        settings = read_settings(service.db)
        return templates.TemplateResponse(request=request, name="page.html", context={
            "view": view, "nav": NAV, "settings": settings,
            "csrf": auth.csrf(request.cookies.get("rss_session", "")),
            "maintenance": maintenance_active(config), "notice": request.query_params.get("notice"),
            "username": (auth.admin() or {}).get("username", ""), **context,
        })

    def redirect(path, notice="已保存"):
        return RedirectResponse(path + ("?" + urlencode({"notice": notice}) if notice else ""), 303)

    async def form(request):
        result = await request.form(max_files=1, max_fields=100, max_part_size=65536)
        token = request.cookies.get("rss_session", "")
        if not auth.valid_csrf(token, str(result.get("csrf", ""))):
            raise HTTPException(403, "页面已过期，请刷新后再试")
        return result

    def audit(action):
        auth.audit((auth.admin() or {}).get("username", "管理员"), action)

    @app.middleware("http")
    async def protect(request, call_next):
        path = request.url.path
        if path.startswith("/static/") or path in {"/healthz", "/favicon.ico"}:
            response = await call_next(request)
        else:
            # Restore itself obtains the exclusive lock; every other request uses a read gate.
            gate = None if path in {"/operations/restore", "/operations/resume"} and request.method == "POST" else data_lock(config)
            if gate:
                await run_in_threadpool(gate.__enter__)
            try:
                if path not in {"/login", "/setup"}:
                    authenticated = await run_in_threadpool(
                        auth.authenticated, request.cookies.get("rss_session", "")
                    )
                    if not authenticated:
                        response = (JSONResponse({"detail": "请先登录"}, 401) if path.startswith("/api/")
                                    else redirect("/login" if auth.admin() else "/setup", ""))
                    elif maintenance_active(config) and request.method not in {"GET", "HEAD"} and path not in {
                        "/operations/restore", "/operations/resume", "/logout"
                    }:
                        response = page(request, "error", error="系统处于维护模式，请先检查数据并退出维护模式")
                        response.status_code = 409
                    elif path.startswith("/api/") and request.method not in {"GET", "HEAD"} and not auth.valid_csrf(
                        request.cookies.get("rss_session", ""), request.headers.get("x-csrf-token", "")
                    ):
                        response = JSONResponse({"detail": "CSRF 校验失败"}, 403)
                    else:
                        response = await call_next(request)
                else:
                    response = await call_next(request)
            except (ValueError, sqlite3.IntegrityError) as error:
                message = "记录已存在或仍被其他记录引用" if isinstance(error, sqlite3.IntegrityError) else str(error)
                response = (JSONResponse({"detail": message}, 400) if path.startswith("/api/")
                            else page(request, "error", error=message))
                response.status_code = 400
            except (OSError, sqlite3.OperationalError, zipfile.BadZipFile, KeyError, TypeError):
                response = (JSONResponse({"detail": "操作失败，请检查数据和输入"}, 400)
                            if path.startswith("/api/") else page(
                                request, "error", error="操作失败，请检查数据目录、凭据权限或上传文件"
                            ))
                response.status_code = 400
            finally:
                if gate:
                    await run_in_threadpool(gate.__exit__, None, None, None)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        })
        return response

    app.add_middleware(BodyLimit)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(ASSETS / "static" / "favicon.svg", media_type="image/svg+xml")

    @app.get("/login")
    @app.get("/setup")
    def login_page(request: Request):
        setup = auth.admin() is None
        nonce = request.cookies.get("rss_preauth", "")
        if not 32 <= len(nonce) <= 128 or not nonce.isascii():
            nonce = secrets.token_urlsafe(32)
        response = templates.TemplateResponse(request=request, name="login.html", context={
            "setup": setup, "csrf": auth.csrf(nonce), "notice": request.query_params.get("notice")
        })
        response.set_cookie("rss_preauth", nonce, httponly=True, secure=secure,
                            samesite="strict", max_age=1800)
        return response

    async def preauth(request):
        values = await request.form(max_files=0, max_fields=8, max_part_size=2048)
        if not auth.valid_csrf(request.cookies.get("rss_preauth", ""), str(values.get("csrf", ""))):
            raise HTTPException(403, "页面已过期，请刷新后再试")
        return values

    @app.post("/setup")
    async def setup(request: Request):
        values = await preauth(request)
        await run_in_threadpool(auth.throttle, request.client.host)
        await run_in_threadpool(auth.setup, str(values.get("token", "")),
                               str(values.get("username", "")), str(values.get("password", "")))
        return redirect("/login", "管理员已创建，请登录")

    @app.post("/login")
    async def login(request: Request):
        values = await preauth(request)
        token = await run_in_threadpool(auth.login, str(values.get("username", "")),
                                       str(values.get("password", "")), request.client.host)
        response = redirect("/", "")
        response.set_cookie("rss_session", token, httponly=True, secure=secure,
                            samesite="strict", max_age=43200)
        response.delete_cookie("rss_preauth")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        await form(request)
        await run_in_threadpool(auth.logout, request.cookies.get("rss_session", ""))
        response = redirect("/login", "")
        response.delete_cookie("rss_session")
        return response

    @app.get("/")
    def dashboard(request: Request):
        return page(request, "dashboard", status=status_data(service),
                    sources=service.source_list(), rules=service.rule_list(), bots=service.bot_list())

    @app.get("/sources")
    @app.get("/rules")
    @app.get("/bots")
    def entities(request: Request, edit: int | None = None):
        kind = request.url.path.strip("/")
        rows = getattr(service, kind[:-1] + "_list")()
        selected = next((row for row in rows if row["id"] == edit), None)
        if edit is not None and selected is None:
            raise ValueError("记录不存在")
        return page(request, kind, rows=rows, selected=selected,
                    sources=service.source_list(), bots=service.bot_list())

    @app.post("/sources/save")
    @app.post("/rules/save")
    @app.post("/bots/save")
    async def entity_save(request: Request):
        kind = request.url.path.split("/")[1]
        values = await form(request)
        id = int(values["id"]) if values.get("id") else None
        data = dict(values)
        if kind == "rules":
            data["source_ids"] = values.getlist("source_ids")
            if id and not data["source_ids"]:
                old = next((r for r in service.rule_list() if r["id"] == id), None)
                if old and json.loads(old["source_ids"]) and values.get("all_sources") != "1":
                    raise ValueError("清空来源范围会适用于全部订阅源，请勾选确认后保存")
        saved = await run_in_threadpool(save_entity, service, kind, data, id)
        await run_in_threadpool(audit, f"保存{kind} #{saved}")
        return redirect("/" + kind)

    @app.post("/{kind}/{id}/action")
    async def entity_action(request: Request, kind: str, id: int):
        values = await form(request)
        operation = values.get("operation")
        if kind not in {"sources", "rules", "bots"}:
            raise ValueError("未知管理类型")
        if operation in {"enable", "disable"} and kind != "bots":
            await run_in_threadpool(getattr(service, kind[:-1] + "_enable"), id, operation == "enable")
        elif operation == "delete":
            await run_in_threadpool(getattr(service, kind[:-1] + "_delete"), id)
        elif operation == "test" and kind == "bots":
            await run_in_threadpool(service.bot_test, id)
        elif operation == "test" and kind == "rules":
            hits = await run_in_threadpool(service.rule_test, id, str(values.get("title", "")), "")
            return redirect("/rules", "命中：" + "、".join(hits) if hits else "未命中规则")
        else:
            raise ValueError("操作无效")
        await run_in_threadpool(audit, f"{operation} {kind} #{id}")
        return redirect("/" + kind, "测试消息已入队" if operation == "test" else "操作完成")

    @app.get("/history")
    def history(request: Request, state: str | None = None, page_number: int = 1,
                bot_id: str = ""):
        if state and state not in {"pending", "sending", "sent", "failed"}:
            raise ValueError("推送状态无效")
        if not 1 <= page_number <= 100000:
            raise ValueError("页码无效")
        bot_id = int(bot_id) if bot_id else None
        where, args = [], []
        if state:
            where.append("d.state=?")
            args.append(state)
        if bot_id:
            where.append("d.bot_id=?")
            args.append(bot_id)
        sql = " WHERE " + " AND ".join(where) if where else ""
        total = service.db.rows("SELECT count(*) AS n FROM deliveries d" + sql, args)[0]["n"]
        rows = service.db.rows("SELECT d.*,b.name AS bot_name FROM deliveries d JOIN bots b "
                               "ON b.id=d.bot_id" + sql + " ORDER BY d.created DESC,d.id "
                               "LIMIT 30 OFFSET ?", [*args, (page_number - 1) * 30])
        return page(request, "history", rows=rows, total=total, page_number=page_number,
                    state=state or "", bot_id=bot_id, bots=service.bot_list())

    @app.post("/history/{event}/retry")
    async def retry(request: Request, event: str):
        await form(request)
        await run_in_threadpool(service.retry, event)
        await run_in_threadpool(audit, f"重试推送 {event}")
        return redirect("/history", "已重新入队")

    @app.get("/settings")
    def settings_page(request: Request):
        return page(request, "settings", fields=FIELDS, status=status_data(service))

    @app.post("/settings/save")
    async def settings_save(request: Request):
        values = await form(request)
        data = {}
        for key, field in FIELDS.items():
            if field.group == "控制":
                continue
            raw = values.get(key, field.default)
            data[key] = int(raw) if isinstance(field.default, int) else str(raw)
        await run_in_threadpool(save_settings, service.db, data)
        await run_in_threadpool(audit, "修改系统设置")
        return redirect("/settings", "设置已保存，监控服务将自动应用")

    @app.post("/settings/password")
    async def password(request: Request):
        values = await form(request)
        if values.get("password") != values.get("confirm_password"):
            raise ValueError("两次输入的新密码不一致")
        await run_in_threadpool(auth.change_password, str(values.get("current", "")),
                               str(values.get("password", "")))
        response = redirect("/login", "密码已修改，请重新登录")
        response.delete_cookie("rss_session")
        return response

    @app.get("/operations")
    def operations(request: Request):
        logs = []
        for name in ("worker.log", "web.log"):
            path = config.data_dir / "logs" / name
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 32768))
                    logs.append({"name": name, "content": stream.read().decode(errors="replace")})
        return page(request, "operations", status=status_data(service), logs=logs,
                    health=service.health(),
                    backups=list_backups(service), audit=service.db.rows(
                        "SELECT * FROM audit ORDER BY id DESC LIMIT 100"))

    @app.post("/operations/control")
    async def control(request: Request):
        values = await form(request)
        key = values.get("key")
        if key not in {"fetch_paused", "delivery_paused"}:
            raise ValueError("控制选项无效")
        await run_in_threadpool(save_settings, service.db, {key: values.get("paused") == "1"})
        await run_in_threadpool(audit, f"设置 {key}={values.get('paused') == '1'}")
        return redirect("/operations", "状态已保存，在途请求结束后生效")

    @app.post("/operations/backup")
    async def backup(request: Request):
        await form(request)
        destination = await run_in_threadpool(create_backup, service)
        await run_in_threadpool(audit, f"创建备份 {destination.name}")
        return redirect("/operations", "备份已完成，包含机器人凭据")

    @app.get("/operations/backups/{name}/download")
    async def download(name: str):
        directory = find_backup(service, name)
        fd, temporary = tempfile.mkstemp(suffix=".zip", dir=config.data_dir)
        os.close(fd)
        destination = Path(temporary)
        try:
            await run_in_threadpool(zip_backup, directory, destination)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return FileResponse(destination, filename=name + ".zip", media_type="application/zip",
                            background=BackgroundTask(destination.unlink, missing_ok=True))

    @app.post("/operations/backups/{name}/delete")
    async def delete_backup(request: Request, name: str):
        import shutil
        await form(request)
        await run_in_threadpool(shutil.rmtree, find_backup(service, name))
        await run_in_threadpool(audit, f"删除备份 {name}")
        return redirect("/operations", "备份已删除")

    @app.post("/operations/restore")
    async def restore(request: Request):
        values = await form(request)
        if values.get("confirmation") != "恢复":
            raise ValueError("请输入“恢复”确认覆盖业务数据")
        upload = values.get("archive")
        if not upload or not hasattr(upload, "read"):
            raise ValueError("请选择 ZIP 备份")
        fd, temporary = tempfile.mkstemp(suffix=".zip", dir=config.data_dir)
        os.close(fd)
        path = Path(temporary)
        try:
            size = 0
            with path.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise ValueError("上传文件超过 100 MiB 限制")
                    output.write(chunk)

            def apply():
                with unpack_archive(config, path) as directory:
                    return restore_backup(service, directory)
            await run_in_threadpool(apply)
            await run_in_threadpool(audit, "恢复备份，等待检查并退出维护模式")
        finally:
            path.unlink(missing_ok=True)
            await upload.close()
        return redirect("/login", "恢复完成，请重新登录，检查数据后退出维护模式；管理员密码保持不变")

    @app.post("/operations/resume")
    async def resume(request: Request):
        await form(request)
        def leave():
            if maintenance_active(config):
                with data_lock(config, exclusive=True):
                    (config.data_dir / "maintenance").unlink(missing_ok=True)
        await run_in_threadpool(leave)
        await run_in_threadpool(audit, "退出维护模式")
        return redirect("/operations", "已退出维护模式，可分别恢复抓取和推送")

    @app.get("/api/{kind}")
    def api_read(kind: str, limit: int = 30, state: str | None = None):
        if kind == "status":
            # Proxy URLs may contain credentials; the status API only returns controls.
            result = status_data(service)
            result["timezone"] = result["settings"]["timezone"]
            result["settings"] = {k: result["settings"][k] for k in ("fetch_paused", "delivery_paused")}
            return result
        if kind in {"sources", "rules", "bots"}:
            rows = getattr(service, kind[:-1] + "_list")()
            if kind == "rules":
                rows = [{**row, **{k: json.loads(row[k]) for k in ("includes", "excludes", "source_ids")}}
                        for row in rows]
            return rows
        if kind == "history":
            return [{**r, "matches": json.loads(r["matches"])} for r in service.history(limit, state)]
        if kind == "settings":
            return read_settings(service.db)
        if kind == "health":
            return service.health()
        raise HTTPException(404)

    @app.get("/api/session/csrf")
    def api_csrf(request: Request):
        return {"csrf": auth.csrf(request.cookies.get("rss_session", ""))}

    @app.post("/api/{kind}")
    @app.put("/api/{kind}/{id}")
    async def api_save(request: Request, kind: str, id: int | None = None):
        values = await request.json()
        if not isinstance(values, dict):
            raise ValueError("请求必须是 JSON 对象")
        if kind == "settings" and id is None:
            await run_in_threadpool(save_settings, service.db, values)
            await run_in_threadpool(audit, "通过 API 修改系统设置")
            return {"ok": True}
        result = await run_in_threadpool(save_entity, service, kind, values, id)
        await run_in_threadpool(audit, f"通过 API 保存{kind} #{result}")
        return {"id": result}

    @app.post("/api/deliveries/{event}/retry")
    async def api_retry(event: str):
        await run_in_threadpool(service.retry, event)
        await run_in_threadpool(audit, f"通过 API 重试推送 {event}")
        return {"ok": True}

    @app.post("/api/{kind}/{id}/{operation}")
    async def api_action(request: Request, kind: str, id: int, operation: str):
        if kind not in {"sources", "rules", "bots"}:
            raise ValueError("未知管理类型")
        if operation in {"enable", "disable"} and kind != "bots":
            await run_in_threadpool(getattr(service, kind[:-1] + "_enable"), id, operation == "enable")
        elif operation == "delete":
            await run_in_threadpool(getattr(service, kind[:-1] + "_delete"), id)
        elif operation == "test" and kind == "bots":
            result = await run_in_threadpool(service.bot_test, id)
            await run_in_threadpool(audit, f"通过 API 测试 bots #{id}")
            return {"event_id": result}
        elif operation == "test" and kind == "rules":
            values = await request.json()
            if not isinstance(values, dict) or not isinstance(values.get("title"), str):
                raise ValueError("规则测试需要 title 字符串")
            hits = await run_in_threadpool(service.rule_test, id, values["title"], "")
            return {"matched": bool(hits), "keywords": hits}
        else:
            raise ValueError("操作无效")
        await run_in_threadpool(audit, f"通过 API {operation} {kind} #{id}")
        return {"ok": True}

    return app
