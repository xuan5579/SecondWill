"""FastAPI 应用入口。

v1.0 单用户本地运行，无鉴权中间件。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    decisions,
    events,
    export,
    interventions,
    parse,
    pet,
    records,
    state,
    stream,
)
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.session import init_db

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    logger.info("数据库就绪: %s", settings.DATABASE_PATH)
    if settings.DEMO_MODE:
        logger.info("DEMO_MODE 已开启：决策将走录制回放，不真实调用模型")
    yield


app = FastAPI(
    title=settings.APP_TITLE,
    version="0.3.0-p3",
    description="AI 执行教练 / 拖延干预系统 · v1.0 最小闭环",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (
    records.router, events.router, pet.router,
    state.router, stream.router, export.router, parse.router,
    decisions.router, interventions.router,
):
    app.include_router(r, prefix=settings.API_PREFIX)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {
        "status": "ok",
        "demo_mode": settings.DEMO_MODE,
        "db": str(settings.DATABASE_PATH),
    }
