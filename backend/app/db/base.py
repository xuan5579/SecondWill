"""SQLAlchemy 声明基类与公共列定义。

时间约定：数据库统一存 **UTC naive datetime**。
SQLite 对时区感知支持不完整，因此在持久层一律用 naive UTC，
由 API 层负责把 ISO8601 带时区字符串转进转出。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """当前 UTC 时间（naive）。所有默认值统一走这里，避免混用 local 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
