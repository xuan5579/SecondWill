"""行为事件流。

这是 LLM 决策器的唯一输入来源：模型看到的不是"日程表"，
而是"执行痕迹"（开始 / 推迟 / 放弃 / 完成 / 对干预的反馈）。
原桌面应用没有这一层。
"""

from __future__ import annotations

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base, utcnow

EVENT_KINDS = (
    "task_started",
    "task_delayed",
    "task_abandoned",
    "task_completed",
    "intervention_accepted",
    "intervention_ignored",
)


class BehaviorEvent(Base):
    __tablename__ = "behavior_events"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: new_id("evt_")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 不设外键：记录被删除后事件仍需保留，否则执行历史会出现空洞
    record_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
