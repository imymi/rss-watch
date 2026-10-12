import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from rss_watch.archives import create_backup, zip_backup
from rss_watch.auth import verify_password
from rss_watch.settings import read_settings
from rss_watch.web import create_app

PASSWORD = "test-admin-password-123"
WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/web-secret-token"


def csrf(response):
    return re.search(r'name="csrf" value="([a-f0-9]+)"', response.text)[1]


def login(client, username="管理员", password=PASSWORD):
    token = csrf(client.get("/login"))
    return client.post("/login", data={"csrf": token, "username": username, "password": password})


@pytest.fixture
def web(config):
    app = create_app(config, schedule_backups=False)
    with TestClient(app) as client:
        setup_csrf = csrf(client.get("/setup"))
        result = client.post("/setup", data={
            "csrf": setup_csrf, "token": app.state.auth.bootstrap_path.read_text(),
            "username": "管理员", "password": PASSWORD,
        })
        assert result.status_code == 200, result.text
        assert login(client).status_code == 200
        yield client, app.state.service, app.state.auth


def post(client, path, **values):
    values["csrf"] = csrf(client.get("/"))
    return client.post(path, data=values)


def test_setup_requires_token_csrf_and_stores_only_password_hash(config):
    app = create_app(config, schedule_backups=False)
    with TestClient(app) as client:
        assert client.get("/api/sources").status_code == 401
        assert client.get("/healthz").json() == {"status": "ok"}
        token = csrf(client.get("/setup"))
        assert client.post("/setup", data={"token": "wrong"}).status_code == 403
        result = client.post("/setup", data={"csrf": token, "token": "wrong",
                                             "username": "管理员", "password": PASSWORD})
        assert result.status_code == 400
        assert app.state.auth.admin() is None
        result = client.post("/setup", data={"csrf": token,
                                             "token": app.state.auth.bootstrap_path.read_text(),
                                             "username": "管理员", "password": PASSWORD})
        assert result.status_code == 200
        assert not app.state.auth.bootstrap_path.exists()
        stored = app.state.auth.admin()["password_hash"]
        assert PASSWORD not in stored and verify_password(PASSWORD, stored)
        token = csrf(client.get("/setup"))
        assert client.post("/setup", data={"csrf": token, "token": "anything"}).status_code == 400


def test_favicon_and_second_login_tab_do_not_invalidate_login_form(web):
    client, _, _ = web
    post(client, "/logout")
    original = csrf(client.get("/login"))
    assert client.get("/favicon.ico").status_code == 200
    assert csrf(client.get("/login")) == original
    result = client.post("/login", data={"csrf": original, "username": "管理员", "password": PASSWORD})
    assert result.status_code == 200 and "推送队列" in result.text


def test_dashboard_crud_templates_and_credentials_never_echo(web):
    client, svc, _ = web
    assert post(client, "/bots/save", name="测试群", webhook=WEBHOOK,
                secret="TOPSECRET").status_code == 200
    assert post(client, "/sources/save", name="技术源", url="https://example.org/feed",
                interval="60").status_code == 200
    assert post(client, "/rules/save", name="AI <script>alert(1)</script>",
                includes="AI\n机器学习", excludes="广告", mode="any", bot_id="1",
                source_ids="1").status_code == 200
    for path in ["/", "/sources", "/sources?edit=1", "/rules", "/rules?edit=1",
                 "/bots", "/bots?edit=1", "/history", "/history?state=&bot_id=",
                 "/settings", "/operations"]:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert "TOPSECRET" not in response.text and "web-secret-token" not in response.text
    assert "&lt;script&gt;" in client.get("/rules").text
    assert post(client, "/rules/1/action", operation="test", title="ＡＩ 消息").status_code == 200
    assert not svc.history()
    assert post(client, "/bots/1/action", operation="test").status_code == 200
    assert len(svc.history()) == 1
    assert "测试消息" in client.get("/history").text
    assert post(client, "/bots/1/action", operation="delete").status_code == 400
    assert post(client, "/bots/save", id="1", name="重命名", webhook="", secret="").status_code == 200
    assert svc.bot_list()[0]["name"] == "重命名"
    assert post(client, "/rules/1/action", operation="disable").status_code == 200
    assert svc.rule_list()[0]["enabled"] == 0
    assert client.post("/sources/1/action", data={"operation": "delete"}).status_code == 403
    assert len(svc.source_list()) == 1


def test_session_expiration_logout_password_change_and_rate_limit(web):
    client, _, auth = web
    original = client.cookies.get("rss_session")
    assert auth.authenticated(original)
    assert post(client, "/settings/password", current=PASSWORD, password="changed-password-123",
                confirm_password="changed-password-123").status_code == 200
    assert not auth.authenticated(original)
    assert login(client, password=PASSWORD).status_code == 400
    assert login(client, password="changed-password-123").status_code == 200
    current = client.cookies.get("rss_session")
    assert post(client, "/logout").status_code == 200
    assert not auth.authenticated(current)
    for _ in range(10):
        assert login(client, password="wrong").status_code == 400
    assert "登录尝试过多" in login(client, password="changed-password-123").text


def test_settings_api_validates_atomically_and_controls_persist(web):
    client, svc, _ = web
    token = client.get("/api/session/csrf").json()["csrf"]
    headers = {"x-csrf-token": token}
    assert client.post("/api/settings", json={"default_interval": 60}).status_code == 403
    result = client.post("/api/settings", json={"default_interval": 60, "max_sources": 1}, headers=headers)
    assert result.status_code == 200
    result = client.post("/api/settings", json={"default_interval": 120, "request_deadline": 0}, headers=headers)
    assert result.status_code == 400 and read_settings(svc.db)["default_interval"] == 60
    assert post(client, "/sources/save", name="one", url="https://example.org/one").status_code == 200
    assert svc.source_list()[0]["interval"] == 60
    assert post(client, "/sources/save", name="two", url="https://example.org/two").status_code == 400
    assert post(client, "/operations/control", key="delivery_paused", paused="1").status_code == 200
    assert read_settings(svc.db)["delivery_paused"]
    form_values = {k: str(v) for k, v in read_settings(svc.db).items()
                   if k not in {"fetch_paused", "delivery_paused"}}
    form_values["timezone"] = "UTC"
    assert post(client, "/settings/save", **form_values).status_code == 200
    assert read_settings(svc.db)["timezone"] == "UTC"
    assert read_settings(svc.db)["delivery_paused"]
    assert client.put("/api/sources/1", json={"name": "renamed", "interval": 120}, headers=headers).status_code == 200
    assert svc.source_list()[0]["name"] == "renamed"


def test_online_backup_download_restore_preserves_identity_and_login(web):
    client, svc, auth = web
    bot = svc.bot_save("群", WEBHOOK, "SECRET")
    source = svc.source_save("原始源", "https://example.org/original")
    svc.bot_test(bot)
    backup = create_backup(svc)
    response = client.get(f"/operations/backups/{backup.name}/download")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "rss-watch.sqlite3" in archive.namelist()
        assert not any("session-key" in p for p in archive.namelist())
    svc.source_delete(source)
    temporary_bot = svc.bot_save("临时群", WEBHOOK)
    temporary_ref = svc.db.rows("SELECT credential_ref FROM bots WHERE id=?", (temporary_bot,))[0]["credential_ref"]
    assert not svc.source_list()
    old_token = client.cookies.get("rss_session")
    response = client.post("/operations/restore", data={
        "csrf": csrf(client.get("/")), "confirmation": "恢复"
    }, files={"archive": ("backup.zip", response.content, "application/zip")})
    assert response.status_code == 200, response.text
    assert (svc.config.data_dir / "maintenance").exists()
    assert svc.source_list()[0]["id"] == source
    assert len(svc.history()) == 1
    assert not (svc.config.credentials_dir / temporary_ref).exists()
    assert not auth.authenticated(old_token)
    assert login(client).status_code == 200
    assert post(client, "/sources/save", name="blocked", url="https://example.org/block").status_code == 409
    assert post(client, "/operations/resume").status_code == 200
    assert not (svc.config.data_dir / "maintenance").exists()
    assert read_settings(svc.db)["fetch_paused"] and read_settings(svc.db)["delivery_paused"]
    assert len(list((svc.config.data_dir / "backups").glob("pre-restore-*"))) == 1


def test_rule_edit_requires_confirmation_to_broaden_scope(web):
    client, svc, _ = web
    bot = svc.bot_save("群", WEBHOOK)
    source = svc.source_save("源", "https://example.org/feed")
    rule = svc.rule_save("规则", ["AI"], [], "any", [source], bot)
    svc.source_delete(source)
    assert post(client, "/rules/save", id=str(rule), name="改名", includes="AI",
                excludes="", mode="any", bot_id=str(bot)).status_code == 400
    assert post(client, "/rules/save", id=str(rule), name="改名", includes="AI",
                excludes="", mode="any", bot_id=str(bot), all_sources="1").status_code == 200
    headers = {"x-csrf-token": client.get("/api/session/csrf").json()["csrf"]}
    assert client.get("/api/rules").json()[0]["includes"] == ["AI"]
    result = client.post(f"/api/rules/{rule}/test", json={"title": "AI 新闻"}, headers=headers)
    assert result.json() == {"matched": True, "keywords": ["AI"]}


def test_api_retry_keeps_event_id(web):
    client, svc, _ = web
    bot = svc.bot_save("群", WEBHOOK)
    event = svc.bot_test(bot)
    with svc.db.transaction() as db:
        db.execute("UPDATE deliveries SET state='failed',attempts=5 WHERE id=?", (event,))
    headers = {"x-csrf-token": client.get("/api/session/csrf").json()["csrf"]}
    result = client.post(f"/api/deliveries/{event}/retry", headers=headers)
    assert result.status_code == 200 and result.json() == {"ok": True}
    assert svc.history()[0]["id"] == event
    assert svc.history()[0]["state"] == "pending" and svc.history()[0]["attempts"] == 0


@pytest.mark.parametrize("bad_name", ["../../outside", "/tmp/outside", "credentials/../outside.json"])
def test_restore_rejects_zip_path_escape_without_touching_live_data(web, tmp_path, bad_name):
    client, svc, _ = web
    svc.source_save("保留", "https://example.org/keep")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(bad_name, "bad")
    response = client.post("/operations/restore", data={
        "csrf": csrf(client.get("/")), "confirmation": "恢复"
    }, files={"archive": ("bad.zip", archive.getvalue(), "application/zip")})
    assert response.status_code == 400
    assert svc.source_list()[0]["name"] == "保留"
    assert not (svc.config.data_dir / "maintenance").exists()


def test_restore_missing_credentials_does_not_enter_maintenance(web, tmp_path):
    client, svc, _ = web
    svc.bot_save("群", WEBHOOK)
    backup = create_backup(svc)
    for path in (backup / "credentials").iterdir():
        path.unlink()
    archive = tmp_path / "bad.zip"
    zip_backup(backup, archive)
    response = client.post("/operations/restore", data={
        "csrf": csrf(client.get("/")), "confirmation": "恢复"
    }, files={"archive": ("bad.zip", archive.read_bytes(), "application/zip")})
    assert response.status_code == 400
    assert len(svc.bot_list()) == 1 and not (svc.config.data_dir / "maintenance").exists()
