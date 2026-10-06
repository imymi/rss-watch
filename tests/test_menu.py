import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from rss_watch import cli


@pytest.fixture
def run_menu(service, monkeypatch):
    monkeypatch.setattr(cli, "service", lambda ctx: service)

    def run(*inputs):
        result = CliRunner().invoke(cli.app, ["menu"], input="\n".join(inputs) + "\n")
        assert result.exit_code == 0, (result.output, result.exception)
        return result.output

    return run


def test_source_form_retries_fields_and_stays_in_submenu(service, run_menu):
    output = run_menu(
        "bad", "1", "1", "", "技术源", "file:///tmp/feed", "https://example.org/feed",
        "abc", "59", "60", "4", "999", "1", "0", "0",
    )
    row, = service.source_list()
    assert row["name"] == "技术源" and row["interval"] == 60 and row["enabled"] == 0
    assert "无效选择" in output and "此项不能为空" in output
    assert "请输入整数" in output and "不小于 60" in output and "ID 不存在" in output
    assert "已新增订阅源" in output and "已停用" in output
    assert "首次抓取" in output and "initialized" not in output


@pytest.mark.parametrize("cancel", ["/取消", "interrupt", "eof"])
def test_partial_source_form_never_saves(service, run_menu, monkeypatch, cancel):
    if cancel == "/取消":
        output = run_menu("1", "1", "unfinished", "/取消", "0", "0")
    elif cancel == "eof":
        output = run_menu("1", "1", "unfinished")
    else:
        ask = cli.Prompt.ask

        def interrupt_url(label, **kwargs):
            if label == "RSS/Atom URL":
                raise KeyboardInterrupt
            return ask(label, **kwargs)

        monkeypatch.setattr(cli.Prompt, "ask", interrupt_url)
        output = run_menu("1", "1", "unfinished", "0", "0")
    assert service.source_list() == []
    assert "已取消当前操作" in output or "已退出管理菜单" in output


def test_rule_requires_bot_before_collecting_form(service, run_menu):
    output = run_menu("2", "1", "0", "0")
    assert "先返回主菜单" in output
    assert service.rule_list() == []


def test_rule_edit_preserves_defaults_then_clears_optional_fields(configured, run_menu):
    svc, bot, source, rule = configured
    svc.rule_save("技术动态", ["AI"], ["广告"], "all", [source], bot, rule)
    before = svc.rule_list()[0]
    output = run_menu("2", "2", str(rule), "", "", "", "", "", "", "0", "0")
    assert svc.rule_list()[0] == before
    assert "规则「技术动态」已保存" in output
    output = run_menu("2", "2", str(rule), "", "", "", "-", "1", "-", "0", "0")
    row, = svc.rule_list()
    assert row["name"] == before["name"] and row["bot_id"] == bot
    assert json.loads(row["includes"]) == ["AI"]
    assert json.loads(row["excludes"]) == [] and json.loads(row["source_ids"]) == []
    assert row["mode"] == "any"
    assert "排除词" in output and "includes" not in output


def test_rule_form_retries_invalid_references_and_words(configured, run_menu):
    svc, bot, source, _ = configured
    output = run_menu(
        "2", "1", "新规则", "999", str(bot), "， ,", "AI，研究", "", "bad", "2",
        "abc", "999", str(source), "0", "0",
    )
    row = svc.rule_list()[-1]
    assert row["name"] == "新规则" and row["bot_id"] == bot
    assert json.loads(row["includes"]) == ["AI", "研究"]
    assert json.loads(row["source_ids"]) == [source] and row["mode"] == "all"
    assert "至少输入一个包含词" in output and "存在无效订阅源 ID" in output


def test_cancel_rule_edit_preserves_existing_rule(configured, run_menu):
    svc, _, _, rule = configured
    before = svc.rule_list()
    run_menu("2", "2", str(rule), "不要保存", "", "AI", "/取消", "0", "0")
    assert svc.rule_list() == before


def test_delete_defaults_to_cancel_and_names_target(configured, run_menu):
    svc, _, source, _ = configured
    output = run_menu("1", "5", str(source), "", "0", "0")
    assert "删除订阅源「技术源」" in output and "已取消删除" in output
    assert len(svc.source_list()) == 1
    output = run_menu("1", "5", str(source), "1", "0", "0")
    assert "订阅源已删除" in output and svc.source_list() == []


def test_bot_delete_constraint_keeps_submenu_usable(configured, run_menu):
    svc, bot, _, _ = configured
    output = run_menu("3", "3", str(bot), "1", "4", str(bot), "0", "0", "0")
    assert "请先删除或改绑引用该机器人的规则" in output
    assert "已取消测试消息" in output
    assert len(svc.bot_list()) == 1 and svc.history() == []


def test_bot_test_requires_confirmation(configured, run_menu):
    svc, bot, _, _ = configured
    output = run_menu("3", "4", str(bot), "", "4", str(bot), "1", "0", "0")
    assert len(svc.history()) == 1
    assert "向机器人「测试群」发送一条测试消息" in output
    assert "已取消测试消息" in output and "测试已入队" in output


def test_cancel_hidden_credentials_preserves_bot(configured, run_menu, monkeypatch):
    svc, bot, _, _ = configured
    before = svc.db.rows("SELECT * FROM bots")
    files = {p.name: p.read_bytes() for p in svc.config.credentials_dir.iterdir()}

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "credential_input", cancel)
    run_menu("3", "2", str(bot), "不要保存", "0", "0")
    assert svc.db.rows("SELECT * FROM bots") == before
    assert {p.name: p.read_bytes() for p in svc.config.credentials_dir.iterdir()} == files


def test_menu_credentials_stay_hidden_and_preserve_secret_whitespace(monkeypatch, capsys):
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/test"
    values = iter(["invalid-secret-url", webhook, " secret with spaces "])
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def hidden_prompt(label, **kwargs):
        assert kwargs["password"] is True
        return next(values)

    monkeypatch.setattr(cli.Prompt, "ask", hidden_prompt)
    assert cli.credential_input(None, cancelable=True) == (webhook, " secret with spaces ")
    output = capsys.readouterr().out
    assert "invalid-secret-url" not in output and webhook not in output
    assert "secret with spaces" not in output


def test_failed_task_selected_by_number_and_confirmed(configured, run_menu):
    svc, bot, _, _ = configured
    event = svc.bot_test(bot)
    with svc.db.transaction() as db:
        db.execute("UPDATE deliveries SET state='failed',last_error='超时' WHERE id=?", (event,))
    output = run_menu("5", "1", "", "r", "1", "1", "0", "0")
    assert svc.history()[0]["state"] == "pending"
    assert "序号" in output and "消息内容" in output and event in output
    assert "任务已重新入队" in output and "暂无失败任务" in output


def test_stale_failed_task_does_not_retry_other_task(configured, run_menu, monkeypatch):
    svc, bot, _, _ = configured
    event = svc.bot_test(bot)
    with svc.db.transaction() as db:
        db.execute("UPDATE deliveries SET state='failed' WHERE id=?", (event,))
    retry = svc.retry

    def completed_elsewhere(event_id):
        with svc.db.transaction() as db:
            db.execute("UPDATE deliveries SET state='sent' WHERE id=?", (event_id,))
        retry(event_id)

    monkeypatch.setattr(svc, "retry", completed_elsewhere)
    output = run_menu("5", "1", "1", "0", "0")
    assert "任务不存在或不是失败状态" in output
    assert "任务已重新入队" not in output and svc.history()[0]["state"] == "sent"
