"""状态查询：信号 + 宠物。

P2 起这里不再是"读快照"，而是**每次请求实时重算**。
理由：宠物是仪表盘，用户看到的必须是当前真实状态；读快照会让它变成"历史记录"。

全程**零 LLM 调用**——这是验收标准之一：拔网线、删 API Key，这个端点也必须正常返回。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.pet import PetStateOut
from app.schemas.signal import SignalsOut, conflict_to_out, idle_blocks_to_out
from app.services.pet import template_line
from app.services.state import compute_state


router = APIRouter(prefix="/state", tags=["state"])


@router.get("", response_model=dict)
def get_state(db: Session = Depends(get_db)) -> dict:
    """一次返回信号与宠物状态，避免前端为同一份数据发两次请求。"""
    signals, pet = compute_state(db)

    signals_out = SignalsOut(
        conflicts=[conflict_to_out(c) for c in signals.conflicts],
        procrastination_index=signals.procrastination_index,
        load_ratio=signals.load_ratio,
        idle_blocks=idle_blocks_to_out(signals.idle_blocks),
        completion_rate_7d=signals.completion_rate_7d,
        completion_rate_today=signals.completion_rate_today,
        overdue_count=signals.overdue_count,
        avg_overdue_days=signals.avg_overdue_days,
        streak_days=signals.streak_days,
        due_total_7d=signals.due_total_7d,
        computed_at=pet.computed_at,
    )
    # 同一档位稳定对应同一句台词：刷新页面不会跳不同的话
    seed = sum(ord(ch) for ch in pet.mood.value)
    pet_out = PetStateOut(
        mood=pet.mood.value,
        energy=pet.energy,
        streak_days=pet.streak_days,
        line=template_line(pet, seed=seed),
        computed_at=pet.computed_at,
    )
    return {
        "signals": signals_out.model_dump(),
        "pet": pet_out.model_dump(),
        "pet_reason": pet.reason,
    }
