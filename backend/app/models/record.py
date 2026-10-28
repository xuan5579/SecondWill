"""统一数据源模型。

`record` 是日程 / 笔记 / 待办的公共抽象。v1.0 只实现 `type=schedule`，
其余类型在 v1.1+ 接入，但表结构与 API 已按其预留。
"""

from __future__ import annotations

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_id
from app.db.base import Base, TimestampMixin

RECORD_TYPES = ("schedule",)  # v1.0
RECORD_STATUSES = ("pending", "in_progress", "done", "dropped")


class Record(Base, TimestampMixin):
    __tablename__ = "records"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: new_id("rec_")
    )
    type: Mapped[str] = mapped_column(String(16), default="schedule", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    start_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    end_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
