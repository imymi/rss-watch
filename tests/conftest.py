import html

import pytest

from rss_watch.config import Config
from rss_watch.service import Service


@pytest.fixture
def config(tmp_path):
    value = Config(tmp_path / "data", tmp_path / "credentials")
    value.prepare()
    return value


@pytest.fixture
def service(config):
    return Service(config)


@pytest.fixture
def configured(service):
    bot = service.bot_save("测试群", "https://open.feishu.cn/open-apis/bot/v2/hook/test", "secret")
    source = service.source_save("技术源", "https://example.com/feed")
    rule = service.rule_save("技术动态", ["AI"], ["广告"], "any", [], bot)
    return service, bot, source, rule


def rss(*items):
    entries = []
    for id, title in items:
        entries.append(f"<item><guid>{html.escape(id)}</guid><title>{html.escape(title)}</title>"
                       f"<link>https://example.com/{html.escape(id)}</link>"
                       "<description>技术研究</description></item>")
    return ("<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel>"
            "<title>测试</title><link>https://example.com</link><description>测试</description>"
            + "".join(entries) + "</channel></rss>").encode()
