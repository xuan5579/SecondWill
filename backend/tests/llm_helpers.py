"""P4 测试用的构造工具。

不放进 conftest.py 是因为它只服务 LLM 相关的几个测试文件，
而 conftest 里的东西对所有用例可见 —— 少一个全局名字就少一处耦合。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.time import local_to_utc, utc_to_local
from app.llm.contract import (
    DecisionConstraints,
    DecisionRequest,
    DecisionRecord,
    DecisionSignals,
    PetStatePayload,
    TimeRange,
)

#: 固定基线：本地 2026-10-30 07:00（UTC naive）。
#: 刻意设在早晨 —— 前瞻窗口是 [now, now+24h]，基线若定在 10:00，
#: 早上 9 点的日程会落在窗口外，冲突检测根本看不到它们，
#: 测试用例就会在"以为有冲突"的前提下通过（实测踩过）。
NOW = local_to_utc(datetime(2026, 10, 30, 7, 0))


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """按"今天 + day_offset 天的本地 HH:MM"造一个 UTC naive 时刻。"""
    base = datetime(2026, 10, 30, hour, minute) + timedelta(days=day_offset)
    return local_to_utc(base)


def rec(
    rid: str,
    title: str,
    start: tuple[int, int] | None = None,
    end: tuple[int, int] | None = None,
    *,
    day_offset: int = 0,
    priority: int = 3,
    status: str = "pending",
) -> DecisionRecord:
    """造一条契约记录。`start/end` 为 (小时, 分钟)。"""
    return DecisionRecord(
        id=rid,
        title=title,
        status=status,  # type: ignore[arg-type]
        start=at(day_offset, start[0], start[1]) if start else None,
        end=at(day_offset, end[0], end[1]) if end else None,
        priority=priority,
    )


def make_request(
    records: list[DecisionRecord],
    *,
    immutable: tuple[str, ...] = (),
    max_suggestions: int = 3,
    working_start: str = "08:00",
    working_end: str = "23:00",
    index: float = 0.0,
    load_ratio: float = 0.0,
    mood: str = "normal",
) -> DecisionRequest:
    return DecisionRequest(
        request_id="req_test",
        trigger="manual",
        triggered_at=NOW,
        horizon=TimeRange(start=NOW, end=NOW + timedelta(hours=24)),
        records=records,
        signals=DecisionSignals(
            procrastination_index=index,
            load_ratio=load_ratio,
            overdue_count=0,
            avg_overdue_days=0.0,
        ),
        pet_state=PetStatePayload(mood=mood, energy=0.5),
        constraints=DecisionConstraints(
            max_suggestions=max_suggestions,
            immutable_record_ids=list(immutable),
            working_hours={"start": working_start, "end": working_end},
        ),
    )


def suggestion_dict(
    rid: str,
    *,
    kind: str = "move",
    start: tuple[int, int] | None = None,
    end: tuple[int, int] | None = None,
    day_offset: int = 0,
    rationale: str = "测试",
    risk: str = "low",
) -> dict:
    """造一条**原始**建议（模拟模型输出的 dict，未过 Pydantic）。"""
    out: dict = {
        "id": "s1",
        "kind": kind,
        "target_record_id": rid,
        "rationale": rationale,
        "risk": risk,
    }
    if start:
        out["proposed_start"] = _local_wall(day_offset, start[0], start[1])
    if end:
        out["proposed_end"] = _local_wall(day_offset, end[0], end[1])
    return out


def _local_wall(day_offset: int, hour: int, minute: int) -> str:
    """模型视角的时间字符串：**本地墙上时间，不带时区**。

    刻意用这种形式 —— 真实模型就是这么写的，测试用例必须还原它，
    否则"naive 按本地解释"这条规则根本没被验证到。
    """
    return utc_to_local(at(day_offset, hour, minute)).isoformat()


class FakeProvider:
    """按脚本返回响应的假 provider。

    用脚本而不是"总返回同一个"，是为了覆盖重试路径：
    **第一次错、第二次对**才是真实世界里最常见的情形。
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls = 0
        self.max_tokens_seen: list = []
        self.prompts: list[str] = []
        self.available = True

    async def complete_json(self, messages, *, max_tokens=None, timeout=None):
        from app.llm.provider import LLMError, LLMErrorKind, LLMResponse

        self.calls += 1
        self.max_tokens_seen.append(max_tokens)
        self.prompts.append(messages[-1]["content"] if messages else "")
        item = (
            self.script.pop(0)
            if self.script
            else LLMError(LLMErrorKind.EMPTY, "脚本用尽")
        )
        if isinstance(item, BaseException):
            raise item
        import json

        return LLMResponse(
            content=json.dumps(item, ensure_ascii=False), data=item, finish_reason="stop"
        )


class UnavailableProvider:
    """模拟"没配 API Key"。它的 complete_json 一旦被调用就是 bug。"""

    available = False

    async def complete_json(self, *a, **kw):  # pragma: no cover
        raise AssertionError("provider 不可用时不应发起调用")


def payload_dict(**kw) -> dict:
    base = {
        "schema_version": "1.0",
        "decision_type": "reschedule",
        "confidence": 0.8,
        "summary": "测试摘要",
        "suggestions": [],
    }
    base.update(kw)
    return base
