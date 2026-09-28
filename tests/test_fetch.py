import asyncio
import json
import sqlite3
import time

import httpx
import pytest

from rss_watch.worker import Worker

from .conftest import rss


async def test_baseline_dedup_rule_merge_and_restart(configured):
    svc, bot, source, _ = configured
    body = rss(("old", "AI 历史"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))) as client:
        worker = Worker(svc.config, client)
        await worker.fetch(svc.source_list()[0])
        assert not svc.history()
        svc.rule_save("另一规则", ["研究"], [], "any", [source], bot)
        body = rss(("old", "AI 修改历史"), ("new", "AI 研究新文章"))
        await worker.fetch(svc.source_list()[0])
        assert len(svc.history()) == 1
        assert len(json.loads(svc.history()[0]["matches"])) == 2
        event = svc.history()[0]["id"]
        worker = Worker(svc.config, client)
        await worker.fetch(svc.source_list()[0])
        assert [d["id"] for d in svc.history()] == [event]
        svc.rule_save("不重放", ["AI"], [], "any", [], bot)
        await worker.fetch(svc.source_list()[0])
        assert len(svc.history()) == 1


async def test_validators_and_304(configured):
    svc, _, _, _ = configured
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, content=rss(), headers={"ETag": '"v1"', "Last-Modified": "yesterday"})
        return httpx.Response(304)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = Worker(svc.config, client)
        await worker.fetch(svc.source_list()[0])
        await worker.fetch(svc.source_list()[0])
    assert requests[1].headers["If-None-Match"] == '"v1"'
    assert requests[1].headers["If-Modified-Since"] == "yesterday"
    assert svc.source_list()[0]["etag"] == '"v1"'
    assert not svc.history()


async def test_only_title_keywords_enqueue(configured):
    svc, bot, source, rule = configured
    svc.rule_save("bero", ["bero"], [], "any", [source], bot, rule)
    body = rss()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=body)
    )) as client:
        worker = Worker(svc.config, client)
        await worker.fetch(svc.source_list()[0])
        body = rss(("unrelated", "Delux 有一个ip段上游不再提供了")).replace(
            "技术研究".encode(),
            b"ImportantNetworkUpdate-IPv4AddressReplacementWearecontactingyou"
            b"regardinganetworkissuethathasaffectedalimitednumberofourVPSservicestoday",
        )
        await worker.fetch(svc.source_list()[0])
        assert not svc.history()
        body = rss(("summary-only", "VPS 优惠")).replace("技术研究".encode(), b"BERO VPS")
        await worker.fetch(svc.source_list()[0])
        assert not svc.history()
        body = rss(("relevant", "BERO VPS 优惠"))
        await worker.fetch(svc.source_list()[0])
        assert len(svc.history()) == 1
        assert json.loads(svc.history()[0]["matches"])[0]["keywords"] == ["bero"]


@pytest.mark.parametrize("response", [httpx.Response(304), httpx.Response(500),
                                       httpx.Response(200, content=b"broken"),
                                       httpx.Response(200, content=b"x" * (5 * 1024 * 1024 + 1))])
async def test_failure_does_not_initialize(configured, response):
    svc, _, _, _ = configured
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as client:
        await Worker(svc.config, client).fetch(svc.source_list()[0])
    source = svc.source_list()[0]
    assert source["initialized"] == 0
    assert source["failures"] == 1 and source["last_error"]
    assert source["next_fetch"] >= time.time() + 290


async def test_timeout_safe_error(configured):
    svc, _, _, _ = configured
    def handler(request):
        raise httpx.ReadTimeout("secret URL must not appear")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await Worker(svc.config, client).fetch(svc.source_list()[0])
    assert "secret" not in svc.source_list()[0]["last_error"]


async def test_fetch_total_deadline(configured, monkeypatch):
    svc, _, _, _ = configured
    monkeypatch.setattr("rss_watch.worker.REQUEST_DEADLINE", 0.01)
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, content=rss())
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        await Worker(svc.config, client).fetch(svc.source_list()[0])
    assert svc.source_list()[0]["initialized"] == 0
    assert "超时" in svc.source_list()[0]["last_error"]


async def test_rollback_includes_articles_and_validators(configured):
    svc, _, _, _ = configured
    body = rss()
    etag = '"v1"'
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=body, headers={"ETag": etag})
    )) as client:
        worker = Worker(svc.config, client)
        await worker.fetch(svc.source_list()[0])
        with svc.db.transaction() as db:
            db.execute("""CREATE TRIGGER fail_delivery BEFORE INSERT ON deliveries
                          BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        body = rss(("1", "AI"))
        etag = '"v2"'
        with pytest.raises(sqlite3.IntegrityError):
            await worker.fetch(svc.source_list()[0])
        assert svc.db.rows("SELECT * FROM articles") == []
        assert svc.source_list()[0]["etag"] == '"v1"'
        with svc.db.transaction() as db:
            db.execute("DROP TRIGGER fail_delivery")
        await worker.fetch(svc.source_list()[0])
        assert len(svc.history()) == 1
        assert svc.source_list()[0]["etag"] == '"v2"'


async def test_cli_disable_discards_inflight_and_new_rules_apply(configured):
    svc, bot, source, rule = configured
    started, finish = asyncio.Event(), asyncio.Event()
    async def handler(request):
        started.set()
        await finish.wait()
        return httpx.Response(200, content=rss(("1", "AI")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = Worker(svc.config, client)
        task = asyncio.create_task(worker.fetch(svc.source_list()[0]))
        await started.wait()
        await asyncio.to_thread(svc.source_enable, source, False)
        finish.set()
        await task
        assert svc.source_list()[0]["initialized"] == 0
        svc.source_enable(source, True)
        worker.commit_feed(svc.source_list()[0], [], {}, time.time())
        await asyncio.to_thread(svc.rule_save, "改后", ["AI"], [], "all", [source], bot, rule)
        await worker.fetch(svc.source_list()[0])
        assert json.loads(svc.history()[0]["matches"])[0]["name"] == "改后"


async def test_source_scoping_and_different_bots(configured):
    svc, bot, source, rule = configured
    other = svc.source_save("另一源", "https://example.org/rss")
    bot2 = svc.bot_save("第二群", "https://open.feishu.cn/open-apis/bot/v2/hook/second")
    svc.rule_save("限定", ["AI"], [], "any", [other], bot, rule)
    svc.rule_save("第二群", ["AI"], [], "any", [source], bot2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=rss(("1", "AI")))
    )) as client:
        worker = Worker(svc.config, client)
        worker.commit_feed(svc.source_list()[0], [], {}, time.time())
        await worker.fetch(svc.source_list()[0])
    assert [d["bot_id"] for d in svc.history()] == [bot2]
