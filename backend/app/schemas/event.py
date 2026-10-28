"""行为事件的 API DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.core.time import parse_iso, to_iso
from app.models.event import EVENT_KINDS


class EventCreate(BaseModel):
    kind: str = Field(description=f"可选 {EVENT_KINDS}")
    record_id: str | None = None
    at: datetime | None = None
    payload: dict = Field(default_factory=dict)

    _norm_at = field_validator("at", mode="before")(parse_iso)


class EventBatchCreate(BaseModel):
    """批量上报。前端可攒一批再发，减少请求数。"""

    events: list[EventCreate] = Field(min_length=1, max_length=200)


class EventOut(BaseModel):
    id: str
    kind: str
    record_id: str | None = None
    at: datetime
    payload: dict

    @field_serializer("at")
    def _ser(self, v: datetime | None) -> str | None:
        return to_iso(v)

    @classmethod
    def from_orm_model(cls, obj) -> "EventOut":
        return cls.model_validate(obj, from_attributes=True)
