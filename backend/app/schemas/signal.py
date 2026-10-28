"""信号与冲突的 API DTO。

冲突对象不只给前端渲染——它同时是 **LLM 决策器的输入**（文档 §4.2）。
所以字段名与语义保持前后端、模型三处一致，避免各写一套。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_serializer

from app.core.time import to_iso


class ConflictOut(BaseModel):
    record_ids: list[str]
    kind: str
    hard: bool
    overlap_minutes: int
    window: dict[str, str | None] | None = None
    load_ratio: float | None = None
    detail: dict = {}


class SignalsOut(BaseModel):
    conflicts: list[ConflictOut]
    procrastination_index: float
    load_ratio: float
    idle_blocks: list[dict[str, str]]
    completion_rate_7d: float
    completion_rate_today: float
    overdue_count: int
    avg_overdue_days: float
    streak_days: int
    due_total_7d: int
    computed_at: datetime

    @field_serializer("computed_at")
    def _ser(self, v: datetime) -> str | None:
        return to_iso(v)


def conflict_to_out(c) -> ConflictOut:
    return ConflictOut(
        record_ids=list(c.record_ids),
        kind=c.kind,
        hard=c.hard,
        overlap_minutes=c.overlap_minutes,
        window=(
            {"start": to_iso(c.window.start), "end": to_iso(c.window.end)}
            if c.window
            else None
        ),
        load_ratio=c.load_ratio,
        detail=dict(c.detail),
    )


def idle_blocks_to_out(blocks) -> list[dict[str, str]]:
    return [
        {"start": to_iso(b.start), "end": to_iso(b.end)} for b in blocks
    ]
