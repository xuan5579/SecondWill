"""干预记录。

每次通过节奏闸门下发的干预都落一条，附带用户反馈。
`source` 字段记录方案来自 LLM / 缓存 / 规则降级，用于可观测性与答辩演示。
"""

from __future__ import annotations

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base, utcnow

INTERVENTION_SOURCES = ("llm", "cache", "fallback")
FEEDBACK_KINDS = ("accept", "snooze", "mute")


class Intervention(Base):
    __tablename__ = "interventions"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: new_id("ivt_")
    )
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="llm", nullable=False)

    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow, nullable=False)
    delivered_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)

    feedback: Mapped[str | None] = mapped_column(String(16), nullable=True)
    feedback_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
