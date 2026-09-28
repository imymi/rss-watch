import json
import sqlite3
import stat
import time

import pytest
from typer.testing import CliRunner

from rss_watch.cli import app
from rss_watch.config import file_lock, read_credential, validate_url
from rss_watch.db import Database
from rss_watch.domain import parse_feed
from rss_watch.worker import Worker

from .conftest import rss


def test_validation_and_crud(configured):
    svc, bot, source, rule = configured
    with pytest.raises(ValueError):
        svc.source_save("bad", "https://example.com/a", 59)
    with pytest.raises(sqlite3.IntegrityError):
        svc.source_save("duplicate", "https://example.com/feed")
    with pytest.raises(ValueError):
        svc.rule_save("bad", [], [], "any", [], bot)
    with pytest.raises(ValueError):
        svc.rule_save("bad", ["AI"], [], "any", [9999], bot)
    with pytest.raises(ValueError):
        svc.bot_delete(bot)
    svc.source_save("new", "https://example.com/feed", 60, source)
    assert svc.source_list()[0]["name"] == "new"
    svc.rule_enable(rule, False)
    assert svc.rule_list()[0]["enabled"] == 0
    svc.rule_delete(rule)
    svc.source_delete(source)
    assert not svc.source_list()
    svc.bot_delete(bot)
    assert not svc.bot_list()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:pass@example.org/a",
                                  "https://open.feishu.cn.evil.test/open-apis/bot/v2/hook/a",
                                  "http://open.feishu.cn/open-apis/bot/v2/hook/a"])
def test_webhook_validation(url):
    with pytest.raises(ValueError):
        validate_url(url, webhook=True)


def test_credentials_and_backup(configured, tmp_path):
    svc, bot, _, _ = configured
    ref = svc.db.rows("SELECT credential_ref FROM bots")[0]["credential_ref"]
    assert stat.S_IMODE((svc.config.credentials_dir / ref).stat().st_mode) == 0o600
    assert read_credential(svc.config, ref)["secret"] == "secret"
    assert "secret" not in json.dumps(svc.bot_list())
    svc.bot_test(bot)
    target = tmp_path / "backup"
    svc.backup(target)
    assert (target / "credentials" / ref).is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert Database(target / "rss-watch.sqlite3").rows("SELECT * FROM deliveries")
    # Online backup remains independently readable after the live DB changes.
    svc.bot_test(bot)
    assert len(Database(target / "rss-watch.sqlite3").rows("SELECT * FROM deliveries")) == 1
    with pytest.raises(FileExistsError):
        svc.backup(target)


def test_cleanup_keeps_fingerprints_and_failed_history(configured):
    svc, bot, _, _ = configured
    worker = Worker(svc.config, None)
    old = time.time() - 100 * 86400
    worker.commit_feed(svc.source_list()[0], [], {}, old)
    worker.commit_feed(svc.source_list()[0], parse_feed(rss(("1", "AI"), ("2", "AI"))), {}, old)
    items = svc.history()
    with svc.db.transaction() as db:
        db.execute("UPDATE deliveries SET state='sent' WHERE id=?", (items[0]["id"],))
        db.execute("UPDATE deliveries SET state='failed' WHERE id=?", (items[1]["id"],))
    svc.db.cleanup(time.time())
    assert len(svc.history()) == 1 and svc.history()[0]["state"] == "failed"
    articles = svc.db.rows("SELECT * FROM articles")
    assert len(articles) == 2 and sum(a["title"] is None for a in articles) == 1
    worker.commit_feed(svc.source_list()[0], parse_feed(rss(("1", "AI"), ("2", "AI"))), {}, time.time())
    assert len(svc.history()) == 1


def test_process_lock(config):
    with file_lock(config.data_dir / "daemon.lock", blocking=False):
        with pytest.raises(ValueError, match="已运行"):
            with file_lock(config.data_dir / "daemon.lock", blocking=False):
                pass


def test_cli_init_menu_and_commands(tmp_path):
    runner = CliRunner()
    config = tmp_path / "config.toml"
    prefix = ["--config", str(config)]
    result = runner.invoke(app, [*prefix, "init", "--data-dir", str(tmp_path / "data"),
                                 "--credentials-dir", str(tmp_path / "credentials")])
    assert result.exit_code == 0, result.output
    for command in (["--help"], ["source", "--help"], ["rule", "add", "--help"],
                    ["menu"], ["status"]):
        result = runner.invoke(app, [*prefix, *command], input="0\n")
        assert result.exit_code == 0, (result.output, result.exception)
    credential = tmp_path / "input.json"
    credential.write_text(json.dumps({"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
                                      "secret": "TOPSECRET"}))
    credential.chmod(0o600)
    commands = [
        ["bot", "add", "飞书", "--credential-file", str(credential)],
        ["source", "add", "源", "https://example.org/feed"],
        ["rule", "add", "AI", "--bot", "1", "--include", "AI", "--source", "1"],
        ["rule", "test", "1", "ＡＩ"],
        ["source", "update", "1", "--interval", "60"],
        ["rule", "update", "1", "新规则", "--bot", "1", "--include", "研究"],
        ["bot", "test", "1"],
        ["source", "list", "--json"],
        ["rule", "list", "--json"],
        ["bot", "list", "--json"],
        ["history", "--json"],
        ["backup", str(tmp_path / "backup")],
    ]
    for command in commands:
        result = runner.invoke(app, [*prefix, *command])
        assert result.exit_code == 0, (command, result.output, result.exception)
        assert "TOPSECRET" not in result.output


def test_menu_crud_and_test_rule(tmp_path, monkeypatch):
    runner = CliRunner()
    prefix = ["--config", str(tmp_path / "config.toml")]
    runner.invoke(app, [*prefix, "init", "--data-dir", str(tmp_path / "data"),
                        "--credentials-dir", str(tmp_path / "creds")])
    # Bot, source, rule, rule sample, status, exit: exercises the real numbered menus.
    monkeypatch.setattr("rss_watch.cli.credential_input", lambda _: (
        "https://open.feishu.cn/open-apis/bot/v2/hook/test", "",
    ))
    inputs = ["3", "1", "群",
              "1", "1", "技术源", "https://example.org/feed", "300",
              "2", "1", "AI", "1", "AI", "广告", "any", "1",
              "2", "6", "1", "AI 新消息", "", "6", "0"]
    result = runner.invoke(app, [*prefix, "menu"], input="\n".join(inputs) + "\n")
    assert result.exit_code == 0, (result.output, result.exception)
    assert "命中：AI" in result.output


@pytest.mark.parametrize("running,heartbeat,online", [
    ("1", 995, True), ("1", 990, False), ("0", 995, False),
])
def test_status_heartbeat_boundary(service, monkeypatch, running, heartbeat, online):
    monkeypatch.setattr("rss_watch.service.time.time", lambda: 1000)
    with service.db.transaction() as db:
        db.executemany("INSERT INTO runtime(key,value) VALUES (?,?)",
                       [("running", running), ("heartbeat", str(heartbeat))])
    assert service.status()["后台运行"] is online


def test_status_summary_detail_and_json(configured, monkeypatch):
    svc, bot, source, _ = configured
    monkeypatch.setattr("rss_watch.cli.service", lambda ctx: svc)
    runner = CliRunner()
    empty = runner.invoke(app, ["status"])
    assert empty.exit_code == 0, empty.exception
    assert "后台：离线" in empty.output
    assert "最近心跳：尚无记录" in empty.output
    assert "待发送 0" in empty.output
    assert "--detail" not in empty.output
    event = svc.bot_test(bot)
    with svc.db.transaction() as db:
        db.executemany("INSERT INTO runtime(key,value) VALUES (?,?)",
                       [("running", "1"), ("heartbeat", str(time.time()))])
        db.execute("UPDATE sources SET last_error='抓取超时' WHERE id=?", (source,))
        db.execute("UPDATE deliveries SET state='failed',last_error='推送超时' WHERE id=?",
                   (event,))
    summary = runner.invoke(app, ["status"])
    assert summary.exit_code == 0, summary.exception
    assert "后台：在线" in summary.output
    assert "失败 1" in summary.output
    assert "--detail" in summary.output
    assert "抓取超时" not in summary.output
    assert event not in summary.output
    detail = runner.invoke(app, ["status", "--detail"])
    assert detail.exit_code == 0, detail.exception
    assert "抓取超时" in detail.output and "推送超时" in detail.output
    raw = runner.invoke(app, ["status", "--json"])
    assert raw.exit_code == 0, raw.exception
    assert json.loads(raw.output) == svc.status()
    menu = runner.invoke(app, ["menu"], input="6\n0\n")
    assert menu.exit_code == 0, menu.exception
    assert "后台：在线" in menu.output and "失败 1" in menu.output
    assert '"后台运行"' not in menu.output
