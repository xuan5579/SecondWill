"""公共依赖项汇总。

路由层统一从这里取依赖，避免各处直接 import 具体实现，
将来替换会话/配置来源时只改这一处。
"""

from __future__ import annotations

from app.db.session import get_db

__all__ = ["get_db"]
