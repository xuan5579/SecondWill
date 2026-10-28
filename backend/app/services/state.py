"""有状态计算的编排层（唯一的 IO 边界）。

`services/conflict.py` / `signal.py` / `pet.py` 全部是纯函数，不碰数据库。
本模块负责：读取 → 调用纯函数 → 落库。

**这条分层不是洁癖。** 纯函数可以被单测完整覆盖、可以脱离 Web 运行（用户明确要求），
而一旦把 Session 混进去，所有测试都得先造 SQLite，反馈环路从毫秒变成秒级。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.models.event import BehaviorEvent
from app.models.pet_snapshot import PetSnapshot
from app.models.record import Record
from app.services.pet import PetState, compute, template_line
from app.services.signal import Signals, aggregate

#: 单次计算拉取的记录 / 事件上限。单用户 v1.0 场景下够用，
#: 且不设上限会让一条失控的数据把整个请求拖垮。
MAX_RECORDS = 1000
MAX_EVENTS = 5000


def load_inputs(
    db: Session,
    limit_records: int = MAX_RECORDS,
    limit_events: int = MAX_EVENTS,
) -> tuple[list[Record], list[BehaviorEvent]]:
    records = db.query(Record).order_by(Record.start_at).limit(limit_records).all()
    events = (
        db.query(BehaviorEvent)
        .order_by(BehaviorEvent.at.desc())
        .limit(limit_events)
        .all()
    )
    return list(records), list(events)


def compute_state(
    db: Session, now: datetime | None = None
) -> tuple[Signals, PetState]:
    """读取全量数据并算出当前信号与宠物状态。不写库。"""
    records, events = load_inputs(db)
    now = now or utcnow()
    signals = aggregate(records, events, now)
    state = compute(records, events, signals, now)
    return signals, state


def refresh_pet_snapshot(
    db: Session, now: datetime | None = None, persist: bool = True
) -> tuple[Signals, PetState]:
    """重算宠物状态并落一份快照。记录变更后调用。

    快照只用于回看趋势与演示，**不参与计算**——每次都从头重算，
    这样任何历史脏快照都不会影响当前情绪。
    """
    signals, state = compute_state(db, now=now)
    if persist:
        seed = sum(ord(c) for c in state.mood.value)
        db.add(
            PetSnapshot(
                mood=state.mood.value,
                energy=state.energy,
                streak_days=state.streak_days,
                line=template_line(state, seed=seed),
                computed_at=state.computed_at,
            )
        )
        db.commit()
    return signals, state
