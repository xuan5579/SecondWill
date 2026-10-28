"""宠物状态查询。

P2 起不再读快照，改为**实时重算并落一份快照**——宠物是仪表盘，
用户看到的必须是当前真实状态；读快照会让它退化成"历史记录"。

情绪档位由后端确定性计算，不由模型决定（文档 §5.3）。
本端点零 LLM 调用。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.pet import PetStateOut
from app.services.pet import template_line
from app.services.state import refresh_pet_snapshot

router = APIRouter(prefix="/pet", tags=["pet"])


@router.get("", response_model=PetStateOut)
def get_pet_state(
    persist: bool = Query(default=True, description="是否落一份快照"),
    db: Session = Depends(get_db),
) -> PetStateOut:
    """实时计算宠物状态。"""
    _signals, state = refresh_pet_snapshot(db, persist=persist)
    seed = sum(ord(ch) for ch in state.mood.value)
    return PetStateOut(
        mood=state.mood.value,
        energy=state.energy,
        streak_days=state.streak_days,
        line=template_line(state, seed=seed),
        computed_at=state.computed_at,
    )


@router.get("/history", response_model=list[PetStateOut])
def list_pet_history(
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[PetStateOut]:
    """最近的宠物状态快照，用于演示"情绪随数据变化"的趋势。"""
    from app.models.pet_snapshot import PetSnapshot

    rows = (
        db.query(PetSnapshot).order_by(PetSnapshot.computed_at.desc()).limit(limit).all()
    )
    return [
        PetStateOut(
            mood=r.mood,
            energy=r.energy,
            streak_days=r.streak_days,
            line=r.line,
            computed_at=r.computed_at,
        )
        for r in rows
    ]
