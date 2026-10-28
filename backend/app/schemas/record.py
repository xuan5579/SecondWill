"""记录的 API DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import (
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.core.time import parse_iso, to_iso
from app.models.record import RECORD_STATUSES, RECORD_TYPES


def _normalize_dt(v: str | datetime | None) -> datetime | None:
    return parse_iso(v)


def _strip_title(v: str | None, *, allow_none: bool) -> str | None:
    """去掉首尾空白并拒绝空标题。

    单独写在函数里是因为 `RecordBase` 与 `RecordUpdate` 都要用，
    区别只在于是否允许 None（PATCH 语义下 None 表示"不改这个字段"）。
    """
    if v is None:
        if allow_none:
            return None
        raise ValueError("标题不能为空")
    value = v.strip()
    if not value:
        # `min_length=1` 拦不住纯空格的字符串，必须 strip 后再判。
        raise ValueError("标题不能为空")
    return value


def _ensure_ordered(start: datetime | None, end: datetime | None):
    """结束必须晚于开始。

    两者都可能为 None（未定时日程），只要缺一个就不比较。
    """
    if start and end and end <= start:
        raise ValueError("结束时间必须晚于开始时间")


class RecordBase(BaseModel):
    type: str = Field(default="schedule", description=f"v1.0 仅 {RECORD_TYPES}")
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    status: str = Field(default="pending", description=f"可选 {RECORD_STATUSES}")
    priority: int = Field(default=3, ge=1, le=5)
    tags: list[str] = Field(default_factory=list)

    _norm_start = field_validator("start_at", mode="before")(_normalize_dt)
    _norm_end = field_validator("end_at", mode="before")(_normalize_dt)

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str) -> str:
        return _strip_title(v, allow_none=False)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in RECORD_STATUSES:
            raise ValueError(f"状态必须是 {list(RECORD_STATUSES)} 之一")
        return v

    @model_validator(mode="after")
    def _check_span(self) -> "RecordBase":
        _ensure_ordered(self.start_at, self.end_at)
        return self


class RecordCreate(RecordBase):
    pass


class RecordUpdate(BaseModel):
    """全字段可选。未提供的字段保持原值。

    注意：本模型的跨字段校验只在**同一请求同时给出 start_at 与 end_at** 时生效。
    只改其中一个的情况必须由路由层合并现有值后再用 `RecordCreate` 整体验证
    （见 `api/records.py::update_record`），否则 "PATCH end_at 到起始时间之前"
    这类操作会绕过约束。
    """

    type: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    status: str | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    tags: list[str] | None = None

    _norm_start = field_validator("start_at", mode="before")(_normalize_dt)
    _norm_end = field_validator("end_at", mode="before")(_normalize_dt)

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str | None) -> str | None:
        return _strip_title(v, allow_none=True)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str | None) -> str | None:
        if v is not None and v not in RECORD_STATUSES:
            raise ValueError(f"状态必须是 {list(RECORD_STATUSES)} 之一")
        return v

    @model_validator(mode="after")
    def _check_span(self) -> "RecordUpdate":
        _ensure_ordered(self.start_at, self.end_at)
        return self


class RecordOut(BaseModel):
    id: str
    type: str
    title: str
    description: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    status: str
    priority: int
    tags: list[str]
    created_at: datetime
    updated_at: datetime

    @field_serializer("start_at", "end_at", "created_at", "updated_at")
    def _ser(self, v: datetime | None) -> str | None:
        return to_iso(v)

    @classmethod
    def from_orm_model(cls, obj) -> "RecordOut":
        return cls.model_validate(obj, from_attributes=True)
