"""SSE 实时通道（P3-b）。

测试重点不是"能连上"，而是这几条容易悄悄坏掉的性质：
1. 订阅者计数准确 —— 它是判断有没有泄漏的唯一肉眼可见指标；
2. 发布不阻塞 —— 写操作绝不能被慢消费者拖住；
3. 队列满时丢最旧而非丢最新 —— 前端要的是最新状态；
4. **行为事件上报也要触发推送**（P2 时期纯事件是个死胡同）。
"""

from __future__ import annotations

import queue
import threading
import time

from fastapi.testclient import TestClient

from app.services.events import EventBus


class TestEventBus:
    """事件总线的单元行为。纯内存，不碰 HTTP。"""

    def test_subscribe_and_publish(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.publish("state_changed", {"pet_mood": "normal"})
        event_type, payload = q.get_nowait()
        assert event_type == "state_changed"
        assert '"pet_mood"' in payload

    def test_broadcasts_to_all_subscribers(self):
        bus = EventBus()
        a, b = bus.subscribe(), bus.subscribe()
        bus.publish("x", {"n": 1})
        assert a.get_nowait()[1] == '{"n": 1}'
        assert b.get_nowait()[1] == '{"n": 1}'

    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.publish("x", {"n": 1})
        assert q.empty()

    def test_publish_never_blocks_on_full_queue(self):
        """队列满时必须立即返回，绝不能阻塞写请求的线程。"""
        bus = EventBus()
        q = bus.subscribe()
        started = time.monotonic()
        # 远超队列容量
        for i in range(500):
            bus.publish("x", {"n": i})
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"publish 阻塞了 {elapsed:.2f}s"

    def test_full_queue_drops_oldest_keeps_latest(self):
        """满了要丢最旧的，而不是最新的 —— 前端关心的是最新状态。"""
        bus = EventBus()
        q = bus.subscribe()
        for i in range(100):
            bus.publish("x", {"n": i})
        last = None
        while not q.empty():
            last = q.get_nowait()[1]
        assert '"n": 99' in (last or "")

    def test_concurrent_publish_is_safe(self):
        """发布方在不同线程，模拟同步路由与后台任务的并发场景。"""
        bus = EventBus()
        q = bus.subscribe()

        def worker(base: int):
            for i in range(20):
                bus.publish("x", {"n": base * 100 + i})

        threads = [threading.Thread(target=worker, args=(b,)) for b in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert bus.subscriber_count == 1
        assert not q.empty()


class TestStreamEndpoint:
    def test_status_starts_at_zero(self, client: TestClient):
        assert client.get("/api/stream/status").json()["subscribers"] == 0

    def test_stream_emits_hello_first(self, client: TestClient):
        """连接建立后的第一帧必须是 hello —— 前端靠它判定"通道已活",
        并据此决定是否停掉轮询降级。
        """
        with client.stream("GET", "/api/stream") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            first_lines = []
            for line in resp.iter_lines():
                first_lines.append(line)
                if line == "":  # 一个完整帧以空行结束
                    break
            joined = "\n".join(first_lines)
            assert "event: hello" in joined

    def test_no_buffering_header_present(self, client: TestClient):
        """禁止中间层缓冲，否则事件会被攒着不发。"""
        with client.stream("GET", "/api/stream") as resp:
            assert resp.headers.get("x-accel-buffering") == "no"


class TestInboundWritePublishesEvent:
    """写操作必须把状态变化广播出去。"""

    def test_record_change_reaches_subscriber(self, client: TestClient):
        bus = TestEventBusHelper.client_to_bus(client)
        q = bus.subscribe()
        try:
            now = "2026-10-29T10:00:00+00:00"
            resp = client.post(
                "/api/records",
                json={"title": "推送验证", "start_at": now, "end_at": "2026-10-29T11:00:00+00:00"},
            )
            assert resp.status_code == 201, resp.text
            event_type, payload = q.get(timeout=3)
            assert event_type == "state_changed"
            assert "record_changed" in payload
        finally:
            bus.unsubscribe(q)

    def test_behavior_event_also_notifies(self, client: TestClient):
        """纯事件路径：P2 时期这里完全不联动，直到 P3-b 才发现。"""
        bus = TestEventBusHelper.client_to_bus(client)
        q = bus.subscribe()
        try:
            resp = client.post(
                "/api/events", json={"events": [{"kind": "task_delayed", "record_id": None}]}
            )
            assert resp.status_code == 201, resp.text
            event_type, payload = q.get(timeout=3)
            assert event_type == "state_changed"
            assert "events" in payload
        finally:
            bus.unsubscribe(q)


class TestEventBusHelper:
    """取到当前 app 正在用的那一个 EventBus 实例。"""

    @staticmethod
    def client_to_bus(client: TestClient):  # type: ignore[return]
        from app.services.events import get_bus

        return get_bus()
