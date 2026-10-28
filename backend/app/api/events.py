"""行为事件上报。

事件流是 LLM 决策器的唯一输入来源。
v1.0 由前端在用户操作时上报；"逾期未开始"类事件由后端定时扫描补发（P5）。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import get_db
from app.llm.orchestrator import should_trigger
from app.models.event import EVENT_KINDS, BehaviorEvent
from app.schemas.event import EventBatchCreate, EventOut
from app.services.decision_flow import run_decision_bg
from app.services.notify import recompute_and_broadcast

logger = get_logger(__name__)
router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=list[EventOut], status_code=201)
def create_events(
    payload: EventBatchCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> list[EventOut]:
    created: list[BehaviorEvent] = []
    for item in payload.events:
        if item.kind not in EVENT_KINDS:
            # 未知事件类型直接拒绝，避免脏数据污染后续信号聚合
            raise HTTPException(status_code=400, detail=f"未知事件类型: {item.kind}")
        event = BehaviorEvent(
            id=new_id("evt_"),
            kind=item.kind,
            record_id=item.record_id,
            at=item.at or utcnow(),
            payload=item.payload,
        )
        db.add(event)
        created.append(event)

    db.commit()
    for e in created:
        db.refresh(e)
    logger.info("写入 %d 条行为事件", len(created))

    # 事件流改变信号（例如 task_delayed 推高拖延指数），因此同样要联动重算。
    # P2 时期这里没有任何联动 —— 纯事件路径是个死胡同，
    # 用户推迟一次任务，宠物却毫无反应。
    if created:
        outcome = recompute_and_broadcast(db, reason=f"events:{len(created)}")
        # task_delayed 之类的事件会推高拖延指数，达阈值时同样值得问一次模型
        if outcome is not None and should_trigger(outcome[0], "procrastination_signal")[0]:
            background_tasks.add_task(run_decision_bg, "procrastination_signal")

    return [EventOut.from_orm_model(e) for e in created]


@router.get("", response_model=list[EventOut])
def list_events(
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[EventOut]:
    q = db.query(BehaviorEvent)
    if kind:
        q = q.filter(BehaviorEvent.kind == kind)
    rows = q.order_by(BehaviorEvent.at.desc()).limit(limit).all()
    return [EventOut.from_orm_model(r) for r in rows]
