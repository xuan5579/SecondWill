"""宠物状态的 API DTO。

注意：情绪档位由后端确定性计算，不由模型决定（文档 §5.3）。
模型只贡献 `line` 台词。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_serializer

from app.core.time import to_iso
from app.models.pet_snapshot import PET_MOODS


class PetStateOut(BaseModel):
    mood: str
    energy: float
    streak_days: int
    line: str | None = None
    computed_at: datetime

    @field_serializer("computed_at")
    def _ser(self, v: datetime | None) -> str | None:
        return to_iso(v)
