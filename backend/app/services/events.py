"""进程内事件总线（SSE 推送的数据源）。

设计要点：为什么是 `queue.Queue` 而不是 asyncio 原语。

本项目的写操作走的是**同步路由**（`def` 而非 `async def`），它们跑在
Starlette 的线程池里；而 SSE 端点跑在事件循环里。两者是不同线程，
所以事件总线必须是线程安全的，并且**发布方不能 await**。

于是选择如下：
* 订阅对象是线程安全的 `queue.Queue`；
* 发布时对队列 `put_nowait`，同步、非阻塞，不影响写请求的响应时间；
* 消费侧在 generator 里用 `asyncio.to_thread(q.get)`，把阻塞读挪出事件循环，
  不会拖住同一 loop 上的其他请求。

`put_nowait` 意味着**最坏情况是丢事件而不是卡住**——这对本项目是正确的权衡：
单个 SSE 消费者卡顿时，宁可它漏掉一次推送（前端还有轮询兜底），
也不能让"用户保存日程"这个写操作变慢乃至超时。

v1.0 是单进程本地应用，所以不需要 Redis / 消息队列；
若将来要多实例部署，替换本文件的实现即可，订阅接口保持不变。
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

Queue = queue.Queue  # type: ignore[type-arg]

# 单个订阅者最多积压的事件数。超出即丢弃最旧的 —— 慢消费者不该拖垮系统。
_MAX_QUEUE_SIZE = 64


class EventBus:
    """广播式事件总线。所有订阅者收到同一份事件副本。"""

    def __init__(self) -> None:
        self._subscribers: set[Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> Queue:
        """新建一个订阅队列并返回。务必在结束时调用 `unsubscribe` 归还。"""
        q: Queue = Queue(maxsize=_MAX_QUEUE_SIZE)
        with self._lock:
            self._subscribers.add(q)
        logger.debug("SSE 订阅 +1，当前 %d", len(self._subscribers))
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)
        logger.debug("SSE 订阅 -1，当前 %d", len(self._subscribers))

    def publish(self, event_type: str, data: Any) -> None:
        """向所有订阅者广播。同步调用，绝不阻塞。"""
        payload = json.dumps(data, ensure_ascii=False, default=str)
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait((event_type, payload))
            except queue.Full:
                # 队列满说明这个消费者长时间没读。
                # 丢掉最旧的一条腾位置——比让新事件丢失更好，
                # 因为前端拿到最新状态才是正确的（我们不传增量，只传"状态已变"）。
                try:
                    q.get_nowait()
                except queue.Empty:  # pragma: no cover - 竞态窗口极小
                    pass
                try:
                    q.put_nowait((event_type, payload))
                except queue.Full:  # pragma: no cover
                    logger.warning("SSE 事件丢弃: %s", event_type)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


_bus = EventBus()


def get_bus() -> EventBus:
    return _bus


def publish_decision(
    reason: str,
    source: str,
    decision_type: str,
    intervention_id: str | None = None,
    suggestions: list | None = None,
    intervention: dict | None = None,
    summary: str = "",
    confidence: float = 0.0,
) -> None:
    """决策结果广播（P4）。

    与 `state_changed` 不同，这里**直接携带卡片内容**：
    干预卡片是"事件本身"，让前端收到后再去拉一次/接口，
    会出现"状态已回到 normal 但卡片还在说有冲突"的错位。
    """
    _bus.publish(
        "decision",
        {
            "reason": reason,
            "source": source,
            "decision_type": decision_type,
            "intervention_id": intervention_id,
            "suggestions": suggestions or [],
            "intervention": intervention,
            "summary": summary,
            "confidence": confidence,
            # P5：能被广播出来，就说明它已经过了节奏闸门与降权检查。
            # 显式带上，前端不必再猜 —— `delivered === false` 是"没卡片"的判据。
            "delivered": True,
        },
    )


def publish_state_changed(reason: str, pet_mood: str, hard_conflicts: int) -> None:
    """状态变化的统一广播入口。

    注意：**只广播"变了"这件事，不广播具体数据**。
    前端收到后自己拉 `/api/state` 拿全量。

    这样做的好处：事件体极小、不含业务字段演进问题、
    也不会出现"SSE 推来的数据与前端自己拉的不一致"。
    代价是多一次 HTTP 请求，在本地应用里完全无所谓。
    """
    _bus.publish(
        "state_changed",
        {
            "reason": reason,
            "pet_mood": pet_mood,
            "hard_conflicts": hard_conflicts,
        },
    )
