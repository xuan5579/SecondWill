"""冲突检测 —— 纯函数，确定性，零 IO，零 LLM。

**这是整个系统里最不该交给模型的部分。** 区间重叠是 O(n log n) 的确定性算法，
交给 LLM 又慢又可能算错；模型应该做的是"在多个可行解里选哪个"，而不是"这两条撞没撞车"。

相比旧版 Tkinter 应用的一处重要修复
-------------------------------------
旧版把时间存成"当日分钟数"（`to_minutes("23:30") = 1410`），于是跨零点区间会**静默失效**：
`23:30 → 次日 01:00` 的 `end_minutes = 60`，`duration = max(0, 60 - 1410) = 0`，
`ranges_overlap` 判 `1410 < 60` 为假 —— 冲突完全检测不到，且没有任何报错。

本模块用绝对 datetime 区间，跨零点自然成立。这是有意为之的能力升级，不是顺带修 bug。

三类冲突
--------
=========  ========  ==========================================================
kind       hard      含义
=========  ========  ==========================================================
overlap    True      物理重叠，不可能共存。必须由 LLM 或规则重排解决。
no_buffer  False     首尾相接或间隔过窄，人的切换成本太高。理想但非必须。
overload   False     单日计划量超出可用时长。同理，是可权衡的建议。
=========  ========  ==========================================================

注意 `no_buffer` 覆盖了「首尾相接」（10:00 结束 / 10:00 开始）：
半开区间语义下它**不算重叠**（沿用旧版约定），但零切换时间确实是摩擦，所以归为软冲突。
这个区分是被`/tests`里的对抗性用例钉住的，改动前先看测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Literal, Sequence

from app.core.constants import (
    CONFLICT_BUFFER_MINUTES,
    CONFLICT_EXCLUDED_STATUSES,
    CONFLICT_HARD_KINDS,
    CONFLICT_OVERLOAD_MIN_COUNT,
    CONFLICT_OVERLOAD_RATIO,
    DAY_AVAILABLE_MINUTES,
    DAY_WINDOW_END_HOUR,
    DAY_WINDOW_START_HOUR,
    IDLE_BLOCK_MIN_MINUTES,
    PLANNED_CAPACITY_MINUTES,
)
from app.core.time import containing_local_day, local_to_utc, local_tzinfo, utc_to_local

ConflictKind = Literal["overlap", "no_buffer", "overload"]


@dataclass(frozen=True)
class TimeWindow:
    """一段绝对时间区间 [start, end)。所有字段都是 UTC naive datetime。"""

    start: datetime
    end: datetime

    def duration_minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0

    def intersects(self, other: "TimeWindow") -> bool:
        return self.start < other.end and other.start < self.end

    def intersect(self, other: "TimeWindow") -> "TimeWindow | None":
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return TimeWindow(start, end) if start < end else None


@dataclass(frozen=True)
class Conflict:
    """一条冲突记录。

    `overlap_minutes` 的语义随 kind 变化，读取时按 kind 解释：
        overlap   -> 两段实际相交的分钟数（>0）
        no_buffer -> 两段之间的空档分钟数（< 缓冲阈值，可能为 0）
        overload  -> 超出可用时长的那部分分钟数
    """

    record_ids: tuple[str, ...]
    kind: ConflictKind
    hard: bool
    overlap_minutes: int = 0
    window: TimeWindow | None = None      # 冲突发生的具体区间，供 UI 高亮
    load_ratio: float | None = None       # 仅 overload：计划时长 / 可用时长
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Span:
    """一条有效占用。由 Record 归一化而来，避免业务层依赖 ORM 字段。"""

    record_id: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


def _span_of(record) -> Span | None:
    """把任意带 id/start_at/end_at/status 的对象归一化成 Span。

    返回 None 表示"不参与冲突检测"：
      * 已完成 / 已放弃 —— 它们不再占用未来时间（沿用旧版 exclude_done 语义）
      * 缺 start_at     —— 无从定位
      * 零长度区间      —— [t, t) 在半开区间语义下是空集，且会产生噪音级别的 no_buffer
    """
    if getattr(record, "status", None) in CONFLICT_EXCLUDED_STATUSES:
        return None
    start = getattr(record, "start_at", None)
    end = getattr(record, "end_at", None)
    if start is None:
        return None
    if end is None:
        end = start
    if end <= start:
        return None
    return Span(str(getattr(record, "id")), start, end)


def collect_spans(records: Iterable) -> list[Span]:
    """归一化并按开始时间排序。"""
    spans = [s for s in (_span_of(r) for r in records) if s is not None]
    spans.sort(key=lambda s: (s.start, s.end, s.record_id))
    return spans


# ==================================================================
# 两两比较：overlap / no_buffer
# ==================================================================

def _pair_conflict(a: Span, b: Span) -> Conflict | None:
    """判定一对 spans 之间是否存在冲突，返回 None 表示相安无事。"""
    inter_start = max(a.start, b.start)
    inter_end = min(a.end, b.end)

    if inter_start < inter_end:
        minutes = int(round((inter_end - inter_start).total_seconds() / 60.0))
        return Conflict(
            record_ids=tuple(sorted((a.record_id, b.record_id))),
            kind="overlap",
            hard=True,
            overlap_minutes=max(minutes, 0),
            window=TimeWindow(inter_start, inter_end),
        )

    # 不相交：算空档。a.start <= b.start（已排序），所以 gap = b.start - a.end。
    gap = (b.start - a.end).total_seconds() / 60.0
    if gap < CONFLICT_BUFFER_MINUTES:
        return Conflict(
            record_ids=tuple(sorted((a.record_id, b.record_id))),
            kind="no_buffer",
            hard=False,
            overlap_minutes=int(round(gap)),
            window=TimeWindow(a.end, b.start) if b.start >= a.end else TimeWindow(b.end, a.start),
            detail={"required_buffer_minutes": CONFLICT_BUFFER_MINUTES},
        )
    return None


def detect_pair_conflicts(spans: Sequence[Span]) -> list[Conflict]:
    """两两比较，产出 overlap 与 no_buffer 冲突。

    沿用旧版 `build_conflict_map` 的剪枝：先按开始时间排序，一旦某条的开始时间
    超出「前一条结束 + 缓冲」，后面的不可能再冲突，直接跳出内层循环。
    """
    conflicts: list[Conflict] = []
    frontier = timedelta(minutes=CONFLICT_BUFFER_MINUTES)
    for i, a in enumerate(spans):
        limit = a.end + frontier
        for b in spans[i + 1:]:
            if b.start >= limit:
                break
            found = _pair_conflict(a, b)
            if found is not None:
                conflicts.append(found)
    return conflicts


# ==================================================================
# 单日负载：overload
# ==================================================================

def local_day_window(day: date) -> tuple[TimeWindow, int]:
    """某本地日的**活跃窗口** [08:00, 23:00)，用于空档识别。"""
    return _local_window(day, DAY_WINDOW_START_HOUR, DAY_WINDOW_END_HOUR), DAY_AVAILABLE_MINUTES


def local_full_day_window(day: date) -> TimeWindow:
    """某本地日的**整天窗口** [00:00, 次日 00:00)，用于负载统计。

    负载要统计整天内排掉的时间（包括凌晨的安排），否则「你今天排了 18 小时」
    会因为被裁到活跃窗口内而永远算不出来。
    """
    return _local_window(day, 0, 24)


def _local_window(day: date, start_hour: int, end_hour: int) -> TimeWindow:
    tz = local_tzinfo()
    start = datetime.combine(day, time(hour=0)).replace(tzinfo=tz)
    start += timedelta(hours=start_hour)
    end = start + timedelta(hours=end_hour - start_hour)
    return TimeWindow(
        local_to_utc(start.replace(tzinfo=None)),
        local_to_utc(end.replace(tzinfo=None)),
    )


def merge_minutes(spans: Sequence[Span], window: TimeWindow) -> float:
    """spans 在 window 内占用的分钟数，重叠部分只计一次。"""
    pieces: list[TimeWindow] = []
    for s in spans:
        piece = TimeWindow(s.start, s.end).intersect(window)
        if piece is not None:
            pieces.append(piece)
    if not pieces:
        return 0.0
    pieces.sort(key=lambda p: p.start)
    total = 0.0
    cur_s, cur_e = pieces[0].start, pieces[0].end
    for p in pieces[1:]:
        if p.start <= cur_e:
            cur_e = max(cur_e, p.end)
        else:
            total += (cur_e - cur_s).total_seconds() / 60.0
            cur_s, cur_e = p.start, p.end
    total += (cur_e - cur_s).total_seconds() / 60.0
    return total


def detect_overload(spans: Sequence[Span], horizon: TimeWindow) -> list[Conflict]:
    """按本地日历天检查负载是否超标。

    跨零点的日程按其"起始日"归属，同时其在当天的实际占用按时长交集计算——
    这样 23:30→01:00 只会在起始日贡献 30 分钟，不会重复计入次日。
    """
    if not horizon.start <= horizon.end:
        return []

    conflicts: list[Conflict] = []
    day = containing_local_day(horizon.start)
    last_day = containing_local_day(horizon.end - timedelta(microseconds=1))
    while day <= last_day:
        window = local_full_day_window(day)
        involved = [s for s in spans if TimeWindow(s.start, s.end).intersects(window)]
        if len(involved) >= CONFLICT_OVERLOAD_MIN_COUNT:
            used = merge_minutes(involved, window)
            ratio = used / PLANNED_CAPACITY_MINUTES
            if ratio > CONFLICT_OVERLOAD_RATIO:
                excess = used - PLANNED_CAPACITY_MINUTES * CONFLICT_OVERLOAD_RATIO
                conflicts.append(
                    Conflict(
                        record_ids=tuple(sorted(s.record_id for s in involved)),
                        kind="overload",
                        hard=False,
                        overlap_minutes=int(round(max(0.0, excess))),
                        window=window,
                        load_ratio=round(ratio, 3),
                        detail={
                            "day": day.isoformat(),
                            "used_minutes": int(round(used)),
                            "capacity_minutes": PLANNED_CAPACITY_MINUTES,
                        },
                    )
                )
        day += timedelta(days=1)
    return conflicts


# ==================================================================
# 对外主入口
# ==================================================================

def detect_conflicts(records: Sequence, horizon: TimeWindow) -> list[Conflict]:
    """在给定时间窗口内检测全部三类冲突。

    纯函数：不读数据库、不访问时钟（horizon 由调用方传入）、不调用 LLM。
    """
    if horizon.start >= horizon.end:
        return []

    spans = collect_spans(records)
    # 只保留与窗口有交集的部分，天然支持跨零点长区间
    in_window = [s for s in spans if TimeWindow(s.start, s.end).intersects(horizon)]
    return detect_pair_conflicts(in_window) + detect_overload(in_window, horizon)


def has_hard_conflict(conflicts: Sequence[Conflict]) -> bool:
    """是否存在硬冲突。这是 LLM 触发条件之一（文档 B3）。"""
    return any(c.hard for c in conflicts)


def find_idle_blocks(spans: Sequence[Span], horizon: TimeWindow) -> list[TimeWindow]:
    """窗口内被日程夹住的空闲段（>= IDLE_BLOCK_MIN_MINUTES 才算）。

    与旧版 find_gaps 一致的两条取舍：
      * 只看活跃窗口内（不在凌晨的空白处建议休息很荒谬）
      * 只看**两条日程之间**的空隙；日程之前的空白属于"还没排"，之后的属于"今天结束了"
    """
    if horizon.start >= horizon.end:
        return []

    pieces: list[TimeWindow] = []
    for s in spans:
        piece = TimeWindow(s.start, s.end).intersect(horizon)
        if piece is not None:
            pieces.append(piece)
    pieces.sort(key=lambda p: p.start)

    blocks: list[TimeWindow] = []
    cursor = horizon.start
    for p in pieces:
        if p.end <= cursor:
            continue
        if p.start > cursor:
            gap = TimeWindow(cursor, min(p.start, horizon.end))
            if gap.end > gap.start and gap.duration_minutes() >= IDLE_BLOCK_MIN_MINUTES:
                blocks.append(gap)
        cursor = max(cursor, p.end)
        if cursor >= horizon.end:
            break
    if cursor < horizon.end:
        tail = TimeWindow(cursor, horizon.end)
        if tail.duration_minutes() >= IDLE_BLOCK_MIN_MINUTES:
            blocks.append(tail)
    return blocks


def summarize_local(spans: Sequence[Span]) -> list[str]:
    """调试用：把 spans 打印成本地时间，避免直接看 UTC 误判。"""
    out = []
    for s in spans:
        ls, le = utc_to_local(s.start), utc_to_local(s.end)
        out.append(f"{s.record_id}: {ls:%m-%d %H:%M} → {le:%m-%d %H:%M}")
    return out
