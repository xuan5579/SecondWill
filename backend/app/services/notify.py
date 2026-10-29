"""状态变更的统一通知口。

P2 时，`_on_record_changed` 是 `records.py` 里的一个私有钩子，只有记录写操作会触发。
P3-b 引出了两个问题：

1. **行为事件上报同样改变状态**（`task_delayed` 会推高拖延指数），
   但 `POST /api/events` 当时完全不触发重算 —— 纯事件路径是个死胡同。
2. **需要一个广播点**：SSE 订阅者要在状态变化时收到信号。

于是把两者合成一个入口。任何改变系统的写操作都应该调用它，
而不是各自维护一份"联动逻辑"。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.services.events import publish_state_changed
from app.services.pet import PetState
from app.services.signal import Signals
from app.services.state import refresh_pet_snapshot

logger = get_logger(__name__)


def recompute_and_broadcast(db: Session, reason: str) -> tuple[Signals, PetState] | None:
    """重算宠物状态、落快照、并通过 SSE 广播"状态已变"。

    **绝不向上抛异常。** 注册方的写操作已经成功提交，
    联动失败只能让仪表盘暂时不准，绝不能回滚用户的保存动作。
    这是产品优先级判断，不是偷懒。
    """
    try:
        signals, state = refresh_pet_snapshot(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("状态重算失败（已忽略，不影响主流程）: %s", exc)
        return None

    hard = sum(1 for c in signals.conflicts if c.hard)
    try:
        publish_state_changed(reason, state.mood.value, hard)
    except Exception as exc:  # noqa: BLE001
        # 推送失败连"不准"都算不上，纯旁路，更不该影响主流程
        logger.warning("状态广播失败（已忽略）: %s", exc)

    logger.info(
        "状态已更新 reason=%s conflicts=%d hard=%d mood=%s",
        reason,
        len(signals.conflicts),
        hard,
        state.mood.value,
    )
    return signals, state
