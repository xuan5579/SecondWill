"""SSE 实时通道（P3-b）。

为什么选 SSE 而不是 WebSocket：

本系统的服务端→客户端流量全是**单向**的（状态变了、宠物情绪变了、干预到来了），
没有需要客户端高频上行的内容。SSE 天生单向，且自带断线重连、
走标准 HTTP（不需要额外的握手与网关配置），实现成本约为 WebSocket 的一半。

选用的另一个理由是**降级路径简单**：SSE 连不上时，前端退化成轮询即可，
两者拿到的数据完全一样（都是 `/api/state` 的全量快照）。
WebSocket 一旦连不上，降级方案要自己造。
"""

from __future__ import annotations

import asyncio
import queue as queue_mod
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.constants import SSE_DISCONNECT_CHECK_SEC, SSE_HEARTBEAT_SEC
from app.core.logging import get_logger
from app.services.events import get_bus

logger = get_logger(__name__)
router = APIRouter(prefix="/stream", tags=["stream"])


def _frame(event: str, data: str) -> bytes:
    """拼一个 SSE 帧。data 必须是单行 JSON —— SSE 报文以空行分隔，多行会截断。"""
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


@router.get("")
async def stream_events(request: Request) -> StreamingResponse:
    """订阅状态变化。只推"变了"的信号，不推具体数据。

    客户端收到后自行拉取 `/api/state`。这样事件体极小，
    也不存在"推送的数据与前端拉取的不一致"这种最难排查的问题。
    """
    bus = get_bus()
    q = bus.subscribe()

    async def gen():
        last_beat = time.monotonic()
        try:
            yield _frame("hello", '{"status":"connected"}')
            while True:
                # 客户端已断开时立刻收摊。
                # 这个检查必须高频执行，否则 SQLite 队列的阻塞读会把
                # "检测到断连"推迟到下一次心跳超时 —— 实测会留下最多 15 秒的订阅泄漏。
                if await request.is_disconnected():
                    break
                try:
                    # 短超时，让上面的断连检查能高频进行
                    event_type, payload = await asyncio.to_thread(
                        q.get, True, SSE_DISCONNECT_CHECK_SEC
                    )
                except queue_mod.Empty:
                    if time.monotonic() - last_beat >= SSE_HEARTBEAT_SEC:
                        # 注释帧：保持连接活跃，不触发客户端的 message 事件
                        last_beat = time.monotonic()
                        yield b": keepalive\n\n"
                    continue
                yield _frame(event_type, payload)
        finally:
            bus.unsubscribe(q)
            logger.debug("SSE 连接结束，剩余订阅 %d", bus.subscriber_count)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁止 nginx 缓冲，否则事件会被攒着不发
        },
    )


@router.get("/status")
def stream_status() -> dict:
    """当前 SSE 订阅数。

    主要用于 P3-b 的验收演示：**打开的标签页数应与这个数字一致**，
    也让"订阅是否正常释放"变得肉眼可查（不断 F5 却持续上涨就是泄漏）。
    """
    return {"subscribers": get_bus().subscriber_count}
