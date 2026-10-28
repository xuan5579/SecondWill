"""宠物状态快照。

情绪档位由后端确定性计算（见文档 §5.3：情绪是仪表盘，不是模型的输出）。
这里存快照是为了回看趋势与演示，不参与计算。
"""

from __future__ import annotations

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base, utcnow

PET_MOODS = ("alert", "dormant", "tired", "guilty", "great", "normal")


class PetSnapshot(Base):
    __tablename__ = "pet_snapshots"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: new_id("pet_")
    )
    mood: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    energy: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    streak_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    line: Mapped[str | None] = mapped_column(Text, nullable=True)
    computed_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow, nullable=False)
