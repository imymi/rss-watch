import asyncio
import time

import httpx

from rss_watch.worker import Worker

from .conftest import rss


async def wait_until(predicate, timeout=6):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_daemon_end_to_end_with_live_config_and_restart(configured):
    svc, _, source_id, _ = configured
    body = rss(("old", "AI 历史"))
    received = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=body)
        received.append(request.content)
        return httpx.Response(200, json={"code": 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = Worker(svc.config, client)
        task = asyncio.create_task(worker.run())
        try:
            await wait_until(lambda: svc.source_list()[0]["initialized"])
            assert not received
            assert svc.status()["后台运行"] is True
            body = rss(("old", "AI 历史"), ("new", "AI 新消息"))
            # CLI operation on a separate thread/connection triggers the next fetch.
            await asyncio.to_thread(svc.source_save, "技术源", "https://example.com/feed", 60, source_id)
            await wait_until(lambda: len(svc.history(state="sent")) == 1)
            assert len(received) == 1
        finally:
            worker.stopping.set()
            await task
        assert svc.status()["后台运行"] is False
        svc.source_save("技术源", "https://example.com/feed", 60, source_id)
        worker = Worker(svc.config, client)
        task = asyncio.create_task(worker.run())
        try:
            before = time.time()
            await wait_until(lambda: svc.source_list()[0]["last_success"] >= before)
            assert len(received) == 1
        finally:
            worker.stopping.set()
            await task


async def test_max_five_fetches_and_independent_heartbeat(service):
    for i in range(7):
        service.source_save(str(i), f"https://example.org/{i}")
    active, maximum = 0, 0
    started = asyncio.Event()
    gate = asyncio.Event()
    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 5:
            started.set()
        await gate.wait()
        active -= 1
        return httpx.Response(200, content=rss())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = Worker(service.config, client)
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(started.wait(), 3)
            heartbeat = service.status()["心跳时间"]
            await wait_until(lambda: service.status()["心跳时间"] > heartbeat)
            assert maximum == 5
            gate.set()
            await wait_until(lambda: all(row["initialized"] for row in service.source_list()))
            assert maximum <= 5
        finally:
            worker.stopping.set()
            gate.set()
            await task
