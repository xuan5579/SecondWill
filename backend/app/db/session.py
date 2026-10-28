"""数据库引擎与会话管理。

v1.0 用 SQLite 本地文件，同步驱动即可；FastAPI 的路由用普通 def，
由框架放到线程池执行，不会阻塞事件循环。
"""

from __future__ import annotations

from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.base import Base

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """建表。v1.0 不上 Alembic，数据结构变更时重建本地库即可。"""
    settings.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 导入模型以确保元数据完整
    from app.models import event, intervention, pet_snapshot, record  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖项。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
