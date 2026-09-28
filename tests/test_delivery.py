import asyncio
import base64
import hashlib
import hmac
import json

import httpx
import pytest

from rss_watch.worker import DeliveryError, Worker, retry_after, send_feishu, signature

CREDENTIAL = {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test", "secret": "secret"}


def test_signature_vector_and_retry_after():
    expected = base64.b64encode(hmac.new(b"1599360473\nsecret", b"", hashlib.sha256).digest()).decode()
    assert signature("1599360473", "secret") == expected
    assert retry_after("120", 0) == 120
    assert retry_after("Thu, 01 Jan 1970 00:02:00 GMT", 60) == 60
    assert retry_after("invalid", 0) == 0
    assert retry_after("-3", 0) == 0


async def test_signed_payload_and_business_success():
    payloads = []
    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await send_feishu(client, CREDENTIAL, "hello")
    payload = payloads[0]
    assert payload["sign"] == signature(payload["timestamp"], "secret")
    assert payload["msg_type"] == "text" and payload["content"]["text"] == "hello"


@pytest.mark.parametrize("status,body,transient", [
    (200, {"code": 19021, "msg": "secret echo"}, False),
    (200, {"code": 11232}, True),
    (200, {"code": 9499}, False),
    (200, {"code": 5000}, True),
    (200, {"code": 99991500}, True),
    (200, {}, True),
    (200, {"code": []}, True),
    (200, {"code": False}, True),
    (200, [], True),
    (429, {"secret": "echo"}, True),
    (503, {}, True),
    (403, {}, False),
    (302, {}, False),
])
async def test_error_classification(status, body, transient):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json=body, headers={"Retry-After": "120"})
    )) as client:
        with pytest.raises(DeliveryError) as error:
            await send_feishu(client, CREDENTIAL, "text")
    assert error.value.transient is transient
    assert "secret" not in str(error.value)


async def test_delivery_timeouts():
    def handler(request):
        raise httpx.ReadTimeout("webhook=secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DeliveryError, match="网络"):
            await send_feishu(client, CREDENTIAL, "text")


async def test_delivery_total_deadline(monkeypatch):
    monkeypatch.setattr("rss_watch.worker.REQUEST_DEADLINE", 0.01)
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"code": 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        with pytest.raises(DeliveryError, match="超时"):
            await send_feishu(client, CREDENTIAL, "test")


async def test_queue_retry_exhaustion_and_manual_retry(configured, monkeypatch):
    svc, bot, _, _ = configured
    clock = [1000.0]
    monkeypatch.setattr("rss_watch.worker.time.time", lambda: clock[0])
    event = svc.bot_test(bot)
    code = 503
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(code, json={"code": 0})
    )) as client:
        worker = Worker(svc.config, client)
        for attempt, delay in enumerate((30, 120, 600, 1800, 1800), start=1):
            assert await worker.deliver_one()
            item = svc.history()[0]
            assert item["attempts"] == attempt
            assert item["next_attempt"] == clock[0] + delay
            assert item["state"] == ("failed" if attempt == 5 else "pending")
            assert not await worker.deliver_one()
            clock[0] += delay
        svc.retry(event)
        code = 200
        assert await worker.deliver_one()
        assert svc.history()[0]["state"] == "sent"
        assert svc.history()[0]["attempts"] == 1
        assert svc.history()[0]["id"] == event


async def test_rate_limit_persistent_across_restart(configured, monkeypatch):
    svc, bot, _, _ = configured
    clock = [1000.0]
    monkeypatch.setattr("rss_watch.worker.time.time", lambda: clock[0])
    svc.bot_test(bot)
    svc.bot_test(bot)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "120"})
    )) as client:
        worker = Worker(svc.config, client)
        assert await worker.deliver_one()
        assert not await worker.deliver_one()
        worker = Worker(svc.config, client)
        worker.recover()
        clock[0] += 119
        assert not await worker.deliver_one()
        clock[0] += 1
        assert await worker.deliver_one()


async def test_success_spacing_and_missing_credentials(configured, monkeypatch):
    svc, bot, _, _ = configured
    clock = [1000.0]
    monkeypatch.setattr("rss_watch.worker.time.time", lambda: clock[0])
    svc.bot_test(bot)
    svc.bot_test(bot)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"StatusCode": 0})
    )) as client:
        worker = Worker(svc.config, client)
        assert await worker.deliver_one()
        assert not await worker.deliver_one()
        clock[0] += 1
        ref = svc.db.rows("SELECT credential_ref FROM bots")[0]["credential_ref"]
        (svc.config.credentials_dir / ref).chmod(0o644)
        assert await worker.deliver_one()
        failed = svc.history(state="failed")[0]
        assert failed["attempts"] == 1 and "0600" in failed["last_error"]


async def test_crash_recovery_and_max_attempt(configured):
    svc, bot, _, _ = configured
    first = svc.bot_test(bot)
    second = svc.bot_test(bot)
    with svc.db.transaction() as db:
        db.execute("UPDATE deliveries SET state='sending', attempts=1 WHERE id=?", (first,))
        db.execute("UPDATE deliveries SET state='sending', attempts=5 WHERE id=?", (second,))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"code": 0})
    )) as client:
        worker = Worker(svc.config, client)
        worker.recover()
        assert svc.history(state="failed")[0]["id"] == second
        assert await worker.deliver_one()
        sent = svc.history(state="sent")[0]
        assert sent["id"] == first and sent["attempts"] == 2
