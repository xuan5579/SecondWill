"""记录 CRUD。

v1.0 单用户、无鉴权。
记录变更是整条控制闭环的起点：改日程 → 冲突检测 → LLM 决策 → 干预。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.session import get_db
from app.models.record import RECORD_STATUSES, RECORD_TYPES, Record
from app.schemas.record import RecordCreate, RecordOut, RecordUpdate
from app.llm.orchestrator import should_trigger
from app.services.decision_flow import run_decision_bg
from app.services.notify import recompute_and_broadcast

logger = get_logger(__name__)
router = APIRouter(prefix="/records", tags=["records"])


def _first_error(exc: ValidationError) -> str:
    """取出第一条对人类可读的校验错误。

    FastAPI 只自动处理请求体的校验，路由内部构造 model 抛出的错误会变成 500，
    所以这里手动转成 400 并取第一条消息。
    """
    for err in exc.errors():
        msg = err.get("msg", "请求参数不合法")
        loc = err.get("loc") or ()
        return f"{'.'.join(str(p) for p in loc)}: {msg}" if loc else msg
    return "请求参数不合法"


def _on_record_changed(
    db: Session,
    record: Record | str,
    background_tasks: BackgroundTasks | None = None,
) -> None:
    """记录变更后的联动钩子 —— 控制闭环的起点。

    P2（已完成）：改日程 → 冲突检测 → 信号聚合 → 宠物状态重算并落快照。
    P3-b：状态变化经 SSE 广播给在线页面。
    P4（本期）：同步部分结束后，**异步**起一次 LLM 决策。

    两条链路严格分离：
      * 宠物情绪走**同步快路径** —— 仪表盘不等模型（文档 §5.3）；
      * 决策走**后台任务** —— 模型要几秒，绝不能让用户等保存按钮转圈。
    """
    # 删除场景下对象已提交并从会话中脱离，此时访问 `record.id` 会抛
    # DetachedInstanceError —— 所以调用方可以直接把 id 传进来。
    rid = record if isinstance(record, str) else record.id
    outcome = recompute_and_broadcast(db, reason=f"record_changed:{rid}")
    if background_tasks is None or outcome is None:
        return
    signals, _state = outcome
    if should_trigger(signals, "schedule_change")[0]:
        background_tasks.add_task(run_decision_bg, "schedule_change")


@router.get("", response_model=list[RecordOut])
def list_records(
    status: str | None = Query(default=None, description=f"可选 {RECORD_STATUSES}"),
    type: str | None = Query(default=None, description=f"可选 {RECORD_TYPES}"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[RecordOut]:
    q = db.query(Record)
    if status:
        q = q.filter(Record.status == status)
    if type:
        q = q.filter(Record.type == type)
    rows = q.order_by(Record.start_at.is_(None), Record.start_at).limit(limit).all()
    return [RecordOut.from_orm_model(r) for r in rows]


@router.post("", response_model=RecordOut, status_code=201)
def create_record(
    payload: RecordCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RecordOut:
    record = Record(**payload.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    _on_record_changed(db, record, background_tasks)
    return RecordOut.from_orm_model(record)


@router.get("/{record_id}", response_model=RecordOut)
def get_record(record_id: str, db: Session = Depends(get_db)) -> RecordOut:
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return RecordOut.from_orm_model(record)


@router.patch("/{record_id}", response_model=RecordOut)
def update_record(
    record_id: str,
    payload: RecordUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RecordOut:
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")

    # 显式传 null 一律视作"不修改该字段"——v1.0 没有需要被清空成空值的必填字段。
    patch = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}

    # 合并现有值与增量后**整体**重新校验。
    # 只走 RecordUpdate 是不够的：它只在同一次请求同时给出 start_at 与 end_at 时
    # 才能判断顺序，而 PATCH 允许只改其一（例如只把 end_at 挪到 start_at 之前）。
    try:
        merged = RecordCreate(
            type=patch.get("type", record.type),
            title=patch.get("title", record.title),
            description=patch.get("description", record.description),
            start_at=patch.get("start_at", record.start_at),
            end_at=patch.get("end_at", record.end_at),
            status=patch.get("status", record.status),
            priority=patch.get("priority", record.priority),
            tags=patch.get("tags", record.tags),
        )
    except ValidationError as exc:
        # 合并后才暴露的冲突（如 end_at 跑到 start_at 之前）必须转成 400，
        # 否则会以 500 的形式泄漏出去，前端拿不到可读的错误。
        raise HTTPException(status_code=400, detail=_first_error(exc)) from exc

    for field, value in merged.model_dump().items():
        setattr(record, field, value)

    db.commit()
    db.refresh(record)
    _on_record_changed(db, record, background_tasks)
    return RecordOut.from_orm_model(record)


@router.delete("/{record_id}", status_code=204)
def delete_record(
    record_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> None:
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    # 删除同样要联动：日程没了，冲突应当消失、宠物应当回落。
    # P3 时这里完全没有联动 —— 删掉一条冲突日程后宠物还停在 alert，
    # 直到手动刷新页面才恢复，这是当时的一个真实缺口。
    db.delete(record)
    db.commit()
    _on_record_changed(db, record_id, background_tasks)
