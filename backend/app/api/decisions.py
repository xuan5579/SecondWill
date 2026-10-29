"""决策端点。

P4 的定位（文档 §10）：**有建议就发**，节奏控制与反馈回流属 P5。
因此这里没有冷却期、没有每日上限，也不写降权 —— 那些会让本期难以演示，
且单独做一遍是错误的顺序（先能产生建议，再谈什么时候不该发）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.session import get_db
from app.models.intervention import Intervention
from app.services.decision_flow import run_decision

logger = get_logger(__name__)
router = APIRouter(prefix="/decisions", tags=["decisions"])

TRIGGERS = ("schedule_change", "procrastination_signal", "manual")


class DecisionTrigger(BaseModel):
    trigger: str = Field(default="manual")


@router.post("", response_model=dict)
async def create_decision(
    payload: DecisionTrigger, db: Session = Depends(get_db)
) -> dict:
    """触发一次决策。

    `manual` 恒触发（绕过触发判定），用于演示与"我就是要问一句"。
    """
    if payload.trigger not in TRIGGERS:
        raise HTTPException(status_code=400, detail=f"未知触发类型: {payload.trigger}")
    return await run_decision(db, trigger=payload.trigger)


@router.get("/latest", response_model=dict)
def latest_decision(db: Session = Depends(get_db)) -> dict:
    """最近一次决策结果。前端首屏用 —— 否则刷新页面后干预卡片会消失。

    卡片内容带有 `source`，刷新后仍然能看出这条建议是模型给的还是规则兜的。
    """
    row = (
        db.query(Intervention)
        .order_by(Intervention.created_at.desc())
        .first()
    )
    if row is None:
        return {"has_decision": False}
    return {
        "has_decision": True,
        "id": row.id,
        "source": row.source,
        "decision_type": row.decision_type,
        "created_at": row.created_at.isoformat() + "+00:00",
        **(row.payload or {}),
    }


@router.get("", response_model=list[dict])
def list_decisions(
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[dict]:
    """历史决策（调试面板用）。答辩时用它展示"同一场景命中缓存不再调用"。"""
    rows = (
        db.query(Intervention)
        .order_by(Intervention.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "source": r.source,
            "decision_type": r.decision_type,
            "created_at": r.created_at.isoformat() + "+00:00",
            "summary": (r.payload or {}).get("payload", {}).get("summary", ""),
        }
        for r in rows
    ]
