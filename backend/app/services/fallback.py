"""规则降级引擎（L0）—— 与 LLM **同接口**的另一种实现。

设计上刻意让它的签名与 LLM 调用完全一致：

    (DecisionRequest) -> DecisionPayload

这不是巧合，而是让"降级"变成**换一个实现**而不是"走另一条代码路径"：
编排器不需要知道现在是模型在说话还是规则在说话，它只看到同一份契约输出。

算法的性格：**保守、可解释、绝不激进**。
按优先级贪心顺延 —— 优先级高（数值小）的日程先占位，后来的让路。
它不会把日程推到下周，也不会缩短任何任务（缩短等于替用户做价值判断，
规则层没这个资格），找不到落点就明确地说"我没辙，只能提醒你一句"。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from app.core.constants import (
    CONFLICT_BUFFER_MINUTES,
    DECISION_MAX_SUGGESTIONS,
    FALLBACK_CONFIDENCE,
    FALLBACK_MAX_CANDIDATES,
    FALLBACK_MAX_MOVE_DAYS,
    FALLBACK_SLOT_STEP_MINUTES,
    WORKING_HOURS_END,
    WORKING_HOURS_START,
)
from app.core.logging import get_logger
from app.core.time import containing_local_day, local_to_utc, utc_to_local
from app.llm.contract import (
    DecisionPayload,
    DecisionRequest,
    Intervention,
    InterventionAction,
    Suggestion,
)
from app.services.conflict import TimeWindow

logger = get_logger(__name__)

_TERMINAL = {"done", "dropped"}


# ==================================================================
# 落点搜索
# ==================================================================


def _parse_hhmm(value: str, default: time) -> time:
    try:
        hour, minute = value.split(":")[:2]
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return default


def _day_working_window(day, start_t: time, end_t: time) -> TimeWindow:
    """某本地日的可安排窗口（UTC naive 边界）。"""
    start_local = datetime.combine(day, start_t)
    end_local = datetime.combine(day, end_t)
    if end_local <= start_local:
        end_local += timedelta(days=1)
    return TimeWindow(local_to_utc(start_local), local_to_utc(end_local))


def _clashes(occupied: list[TimeWindow], span: TimeWindow) -> bool:
    """与已占位区间是否有 overlap 或间隔不足。

    沿用冲突检测的口径（半开区间 + 缓冲阈值），
    否则会出现"规则引擎认为不冲突，但冲突检测列表里还挂着"的自相矛盾输出。
    """
    for o in occupied:
        if span.intersects(o):
            return True
        if span.start >= o.end:
            gap_min = (span.start - o.end).total_seconds() / 60.0
        else:
            gap_min = (o.start - span.end).total_seconds() / 60.0
        if gap_min < CONFLICT_BUFFER_MINUTES:
            return True
    return False


def _align_up(dt: datetime, step_minutes: int) -> datetime:
    """向上对齐到整刻。生成 `10:07` 这种时间只会让人怀疑系统坏了。"""
    step = timedelta(minutes=step_minutes)
    base = dt.replace(second=0, microsecond=0)
    remainder = (base - base.replace(hour=0, minute=0)).total_seconds() / 60.0
    pad = (-remainder) % step_minutes
    return base + timedelta(minutes=int(pad))


def find_slot(
    earliest: datetime,
    duration: timedelta,
    occupied: list[TimeWindow],
    start_t: time,
    end_t: time,
    now: datetime,
) -> datetime | None:
    """从 `earliest` 起找第一个放得下的落点。

    逐日扫描工作时间窗，步进 15 分钟。上限 `FALLBACK_MAX_CANDIDATES` 个候选，
    保证最坏情况下也是常数时间 —— 降级路径自己不能成为性能问题。
    """
    cursor = _align_up(max(earliest, now), FALLBACK_SLOT_STEP_MINUTES)
    day = containing_local_day(cursor)
    step = timedelta(minutes=FALLBACK_SLOT_STEP_MINUTES)

    for _ in range(FALLBACK_MAX_MOVE_DAYS + 1):
        window = _day_working_window(day, start_t, end_t)
        if cursor < window.start:
            cursor = window.start
        limit = window.end - duration
        tried = 0
        while cursor <= limit and tried < FALLBACK_MAX_CANDIDATES:
            span = TimeWindow(cursor, cursor + duration)
            if not _clashes(occupied, span):
                return cursor
            cursor += step
            tried += 1
        day += timedelta(days=1)
        cursor = _day_working_window(day, start_t, end_t).start
    return None


# ==================================================================
# 主入口
# ==================================================================


def build_fallback(req: DecisionRequest, now: datetime | None = None) -> DecisionPayload:
    """生成规则降级方案。纯函数式的：不读库、不发请求。

    `now` 缺省时取 `req.triggered_at` —— 这样同一份请求永远得到同一份输出，
    可被单测钉死。
    """
    now = now or req.triggered_at
    immutable = set(req.constraints.immutable_record_ids)
    start_t = _parse_hhmm(req.constraints.working_hours.get("start", WORKING_HOURS_START), time(8, 0))
    end_t = _parse_hhmm(req.constraints.working_hours.get("end", WORKING_HOURS_END), time(23, 0))

    movable = [
        r
        for r in req.records
        if r.start and r.end
        and r.end > r.start
        and r.status not in _TERMINAL
        and r.id not in immutable
        and r.end >= now          # 已经过去的日程不再挪动，那是 nudge 的范畴
    ]
    # 优先级升序（1 最高），同优先级按时间先后
    movable.sort(key=lambda r: (r.priority or 3, r.start))

    occupied: list[TimeWindow] = []
    # 不可变的与已终结的先占位，后面的必须让着它们
    for r in req.records:
        if r.id in immutable or r.status in _TERMINAL:
            if r.start and r.end and r.end > r.start:
                occupied.append(TimeWindow(r.start, r.end))

    suggestions: list[Suggestion] = []
    blocked: list[str] = []

    for r in movable:
        duration = r.end - r.start
        span = TimeWindow(r.start, r.end)
        if not _clashes(occupied, span):
            occupied.append(span)
            continue

        # 从"原开始时间"和"挡路者的结束 + 缓冲"里取较晚者开始找
        tail = max((o.end for o in occupied if o.end <= r.start), default=r.start)
        earliest = max(r.start, tail + timedelta(minutes=CONFLICT_BUFFER_MINUTES))
        slot = find_slot(earliest, duration, occupied, start_t, end_t, now)
        if slot is None:
            # 找不到落点就**不硬排**。留一条 blocked 交给 nudge 文案说明，
            # 而不是把日程塞到凌晨——那比不干预更糟。
            blocked.append(r.title)
            occupied.append(span)
            continue

        new_span = TimeWindow(slot, slot + duration)
        occupied.append(new_span)
        if len(suggestions) < req.constraints.max_suggestions:
            shift = int(round((slot - r.start).total_seconds() / 60.0))
            suggestions.append(
                Suggestion(
                    id=f"fb_{len(suggestions) + 1}",
                    kind="move",
                    target_record_id=r.id,
                    proposed_start=slot,
                    proposed_end=slot + duration,
                    rationale=(
                        f"按优先级让路，向后顺延 {shift} 分钟，"
                        f"不与更高优先级的安排冲突"
                    ),
                    risk="low",
                )
            )

    return _compose(req, suggestions, blocked, now)


# ==================================================================
# 文案组装
# ==================================================================


def _compose(
    req: DecisionRequest,
    suggestions: list[Suggestion],
    blocked: list[str],
    now: datetime,
) -> DecisionPayload:
    """把重排结果写成一张能给用户的卡片。

    降级时的文案刻意**承认自己是规则方案**（在 caveats 里写明），
    而不是假装这是模型的判断 —— 演示时这是加分项，冒充则是事故。
    """
    from app.services.pet import PetMood, PetState, template_line

    mood_name = req.pet_state.mood if req.pet_state else "normal"
    try:
        mood = PetMood(mood_name)
    except ValueError:
        mood = PetMood.NORMAL
    state = PetState(
        mood=mood,
        energy=req.pet_state.energy if req.pet_state else 0.5,
        streak_days=req.pet_state.streak_days if req.pet_state else 0,
        computed_at=now,
        reason="fallback",
    )
    # seed 用档位名：同一档位稳定对应同一句，刷新不跳话
    line = template_line(state, seed=sum(ord(c) for c in mood.value))

    overload = req.signals.load_ratio > 1.0
    heavy_delay = req.signals.procrastination_index >= 0.5

    if suggestions:
        first = suggestions[0]
        target = next((r for r in req.records if r.id == first.target_record_id), None)
        title = "有日程撞在一起了"
        when = utc_to_local(first.proposed_start).strftime("%H:%M")
        name = target.title if target else "该日程"
        message = f"「{name}」与另一条安排冲突，挪到 {when} 就能解开。"
        if blocked:
            message += f"另有 {len(blocked)} 条暂时排不开，建议改天或拆小。"
        decision_type = "reschedule"
        int_type = "reschedule_proposal"
        summary = f"按优先级顺延 {len(suggestions)} 条日程，解开当前冲突"
        tags = ["conflict"]
    elif overload or heavy_delay:
        title = "今天的安排有点满" if overload else "有几条拖住了"
        if overload:
            message = (
                f"今天排的量已超过常规承载（负载比 {req.signals.load_ratio:.2f}），"
                "建议把优先级最低的一条挪到明天。"
            )
            tags = ["overload"]
        else:
            message = (
                f"有 {req.signals.overdue_count} 条已经逾期"
                f"（平均 {req.signals.avg_overdue_days:.1f} 天），"
                "先挑最早的那条动手会容易些。"
            )
            tags = ["procrastination"]
        decision_type = "nudge"
        int_type = "nudge"
        summary = "当前不适合自动重排，改为提醒"
        suggestions = []
    else:
        return DecisionPayload(
            decision_type="no_action",
            confidence=FALLBACK_CONFIDENCE,
            summary="规则引擎未发现需要干预的情况",
            caveats=["由本地规则引擎生成（模型不可用时的降级方案）"],
        )

    return DecisionPayload(
        decision_type=decision_type,  # type: ignore[arg-type]
        confidence=FALLBACK_CONFIDENCE,
        summary=summary,
        reasoning_tags=tags,  # type: ignore[arg-type]
        suggestions=suggestions[:DECISION_MAX_SUGGESTIONS],
        intervention=Intervention(
            type=int_type,  # type: ignore[arg-type]
            title=title,
            message=message,
            pet_line=line,
            tone="gentle",
            expires_at=now + timedelta(minutes=60),
            actions=[
                InterventionAction(label="就按这个改", kind="accept"),
                InterventionAction(label="稍后提醒", kind="snooze"),
            ],
        ),
        caveats=["由本地规则引擎生成（模型不可用时的降级方案）"],
    )


__all__ = ["build_fallback", "find_slot"]
