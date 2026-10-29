"""干预节奏闸门 —— 决定"这次该不该打扰用户"。

这是纯函数模块：不碰数据库、不碰时钟（now 由调用方注入）、不产生副作用。
因此它可以被单测完整覆盖，也能脱离 Web 运行（用户明确要求的性质）。

## 为什么要有这一层

P4 的定位是"有建议就发"。那对演示是对的，对长期使用是灾难：
一个每改一次日程就弹卡片的助手，三天后就会被关掉。

节奏闸门是**防负罪感机制**的落地点。它只回答一个问题：
"现在发，用户会觉得被帮到，还是被打扰？"

## 三个参数（全部来自 `core/constants.py`，本文件零字面量）

    B4 冷却期     INTERVENTION_COOLDOWN_MINUTES —— 距上次送达不足此时长不发
    B5 每日上限   INTERVENTION_DAILY_LIMIT      —— 本地一天内自动送达的上限
    B6 降权阈值   INTERVENTION_MUTE_THRESHOLD   —— 同类被忽略几次后彻底闭嘴

## 两条容易被忽略的取舍

1. **手动触发绕过闸门，但仍刷新冷却时钟。**
   用户主动点"问决策器"时被"今日额度已用完"拒绝，是在惩罚用户使用产品；
   但点完之后不该立刻又弹出一条自动干预，所以手动同样把冷却时钟推到 45 分钟后。

2. **降权是双通道的。**
   计数达阈值的类型既进闸门的黑名单（后端拒发），也进 prompt 的负面清单
   （模型自己就不再提）。前者是兜底，后者是根治 —— 只做前者，
   模型每次仍会辛苦生成一条注定被丢掉的建议，白烧一次调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.constants import (
    INTERVENTION_COOLDOWN_MINUTES,
    INTERVENTION_DAILY_LIMIT,
    INTERVENTION_MUTE_THRESHOLD,
    RHYTHM_BYPASS_TRIGGERS,
    RHYTHM_IGNORE_FEEDBACKS,
)
from app.core.time import containing_local_day

# ==================================================================
# 输入形态
# ==================================================================


@dataclass(frozen=True)
class InterventionTrace:
    """闸门的一行输入。

    刻意**不是** ORM 对象：闸门要能在没有数据库的环境里被测试。
    `decision_flow.py` 负责把 Intervention 行映射成这个结构 —— 映射只发生在
    IO 边界上，这条分层与 `state.py` / `decision_flow.py` 是一致的。
    """

    decision_type: str
    created_at: datetime
    delivered_at: datetime | None = None
    feedback: str | None = None
    feedback_at: datetime | None = None
    #: 是否计入每日额度。手动触发送达的干预不计费（见模块 docstring 取舍 1）。
    metered: bool = True

    @property
    def effective_at(self) -> datetime:
        """这条干预"占用"的时刻。

        用户点了"稍后"或"不再提醒"之后，冷却期应当从**反馈时刻**重新起算 ——
        否则用户刚刚表达完"别烦我"，45 分钟后算下来刚好又能发一条，
        体验上就是"点了稍后没用"。
        """
        if self.feedback in RHYTHM_IGNORE_FEEDBACKS and self.feedback_at is not None:
            return self.feedback_at
        return self.delivered_at or self.created_at


@dataclass(frozen=True)
class RhythmVerdict:
    """闸门结论。

    `reason` 是要给人看的：排查"为什么没弹卡片"时，第一件事就是读它，
    所以它必须说清是被**哪一档**拦下的、什么时候解禁，而不是一句 False。
    """

    can_deliver: bool
    reason: str
    #: 拦截原因代号：`""`（放行）/ `cooldown` / `daily_limit`
    blocked_by: str = ""
    #: 最早可再次自动送达的时刻。手动触发被放行时也可能非 None。
    next_allowed_at: datetime | None = None
    #: 今日已用额度 / 剩余额度（仅统计计费的自动干预）
    used_today: int = 0
    remaining_today: int = 0


# ==================================================================
# 降权统计（反馈回流的另一半）
# ==================================================================


def ignore_counts(traces) -> dict[str, int]:
    """每个干预类型被忽略了几次。"""
    counts: dict[str, int] = {}
    for t in traces:
        if t.feedback in RHYTHM_IGNORE_FEEDBACKS:
            counts[t.decision_type] = counts.get(t.decision_type, 0) + 1
    return counts


def accept_counts(traces) -> dict[str, int]:
    """每个干预类型被采纳了几次。"""
    counts: dict[str, int] = {}
    for t in traces:
        if t.feedback == "accept":
            counts[t.decision_type] = counts.get(t.decision_type, 0) + 1
    return counts


def muted_types(
    traces, *, threshold: int = INTERVENTION_MUTE_THRESHOLD
) -> list[str]:
    """已被彻底降权的干预类型（按字典序，保证 prompt 与测试的可复现性）。"""
    return sorted(k for k, v in ignore_counts(traces).items() if v >= threshold)


def is_muted(
    decision_type: str, traces, *, threshold: int = INTERVENTION_MUTE_THRESHOLD
) -> bool:
    """某个类型是否已被降权。

    这是**决策之后**的兜底拒发点：只有拿到 `decision_type` 才知道该不该发，
    所以闸门必须在决策之后再看一次。正常路径下它不会触发 ——
    prompt 里已经写了负面清单，模型不会再提这个类型。
    """
    return ignore_counts(traces).get(decision_type, 0) >= threshold


# ==================================================================
# 闸门主体
# ==================================================================


def evaluate_rhythm(
    traces,
    trigger: str,
    now: datetime,
    *,
    cooldown_minutes: int = INTERVENTION_COOLDOWN_MINUTES,
    daily_limit: int = INTERVENTION_DAILY_LIMIT,
) -> RhythmVerdict:
    """判断这次能不能发。放在**调用模型之前**，被拦下时一次调用都不烧。"""

    traces = list(traces)
    today = containing_local_day(now)
    used_today = sum(
        1
        for t in traces
        if t.metered and containing_local_day(t.effective_at) == today
    )
    remaining = max(0, daily_limit - used_today)

    # ---- 手动触发：绕过闸门，但仍报告用量 ----
    if trigger in RHYTHM_BYPASS_TRIGGERS:
        return RhythmVerdict(
            can_deliver=True,
            reason=f"手动触发，绕过节奏闸门（今日自动额度剩余 {remaining}/{daily_limit}）",
            next_allowed_at=_next_allowed(traces, now, cooldown_minutes),
            used_today=used_today,
            remaining_today=remaining,
        )

    # ---- B5 每日上限 ----
    if used_today >= daily_limit:
        return RhythmVerdict(
            can_deliver=False,
            reason=(
                f"今日自动干预已达上限 {daily_limit}/{daily_limit}"
                f"（明天 00:00 重置；手动触发仍可用）"
            ),
            blocked_by="daily_limit",
            next_allowed_at=_next_local_midnight(now),
            used_today=used_today,
            remaining_today=0,
        )

    # ---- B4 冷却期 ----
    last = _last_effective(traces)
    if last is not None:
        next_ok = last + timedelta(minutes=cooldown_minutes)
        if now < next_ok:
            wait = int((next_ok - now).total_seconds() // 60) + 1
            return RhythmVerdict(
                can_deliver=False,
                reason=f"距上一次干预仅 {cooldown_minutes - wait + 1} 分钟，冷却期 {cooldown_minutes} 分钟未过（还需 {wait} 分钟）",
                blocked_by="cooldown",
                next_allowed_at=next_ok,
                used_today=used_today,
                remaining_today=remaining,
            )

    return RhythmVerdict(
        can_deliver=True,
        reason=f"通过节奏闸门（今日 {used_today}/{daily_limit}）",
        next_allowed_at=_next_allowed(traces, now, cooldown_minutes),
        used_today=used_today,
        remaining_today=remaining,
    )


# ==================================================================
# 内部工具
# ==================================================================


def _last_effective(traces) -> datetime | None:
    times = [t.effective_at for t in traces]
    return max(times) if times else None


def _next_allowed(traces, now: datetime, cooldown_minutes: int) -> datetime | None:
    last = _last_effective(traces)
    if last is None:
        return None
    return last + timedelta(minutes=cooldown_minutes)


def _next_local_midnight(now: datetime) -> datetime:
    """次日本地 00:00 对应的 UTC naive —— 每日额度的重置点。"""
    from datetime import time as _time

    from app.core.time import local_to_utc

    return local_to_utc(
        datetime.combine(containing_local_day(now), _time.min) + timedelta(days=1)
    )


@dataclass
class FeedbackProfile:
    """喂给 `UserProfile` 的反馈摘要。由 `decision_flow.py` 组装。"""

    muted_intervention_types: list[str] = field(default_factory=list)
    accepted_intervention_types: list[str] = field(default_factory=list)
    ignore_counts: dict[str, int] = field(default_factory=dict)
    accept_counts: dict[str, int] = field(default_factory=dict)


def feedback_profile(
    traces, *, threshold: int = INTERVENTION_MUTE_THRESHOLD
) -> FeedbackProfile:
    """把反馈历史压成一份给模型的摘要。"""
    ig = ignore_counts(traces)
    ac = accept_counts(traces)
    return FeedbackProfile(
        muted_intervention_types=muted_types(traces, threshold=threshold),
        accepted_intervention_types=sorted(k for k, v in ac.items() if v > 0),
        ignore_counts=dict(sorted(ig.items())),
        accept_counts=dict(sorted(ac.items())),
    )
