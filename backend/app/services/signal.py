"""信号聚合层 —— 纯函数，确定性，零 IO，零 LLM。

职责：把「原始记录 + 行为事件」压缩成 LLM 能消费的一小撮指标。

为什么要有这一层
----------------
LLM 的上下文窗口不是免费的，而更重要的是：**把 200 条日程原样丢给模型，它算不出稳定的统计**。
日期算术、比率、求和这类事代码既准又廉价；模型该在**已经算好的指标**上做判断。

所以这里输出的每一个字段，都是给模型（同时也给宠物状态机）读的：
模型不负责算 `overdue_count`，它只负责回答"知道了这些之后，我应该建议什么"。

纯函数约定
----------
本模块**不接受也不产生时钟读取**：`now` 必须由调用方传入。
这样任何一条指标都能在不启动 Web、不碰数据库的情况下被单测覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from app.core.constants import (
    EMPTY_COMPLETION_RATE,
    FORWARD_WINDOW,
    LOOKBACK_WINDOW,
    MAX_OVERDUE_DAYS,
    PLANNED_CAPACITY_MINUTES,
    PROCRASTINATION_SCALE,
)
from app.core.time import containing_local_day, utc_to_local
from app.services.conflict import (
    Conflict,
    Span,
    TimeWindow,
    collect_spans,
    detect_conflicts,
    find_idle_blocks,
    local_day_window,
    local_full_day_window,
    merge_minutes,
)

_TERMINAL_STATUSES = frozenset({"done", "dropped"})


@dataclass(frozen=True)
class Signals:
    """喂给 LLM 与宠物状态机的结构化信号。"""

    conflicts: tuple[Conflict, ...]
    procrastination_index: float      # 0..1
    load_ratio: float                 # 单日最大 计划时长/可用时长
    idle_blocks: tuple[TimeWindow, ...]
    completion_rate_7d: float
    completion_rate_today: float
    overdue_count: int
    avg_overdue_days: float
    streak_days: int
    due_total_7d: int


# ==================================================================
# 拖延指数
# ==================================================================

def procrastination_index(
    overdue_count: int,
    avg_overdue_days: float,
    due_total_7d: int,
) -> float:
    """v1 公式（用户裁决）：逾期任务数 × 平均逾期天数 ÷ 近 7 天应完成总数，归一化到 0..1。

        raw   = (overdue_count * avg_overdue_days) / max(1, due_total_7d)
        index = clamp01(raw / PROCRASTINATION_SCALE)

    `raw` 的量纲是「平均逾期天数 × 逾期比例」。当全部任务都逾期且平均逾期
    `PROCRASTINATION_SCALE` 天时取 1.0。

    纯函数：无 IO、无时钟依赖。
    """
    if due_total_7d <= 0:
        # 近 7 天没有应完成的任务 -> 谈不上拖延。
        # 这里返回 0 而不是默认值：没有分母时不该凭空判定用户有问题。
        return 0.0
    raw = (overdue_count * max(0.0, avg_overdue_days)) / float(due_total_7d)
    return clamp01(raw / PROCRASTINATION_SCALE)


def clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


# ==================================================================
# 派生计算
# ==================================================================

def _overdue_items(records: Sequence, now: datetime) -> list:
    """结束时间已过且未终结（未完成 / 未放弃）的记录。文档 B14。"""
    out = []
    for r in records:
        if getattr(r, "status", None) in _TERMINAL_STATUSES:
            continue
        end = getattr(r, "end_at", None)
        if end is not None and end < now:
            out.append(r)
    return out


def avg_overdue_days(items: Sequence, now: datetime) -> float:
    """平均逾期天数。单条的上限会被 MAX_OVERDUE_DAYS 截断，
    避免一条挂了半年的任务把整体指标拉爆。"""
    if not items:
        return 0.0
    total = 0.0
    for r in items:
        days = (now - r.end_at).total_seconds() / 86400.0
        total += min(MAX_OVERDUE_DAYS, max(0.0, days))
    return total / len(items)


def due_total(records: Sequence, now: datetime, window: timedelta) -> list:
    """时间窗内「应完成」的记录（不论完成情况）。以 end_at 是否落在窗口内为准。"""
    start = now - window
    out = []
    for r in records:
        end = getattr(r, "end_at", None)
        if end is not None and start <= end <= now:
            out.append(r)
    return out


def completion_rate(records: Sequence, now: datetime, window: timedelta) -> tuple[float, int]:
    """近 N 天完成率。返回 (比率, 样本总数)。

    样本为空时返回 EMPTY_COMPLETION_RATE —— 没有数据不该被判为 0 分，
    否则新用户打开应用第一天就会被"你什么都做不好"打招呼，这是最典型的负罪感陷阱。
    """
    sample = due_total(records, now, window)
    if not sample:
        return EMPTY_COMPLETION_RATE, 0
    done = sum(1 for r in sample if getattr(r, "status", None) == "done")
    return done / len(sample), len(sample)


def completion_dates(records: Sequence, events: Sequence) -> set[date]:
    """有完成行为的本地日期集合。

    同时看两个来源，取并集：
      * `task_completed` 事件 —— 新版的正式记录途径
      * `status == done` 的记录 —— 兜底。导入的旧数据若没加 --emit-events，
        就没有事件，但不能因此认为用户从未完成过任何事。
    """
    days: set[date] = set()
    for e in events:
        if getattr(e, "kind", None) == "task_completed":
            at = getattr(e, "at", None)
            if at is not None:
                days.add(containing_local_day(at))
    for r in records:
        if getattr(r, "status", None) == "done":
            end = getattr(r, "end_at", None)
            if end is not None:
                days.add(containing_local_day(end))
    return days


def streak_days(days: Iterable[date], today: date) -> int:
    """连续有完成的天数。允许「今天还没完成」不破功——从昨天往前数。

    连续 ≥2 天才算 streak；今天完成但昨天没有 -> 1 天。
    """
    dayset = set(days)
    anchor = today if today in dayset else today - timedelta(days=1)
    if anchor not in dayset:
        return 0
    streak = 0
    cursor = anchor
    while cursor in dayset:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def today_completion_rate(records: Sequence, now: datetime) -> float:
    """当日完成率。用于宠物 `great` 档判定。

    与 7 日完成率的默认值不同：**今天一条计划都没有时返回 0.0**。
    7 日率要防御误伤新用户（所以给中性值），而当日的 `great` 档必须有实打实的证据
    ——没排任何计划却让猫说"你今天真棒"，是虚假正反馈。
    """
    local_now = utc_to_local(now)
    sample = [
        r for r in records
        if getattr(r, "end_at", None) is not None
        and containing_local_day(r.end_at) == local_now.date()
        and getattr(r, "status", None) != "dropped"
    ]
    if not sample:
        return 0.0
    done = sum(1 for r in sample if getattr(r, "status", None) == "done")
    return done / len(sample)


def max_load_ratio(spans: Sequence[Span], horizon: TimeWindow) -> float:
    """窗口内单日最大负载比 = 当天排掉的分钟数 / 单日承载上限。

    口径必须与 `conflict.detect_overload` 完全一致（同一个分母 PLANNED_CAPACITY_MINUTES），
    否则会出现"宠物说很满但冲突列表没有 overload"这类自相矛盾的输出。
    """
    if horizon.start >= horizon.end or not spans:
        return 0.0
    best = 0.0
    day = containing_local_day(horizon.start)
    last = containing_local_day(horizon.end - timedelta(microseconds=1))
    while day <= last:
        window = local_full_day_window(day)
        involved = [s for s in spans if TimeWindow(s.start, s.end).intersects(window)]
        if involved:
            used = merge_minutes(involved, window)
            best = max(best, used / PLANNED_CAPACITY_MINUTES)
        day += timedelta(days=1)
    return round(best, 3)


def collect_idle_blocks(spans: Sequence[Span], horizon: TimeWindow) -> tuple[TimeWindow, ...]:
    """窗口内的空档。按本地日历天切分，跨天不会出现「23:00 → 次日 08:00」这种假空档。"""
    if horizon.start >= horizon.end or not spans:
        return ()
    blocks: list[TimeWindow] = []
    day = containing_local_day(horizon.start)
    last = containing_local_day(horizon.end - timedelta(microseconds=1))
    while day <= last:
        window, _ = local_day_window(day)
        clipped = window.intersect(horizon)
        if clipped is not None and clipped.start < clipped.end:
            blocks.extend(find_idle_blocks(spans, clipped))
        day += timedelta(days=1)
    # 按时长降序，让 LLM / UI 优先看到最大的那块空闲
    blocks.sort(key=lambda b: -b.duration_minutes())
    return tuple(blocks)


# ==================================================================
# 主入口
# ==================================================================

def aggregate(records: Sequence, events: Sequence, now: datetime) -> Signals:
    """聚合全部信号。纯函数：`now` 由调用方注入。"""
    horizon = TimeWindow(now, now + FORWARD_WINDOW)

    conflicts = tuple(detect_conflicts(records, horizon))
    spans = collect_spans(records)

    overdue = _overdue_items(records, now)
    sample = due_total(records, now, LOOKBACK_WINDOW)
    rate_7d, n7 = completion_rate(records, now, LOOKBACK_WINDOW)
    index = procrastination_index(
        overdue_count=len(overdue),
        avg_overdue_days=avg_overdue_days(overdue, now),
        due_total_7d=len(sample),
    )

    local_now = utc_to_local(now)
    days = completion_dates(records, events)

    return Signals(
        conflicts=conflicts,
        procrastination_index=round(index, 4),
        load_ratio=max_load_ratio(spans, horizon),
        idle_blocks=collect_idle_blocks(spans, horizon),
        completion_rate_7d=round(rate_7d, 4),
        completion_rate_today=round(today_completion_rate(records, now), 4),
        overdue_count=len(overdue),
        avg_overdue_days=round(avg_overdue_days(overdue, now), 3),
        streak_days=streak_days(days, local_now.date()),
        due_total_7d=n7,
    )


def active_day_window(now: datetime) -> TimeWindow:
    """当前本地日的活跃窗口，供 UI 与演示直接使用。"""
    return local_day_window(utc_to_local(now).date())[0]


__all__ = [
    "Signals",
    "aggregate",
    "procrastination_index",
    "completion_rate",
    "completion_dates",
    "streak_days",
    "today_completion_rate",
    "clamp01",
    "active_day_window",
]
