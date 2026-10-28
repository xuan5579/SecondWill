"""ID 生成。统一入口，避免各处自己拼 uuid 导致格式不一致。"""

from __future__ import annotations

import uuid


def new_id(prefix: str = "") -> str:
    """生成短 ID。带语义前缀便于日志与调试时辨认对象类型。"""
    return f"{prefix}{uuid.uuid4().hex[:12]}"
