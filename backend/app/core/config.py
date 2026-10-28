"""全局配置。

v1.0 为单用户本地应用，无鉴权，无多租户。
约定：所有时间在 API 层以 ISO8601 带时区表示，数据库统一存 UTC。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# backend/app/core/config.py -> parents: [0]=core [1]=app [2]=backend [3]=仓库根
BACKEND_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = Path(__file__).resolve().parents[3]

load_dotenv(ROOT_DIR / ".env")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """轻量配置容器。不引入 pydantic-settings，避免为几个开关多加依赖。"""

    # 数据库
    DATABASE_PATH: Path = Path(
        os.getenv("DATABASE_PATH", str(ROOT_DIR / "data" / "app.db"))
    )
    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite:///{DATABASE_PATH}")

    # 演示模式：命中预录制响应而非真实调用模型
    DEMO_MODE: bool = _as_bool(os.getenv("DEMO_MODE"), default=False)
    DEMO_RECORD: bool = _as_bool(os.getenv("DEMO_RECORD"), default=False)
    DEMO_FIXTURES_DIR: Path = BACKEND_DIR / "fixtures" / "demo"

    # 前端开发服务器地址（CORS）
    # 必须同时放行 localhost 与 127.0.0.1：预览面板可能以任一形式打开页面，
    # Origin 是逐字节精确匹配的，写错一个就等于整站请求全部被浏览器拦掉。
    CORS_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if o.strip()
    ]

    # LLM（P4 接入；P1 仅占位，缺失不影响启动）
    DEEPSEEK_API_KEY: str | None = os.getenv("DEEPSEEK_API_KEY")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
    LLM_TIMEOUT_SEC: float = float(os.getenv("LLM_TIMEOUT_SEC", "15"))
    # 额度必须按"推理 + 输出共用"来算，而且**随 prompt 规模增长**：
    # 实测种子库（10 条记录）那档 prompt 的推理 token 在 1111~2194 之间，
    # 2048 会直接撞顶 —— 表现不是"截断"，而是 content 全空（推理吃光了额度，
    # 一个输出 token 都没剩）。4096 下同一 prompt 5/5 成功（见 §6.2 与 docs/09）。
    # max_tokens 只是上限，不是预购额度：调高它不为成功响应多付钱。
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "4096"))
    LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.2"))

    # 应用
    APP_TITLE: str = "每日规划助手 · AI 执行教练"
    API_PREFIX: str = "/api"


settings = Settings()
