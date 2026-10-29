"""LLM 输入 / 输出契约 —— **唯一真源**（文档 §7 设计约定）。

本文件里的 Pydantic 模型同时服务三个消费者：

1. **后端权威校验**（`llm/validator.py`）：DeepSeek 不支持 provider 侧 schema 强制，
   只保证"输出是合法 JSON"，字段名/类型/枚举都得由这里兜住。
2. **渲染进 prompt 的格式示例**（`llm/prompts/`）：由 `payload_json_schema()` 生成，
   不手写第二份说明，因此**不会出现"文档和代码说的不是一回事"**这种漂移。
3. **前端展示的类型依据**（`frontend/src/types/api.ts` 由后端返回值对齐）。

## 关于时间

模型输出的时间可能是 `"2026-10-30T15:00:00"`（本地，无时区）也可能带 `+08:00`。
契约内部一律收敛成 **UTC naive**（与 `core/time.py` 的单向约定一致）：

    LLM(字符串) --[naive 按本地时区解释]--> 契约(UTC naive) --to_iso--> 前端/落库

"naive 按本地解释"是唯一的合理选择：让模型写 `+08:00` 不现实，
而按 UTC 解释会让每条建议整体偏移 8 小时 —— 这正是 P3 踩过的那个坑（NL 的 `to_iso`）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
)

from app.core.constants import (
    DECISION_MAX_SUGGESTIONS,
    DECISION_MESSAGE_MAX_LEN,
    DECISION_PET_LINE_MAX_LEN,
    DECISION_RATIONALE_MAX_LEN,
    DECISION_SCHEMA_VERSION,
    DECISION_SUMMARY_MAX_LEN,
    DECISION_TITLE_MAX_LEN,
    WORKING_HOURS_END,
    WORKING_HOURS_START,
)
from app.core.time import local_to_utc, to_iso, to_utc_naive

# ==================================================================
# 时间类型：naive 按本地时区解释，统一收敛为 UTC naive
# ==================================================================


def _coerce_dt(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        # 已经是 datetime：naive 视作**内部表示**（UTC naive），不要再按本地解释一次。
        # 规则引擎构造 Suggestion 时传的就是 UTC naive，
        # 若在这里再转一次，每条建议会被整体平移一个时区。
        return to_utc_naive(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            # 非法时间字符串交给 Pydantic 报成标准的校验错误，
            # 重试时会把这条原文回传给模型，它通常一次就改对。
            return value
        if dt.tzinfo is None:
            # 模型不写时区是常态，此时只能按用户所在时区解释；
            # 按 UTC 解释会让每条建议整体偏移一个时区（P3 踩过同样的坑）。
            return local_to_utc(dt)
        return to_utc_naive(dt)
    return value


def _ser_dt(value: datetime | None):
    return to_iso(value)


#: 模型**输出**的时间：字符串里的 naive 时间按用户本地时区解释（模型不会写 +08:00）
IsoDT = Annotated[
    datetime,
    BeforeValidator(_coerce_dt),
    PlainSerializer(_ser_dt, return_type=str | None, when_used="json"),
]

#: 后端**输入**的时间：已经是 UTC naive，不能再转一次。
#: 这里刻意与 IsoDT 分成两个类型 —— 输入侧如果也按本地解释，
#: 每条日程会被整体平移一个时区，而这种 bug 在单时区环境下极难发现。
UtcDT = Annotated[
    datetime,
    PlainSerializer(_ser_dt, return_type=str | None, when_used="json"),
]

DecisionType = Literal["reschedule", "nudge", "no_action"]
SuggestionKind = Literal["move", "shrink", "drop"]
RiskLevel = Literal["low", "medium", "high"]
Tone = Literal["gentle", "direct", "playful"]
ConflictKindLiteral = Literal["overlap", "no_buffer", "overload"]
ReasoningTag = Literal["conflict", "overload", "procrastination", "idle", "streak"]
TriggerLiteral = Literal["schedule_change", "procrastination_signal", "manual"]
RecordStatusLiteral = Literal["pending", "in_progress", "done", "dropped"]
EventKindLiteral = Literal[
    "task_started",
    "task_delayed",
    "task_abandoned",
    "task_completed",
    "intervention_accepted",
    "intervention_ignored",
]
MoodLiteral = Literal["alert", "dormant", "tired", "guilty", "great", "normal"]


class _Base(BaseModel):
    """契约基类。

    `extra="forbid"` 是有意的：模型多加字段往往意味着它没读懂格式，
    直接判失败（走重试）比默默忽略更接近真实意图。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ==================================================================
# 输入契约（文档 §4.2）
# ==================================================================


class TimeRange(_Base):
    start: UtcDT
    end: UtcDT


class UserProfile(_Base):
    avg_task_overrun_min: float | None = None
    preferred_focus_blocks: list[str] = Field(default_factory=list)
    accepted_intervention_types: list[str] = Field(default_factory=list)
    #: 被忽略多次因而降权的干预类型。模型应避免再次使用（P5 反馈回流写入）
    muted_intervention_types: list[str] = Field(default_factory=list)
    #: 各类型被忽略的次数（含未达阈值的）—— 让模型看得见"快被降权了"，
    #: 而不只是在越过红线之后才收到一句冷冰冰的禁止。
    ignore_counts: dict[str, int] = Field(default_factory=dict)
    #: 各类型被采纳的次数。这是正向信号：被采纳过的类型说明"用户吃这一套"。
    accept_counts: dict[str, int] = Field(default_factory=dict)


class DecisionRecord(_Base):
    id: str
    type: Literal["schedule"] = "schedule"
    title: str
    status: RecordStatusLiteral = "pending"
    start: UtcDT | None = None
    end: UtcDT | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    tags: list[str] = Field(default_factory=list)


class DecisionEvent(_Base):
    at: UtcDT
    kind: EventKindLiteral
    record_id: str | None = None


class DecisionConflict(_Base):
    record_ids: list[str] = Field(default_factory=list)
    kind: ConflictKindLiteral
    hard: bool = False
    overlap_minutes: int = 0


class DecisionSignals(_Base):
    conflicts: list[DecisionConflict] = Field(default_factory=list)
    procrastination_index: float = Field(default=0.0, ge=0.0, le=1.0)
    load_ratio: float = 0.0
    idle_blocks: list[TimeRange] = Field(default_factory=list)
    completion_rate_7d: float = Field(default=0.0, ge=0.0, le=1.0)
    overdue_count: int = 0
    avg_overdue_days: float = 0.0
    streak_days: int = 0


class PetStatePayload(_Base):
    """宠物状态**只作为输入**给模型读（文档 §4.4 ①）。

    模型可以据此调整语气，但无权决定档位 —— 输出契约里没有 `pet_mood` 字段。
    """

    mood: MoodLiteral = "normal"
    energy: float = Field(default=0.5, ge=0.0, le=1.0)
    streak_days: int = 0


class DecisionConstraints(_Base):
    max_suggestions: int = Field(default=DECISION_MAX_SUGGESTIONS, ge=1, le=5)
    immutable_record_ids: list[str] = Field(default_factory=list)
    working_hours: dict[str, str] = Field(
        default_factory=lambda: {"start": WORKING_HOURS_START, "end": WORKING_HOURS_END}
    )


class DecisionRequest(_Base):
    """喂给模型的完整上下文。

    `request_id` 与 `triggered_at` 不参与缓存指纹（见 `llm/cache.py`），
    它们每次请求都不同，但语义相同。
    """

    request_id: str
    trigger: TriggerLiteral
    triggered_at: UtcDT
    timezone: str = "local"
    horizon: TimeRange | None = None
    user_profile: UserProfile = Field(default_factory=UserProfile)
    records: list[DecisionRecord] = Field(default_factory=list)
    behavior_events: list[DecisionEvent] = Field(default_factory=list)
    signals: DecisionSignals = Field(default_factory=DecisionSignals)
    pet_state: PetStatePayload | None = None
    constraints: DecisionConstraints = Field(default_factory=DecisionConstraints)

    def to_fingerprint_payload(self) -> dict:
        """缓存指纹用的规范化字典。"""
        return json.loads(self.model_dump_json())


# ==================================================================
# 输出契约（文档 §4.3）
# ==================================================================


class Suggestion(_Base):
    id: str = ""
    kind: SuggestionKind
    target_record_id: str
    proposed_start: IsoDT | None = None
    proposed_end: IsoDT | None = None
    rationale: str | None = Field(default=None, max_length=DECISION_RATIONALE_MAX_LEN)
    risk: RiskLevel = "low"


class InterventionAction(_Base):
    label: str
    kind: Literal["accept", "snooze", "mute"]


class Intervention(_Base):
    """干预卡片内容。P4 只**展示**，反馈回流（accept/snooze/mute）属 P5。"""

    type: Literal["reschedule_proposal", "nudge"] = "reschedule_proposal"
    title: str = Field(default="", max_length=DECISION_TITLE_MAX_LEN)
    message: str = Field(default="", max_length=DECISION_MESSAGE_MAX_LEN)
    #: 宠物台词 —— 模型在整个宠物系统里唯一的输出口（文档 §5.3）
    pet_line: str | None = Field(default=None, max_length=DECISION_PET_LINE_MAX_LEN)
    tone: Tone = "gentle"
    expires_at: IsoDT | None = None
    actions: list[InterventionAction] = Field(default_factory=list)


class DecisionPayload(_Base):
    schema_version: str = DECISION_SCHEMA_VERSION
    decision_type: DecisionType
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str = Field(default="", max_length=DECISION_SUMMARY_MAX_LEN)
    reasoning_tags: list[ReasoningTag] = Field(default_factory=list)
    suggestions: list[Suggestion] = Field(default_factory=list)
    intervention: Intervention | None = None
    caveats: list[str] = Field(default_factory=list)


# ==================================================================
# Schema 导出：prompt 里那一份格式说明由这里生成，避免手写漂移
# ==================================================================


def payload_json_schema(indent: int | None = 2) -> str:
    """给模型看的"请严格按这个格式输出"示例。

    刻意不直接丢完整 JSON Schema（`$defs`/`anyOf` 对模型不友好且吃 token），
    而是导出后再精简，只保留字段名与类型提示。
    """
    return json.dumps(DecisionPayload.model_json_schema(), ensure_ascii=False, indent=indent)


#: prompt 里用的紧凑版示例。字段顺序即"我希望模型怎么想"的顺序。
PAYLOAD_EXAMPLE: dict[str, Any] = {
    "schema_version": DECISION_SCHEMA_VERSION,
    "decision_type": "reschedule",
    "confidence": 0.72,
    "summary": "把「写周报」往后挪 30 分钟即可解开与例会的重叠",
    "reasoning_tags": ["conflict"],
    "suggestions": [
        {
            "id": "s1",
            "kind": "move",
            "target_record_id": "rec_xxx",
            "proposed_start": "2026-10-30T15:30:00+08:00",
            "proposed_end": "2026-10-30T16:30:00+08:00",
            "rationale": "例会 15:00 结束，顺延半小时不影响当天其他安排",
            "risk": "low",
        }
    ],
    "intervention": {
        "type": "reschedule_proposal",
        "title": "有个时间撞了",
        "message": "「写周报」和「部门例会」重叠了 30 分钟，把周报挪到 15:30 就行。",
        "pet_line": "我看了下日历，这个撞车现在解开最省事。",
        "tone": "gentle",
        "expires_at": "2026-10-30T16:00:00+08:00",
        "actions": [{"label": "就按这个改", "kind": "accept"}],
    },
    "caveats": [],
}


__all__ = [
    "DecisionRequest",
    "DecisionPayload",
    "DecisionRecord",
    "DecisionEvent",
    "DecisionSignals",
    "DecisionConflict",
    "DecisionConstraints",
    "PetStatePayload",
    "UserProfile",
    "TimeRange",
    "Suggestion",
    "Intervention",
    "InterventionAction",
    "payload_json_schema",
    "PAYLOAD_EXAMPLE",
    "IsoDT",
    "UtcDT",
]
