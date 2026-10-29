"""干预反馈端点（P5）。

**这个端点的存在意义不是记一笔数据，而是让反馈真的改变下一次决策。**

一次 `mute` 的落地路径必须是完整的一条链：

    用户点「不再提醒」
      -> POST /api/interventions/{id}/feedback {kind: "mute"}
      -> 落 feedback 字段 + 写一条 behavior_event（进入模型的"执行痕迹"）
      -> 同类忽略计数 +1
      -> 达 INTERVENTION_MUTE_THRESHOLD 后进入 muted_intervention_types
      -> 下一次 build_request 把它写进 UserProfile
      -> render_user_prompt 渲染成「已被用户彻底降权的干预类型」
      -> 模型本轮不再输出该 decision_type

链条里任何一环断了，那次点击就只是数据库里的一个字符串。
`tests/test_feedback_loop.py` 逐环断言这条链，尤其是最后一环 ——
模型有没有**看见**，只能靠检查渲染出的 prompt 文本来证明。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.constants import INTERVENTION_MUTE_THRESHOLD
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import get_db
from app.models.event import BehaviorEvent
from app.models.intervention import FEEDBACK_KINDS, Intervention
from app.services.decision_flow import load_traces
from app.services.rhythm import feedback_profile
from sqlalchemy.orm import Session

logger = get_logger(__name__)
router = APIRouter(prefix="/interventions", tags=["interventions"])

#: 反馈 -> 行为事件类型。
#: 事件流是模型观察用户的唯一窗口，所以反馈必须**同时**落进事件表，
#: 否则模型只看得到"用户做了什么"，看不到"用户希望我别做什么"。
_FEEDBACK_EVENT_KIND = {
    "accept": "intervention_accepted",
    "snooze": "intervention_ignored",
    "mute": "intervention_ignored",
}


class FeedbackIn(BaseModel):
    kind: str = Field(default="accept")


@router.post("/{intervention_id}/feedback", response_model=dict)
def give_feedback(
    intervention_id: str, payload: FeedbackIn, db: Session = Depends(get_db)
) -> dict:
    """对一条干预给出反馈。

    返回更新后的反馈画像 —— 前端可以立刻显示"再忽略 1 次就不再提醒"，
    **用户需要看见自己的操作起了作用**，否则反馈控件形同虚设。
    """
    kind = payload.kind
    if kind not in FEEDBACK_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"未知反馈类型: {kind}（可选 {', '.join(FEEDBACK_KINDS)}）",
        )

    row = db.get(Intervention, intervention_id)
    if row is None:
        raise HTTPException(status_code=404, detail="干预记录不存在")

    now = utcnow()
    row.feedback = kind
    row.feedback_at = now

    # 反馈同时进事件流：模型下一次能看到"用户对干预的态度"，
    # 而不只是"用户做了什么任务"。
    db.add(
        BehaviorEvent(
            kind=_FEEDBACK_EVENT_KIND[kind],
            record_id=None,
            at=now,
            payload={
                "intervention_id": intervention_id,
                "feedback": kind,
                "decision_type": row.decision_type,
                "source": row.source,
            },
        )
    )
    db.commit()

    profile = feedback_profile(load_traces(db, now=now))
    logger.info(
        "干预反馈: id=%s kind=%s -> muted=%s ignore=%s",
        intervention_id,
        kind,
        profile.muted_intervention_types,
        profile.ignore_counts,
    )

    return {
        "ok": True,
        "intervention_id": intervention_id,
        "feedback": kind,
        "muted_intervention_types": profile.muted_intervention_types,
        "ignore_counts": profile.ignore_counts,
        "accept_counts": profile.accept_counts,
        "threshold": INTERVENTION_MUTE_THRESHOLD,
    }


@router.get("/profile", response_model=dict)
def feedback_profile_view(db: Session = Depends(get_db)) -> dict:
    """当前反馈画像。调试面板用，也让"为什么它不提醒我了"有处可查。"""
    profile = feedback_profile(load_traces(db))
    return {
        "muted_intervention_types": profile.muted_intervention_types,
        "accepted_intervention_types": profile.accepted_intervention_types,
        "ignore_counts": profile.ignore_counts,
        "accept_counts": profile.accept_counts,
        "threshold": INTERVENTION_MUTE_THRESHOLD,
    }
