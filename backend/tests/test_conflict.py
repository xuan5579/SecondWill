"""冲突检测 —— 对抗性样例。

用户明确点名要的四类边界：**完全包含 / 边界相等 / 首尾相接 / 跨天区间**，
外加一条最有价值的：**跨零点**（23:30 → 次日 01:00）。

为什么特别在意跨零点：旧版 Tkinter 应用把时间存成「当日分钟数」，
`23:30→01:00` 会被算成长度 0 且**完全检测不到冲突**——静默失效，没有任何报错。
新版用绝对 datetime，这里把该用例钉死，防止有人后来重构回分钟数。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402

from app.core.constants import (  # noqa: E402
    CONFLICT_BUFFER_MINUTES,
    CONFLICT_OVERLOAD_MIN_COUNT,
    IDLE_BLOCK_MIN_MINUTES,
    PLANNED_CAPACITY_MINUTES,
)
from app.core.time import local_to_utc  # noqa: E402
from app.services.conflict import (  # noqa: E402
    Conflict,
    TimeWindow,
    collect_spans,
    detect_conflicts,
    has_hard_conflict,
    local_full_day_window,
)

D = date(2026, 10, 28)     # 周二


class R:
    """最小够用的假 Record，只实现冲突检测用到的字段。"""

    def __init__(self, rid, start, end, status="pending"):
        self.id = rid
        self.start_at = start
        self.end_at = end
        self.status = status


def L(day_offset: int, hh: int, mm: int = 0, day: date | None = None) -> datetime:
    base = day or D
    return local_to_utc(datetime.combine(base + timedelta(days=day_offset), time(hh, mm)))


def horizon(days: int = 3) -> TimeWindow:
    return TimeWindow(L(0, 0, 0), L(days, 0, 0))


def only(cfgs) -> Conflict:
    assert len(cfgs) == 1, f"期望恰好 1 条冲突，实际 {len(cfgs)}: {cfgs}"
    return cfgs[0]


# ==================================================================
class TestOverlap:
    def test_partial_overlap_is_hard(self):
        """09:00-11:00 与 10:30-11:30 -> 重叠 30 分钟。"""
        c = only(detect_conflicts(
            [R("a", L(0, 9), L(0, 11)), R("b", L(0, 10, 30), L(0, 11, 30))], horizon()
        ))
        assert c.kind == "overlap"
        assert c.hard is True
        assert c.overlap_minutes == 30
        assert c.record_ids == ("a", "b")

    def test_full_containment_a_wraps_b(self):
        """完全包含：a 包住 b，重叠量 = b 的长度。"""
        c = only(detect_conflicts(
            [R("a", L(0, 9), L(0, 12)), R("b", L(0, 10), L(0, 10, 30))], horizon()
        ))
        assert c.kind == "overlap"
        assert c.overlap_minutes == 30

    def test_full_containment_reversed_input_order(self):
        """同上但输入顺序调换 —— 结果必须与顺序无关。"""
        c = only(detect_conflicts(
            [R("b", L(0, 10), L(0, 10, 30)), R("a", L(0, 9), L(0, 12))], horizon()
        ))
        assert c.kind == "overlap"
        assert c.overlap_minutes == 30
        assert c.record_ids == ("a", "b")     # id 已归一化为排序 tuple

    def test_identical_intervals(self):
        """两条完全一样的日程 —— 边界中的边界。"""
        c = only(detect_conflicts(
            [R("a", L(0, 9), L(0, 10)), R("b", L(0, 9), L(0, 10))], horizon()
        ))
        assert c.kind == "overlap" and c.overlap_minutes == 60

    def test_same_start_different_end(self):
        c = only(detect_conflicts(
            [R("a", L(0, 9), L(0, 10)), R("b", L(0, 9), L(0, 12))], horizon()
        ))
        assert c.kind == "overlap" and c.overlap_minutes == 60

    def test_one_second_overlap(self):
        """最小重叠粒度。半开区间语义下 [9:59:59, 10:00:00) 仍算重叠。"""
        start = L(0, 9)
        c = only(detect_conflicts(
            [R("a", start, start + timedelta(minutes=1)),
             R("b", start + timedelta(seconds=59), start + timedelta(minutes=2))],
            horizon(),
        ))
        assert c.kind == "overlap"
        assert c.overlap_minutes in (0, 1)      # 1 秒会被四舍五入成 0 或 1


class TestBoundaryEquality:
    def test_back_to_back_is_not_overlap(self):
        """首尾相接（10:00 结束 / 10:00 开始）按约定**不算**重叠。"""
        cfgs = detect_conflicts(
            [R("a", L(0, 9), L(0, 10)), R("b", L(0, 10), L(0, 11))], horizon()
        )
        assert not any(c.kind == "overlap" for c in cfgs)

    def test_back_to_back_is_no_buffer_because_zero_transition_time(self):
        """但零切换时间确实是摩擦 —— 归为软冲突 no_buffer，可被 LLM 权衡。"""
        c = only(detect_conflicts(
            [R("a", L(0, 9), L(0, 10)), R("b", L(0, 10), L(0, 11))], horizon()
        ))
        assert c.kind == "no_buffer"
        assert c.hard is False
        assert c.overlap_minutes == 0

    def test_gap_exactly_at_buffer_threshold_is_fine(self):
        """间隔刚好等于缓冲阈值（10 分钟）—— 判定条件是严格小于，应无冲突。"""
        cfgs = detect_conflicts(
            [R("a", L(0, 9), L(0, 10)),
             R("b", L(0, 10, CONFLICT_BUFFER_MINUTES), L(0, 11))],
            horizon(),
        )
        assert cfgs == []

    def test_gap_one_minute_below_threshold_is_conflict(self):
        """间隔 = 阈值 - 1 分钟 -> 报 no_buffer。边界内侧。"""
        c = only(detect_conflicts(
            [R("a", L(0, 9), L(0, 10)),
             R("b", L(0, 10, CONFLICT_BUFFER_MINUTES - 1), L(0, 11))],
            horizon(),
        ))
        assert c.kind == "no_buffer"
        assert c.overlap_minutes == CONFLICT_BUFFER_MINUTES - 1


class TestCrossMidnight:
    def test_cross_midnight_records_overlap(self):
        """核心用例：23:30→次日 01:00 与 次日 00:30→02:00 必须检测为重叠 30 分钟。

        旧版在此静默失效（end_minutes=60 < start_minutes=1410 -> 判不出任何冲突）。
        """
        c = only(detect_conflicts(
            [R("a", L(0, 23, 30), L(1, 1, 0)),
             R("b", L(1, 0, 30), L(1, 2, 0))],
            horizon(),
        ))
        assert c.kind == "overlap"
        assert c.hard is True
        assert c.overlap_minutes == 30

    def test_cross_midnight_duration_is_90_minutes_not_zero(self):
        """跨零点区间的时长必须正确，不能被算成 0（旧版的 duration() 会）。"""
        spans = collect_spans([R("a", L(0, 23, 30), L(1, 1, 0))])
        assert len(spans) == 1
        assert spans[0].minutes == 90

    def test_cross_midnight_back_to_back(self):
        """23:30→01:00 与 01:00→02:00 首尾相接 -> no_buffer。"""
        c = only(detect_conflicts(
            [R("a", L(0, 23, 30), L(1, 1, 0)),
             R("b", L(1, 1, 0), L(1, 2, 0))],
            horizon(),
        ))
        assert c.kind == "no_buffer"

    def test_cross_midnight_contains_next_day_task(self):
        """跨零点长区间完全包住次日清晨的短任务。"""
        c = only(detect_conflicts(
            [R("a", L(0, 23, 0), L(1, 8, 0)),
             R("b", L(1, 2, 0), L(1, 3, 0))],
            horizon(),
        ))
        assert c.kind == "overlap"
        assert c.overlap_minutes == 60

    def test_cross_midnight_no_relation(self):
        """两条都在深夜但互不相干 -> 无冲突。防止时区换算把不相关的拉到一起。"""
        cfgs = detect_conflicts(
            [R("a", L(0, 23, 30), L(1, 1, 0)),
             R("b", L(1, 5, 0), L(1, 6, 0))],
            horizon(),
        )
        assert cfgs == []


class TestMultiDaySpan:
    def test_multiday_record_overlaps_middle_day(self):
        """周一 22:00 → 周三 10:00 的超长区间，与周二 09:00-10:00 冲突。"""
        c = only(detect_conflicts(
            [R("a", L(0, 22, 0), L(2, 10, 0)),
             R("b", L(1, 9, 0), L(1, 10, 0))],
            horizon(),
        ))
        assert c.kind == "overlap"
        assert c.overlap_minutes == 60

    def test_three_day_span(self):
        c = only(detect_conflicts(
            [R("a", L(0, 0, 0), L(3, 0, 0)),
             R("b", L(2, 14, 0), L(2, 15, 0))],
            horizon(days=4),
        ))
        assert c.kind == "overlap" and c.overlap_minutes == 60


class TestExclusions:
    def test_done_records_do_not_conflict(self):
        cfgs = detect_conflicts(
            [R("a", L(0, 9), L(0, 11), status="done"),
             R("b", L(0, 10), L(0, 10, 30))],
            horizon(),
        )
        assert cfgs == []

    def test_dropped_records_do_not_conflict(self):
        cfgs = detect_conflicts(
            [R("a", L(0, 9), L(0, 11)),
             R("b", L(0, 10), L(0, 10, 30), status="dropped")],
            horizon(),
        )
        assert cfgs == []

    def test_zero_length_span_is_ignored(self):
        """[t, t) 在半开区间语义下是空集；放过去会产生大量噪音级 no_buffer。"""
        t = L(0, 9)
        cfgs = detect_conflicts(
            [R("a", t, t), R("b", L(0, 9), L(0, 10))], horizon()
        )
        assert cfgs == []

    def test_reversed_interval_is_ignored(self):
        """end 早于 start（脏数据）不该被当成有效占用。"""
        cfgs = detect_conflicts(
            [R("a", L(0, 11), L(0, 9)), R("b", L(0, 10), L(0, 10, 30))], horizon()
        )
        assert cfgs == []

    def test_missing_end_is_ignored(self):
        cfgs = detect_conflicts(
            [R("a", L(0, 9), None), R("b", L(0, 9, 30), L(0, 10))], horizon()
        )
        assert cfgs == []


class TestOverload:
    def _busy_day(self, n: int, each_minutes: int) -> list[R]:
        """在同一天连续排 n 条 each_minutes 分钟的任务。"""
        out = []
        cursor = datetime.combine(D, time(6, 0))
        for i in range(n):
            start = cursor
            end = start + timedelta(minutes=each_minutes)
            out.append(R(f"r{i}", local_to_utc(start), local_to_utc(end)))
            cursor = end
        return out

    def test_overload_detected(self):
        """13 小时计划 > 1.2 × 10h 承载上限 -> overload。"""
        recs = self._busy_day(13, 60)
        cfgs = detect_conflicts(recs, horizon())
        overload = [c for c in cfgs if c.kind == "overload"]
        assert len(overload) == 1
        assert overload[0].hard is False
        assert overload[0].load_ratio > 1.2

    def test_normal_day_is_not_overload(self):
        """正常 6 小时 -> 不该报 overload。"""
        cfgs = detect_conflicts(self._busy_day(6, 60), horizon())
        assert not any(c.kind == "overload" for c in cfgs)

    def test_overload_needs_minimum_record_count(self):
        """只有 2 条超长任务不算「塞太满」——避免把单条大块任务误判为过载。"""
        assert CONFLICT_OVERLOAD_MIN_COUNT > 2
        cfgs = detect_conflicts(self._busy_day(2, 60 * 8), horizon())
        assert not any(c.kind == "overload" for c in cfgs)

    def test_overload_is_soft(self):
        """overload 必须是软冲突：LLM 可以判定"今天忙是合理的"而不重排。"""
        cfgs = detect_conflicts(self._busy_day(14, 60), horizon())
        overload = [c for c in cfgs if c.kind == "overload"]
        assert overload and all(c.hard is False for c in overload)
        assert not has_hard_conflict(overload)


class TestDeterminism:
    def test_result_independent_of_input_order(self):
        recs = [
            R("a", L(0, 9), L(0, 11)),
            R("b", L(0, 10, 30), L(0, 11, 30)),
            R("c", L(0, 14), L(0, 15)),
        ]
        base = detect_conflicts(recs, horizon())
        for perm in ([2, 0, 1], [1, 2, 0], [2, 1, 0]):
            shuffled = detect_conflicts([recs[i] for i in perm], horizon())
            assert [(c.kind, c.record_ids) for c in shuffled] == \
                   [(c.kind, c.record_ids) for c in base]

    def test_empty_horizon_returns_nothing(self):
        recs = [R("a", L(0, 9), L(0, 11))]
        assert detect_conflicts(recs, TimeWindow(L(0, 12), L(0, 12))) == []

    def test_has_hard_conflict_helper(self):
        cfgs = detect_conflicts(
            [R("a", L(0, 9), L(0, 11)), R("b", L(0, 10), L(0, 12))], horizon()
        )
        assert has_hard_conflict(cfgs) is True
        assert has_hard_conflict([]) is False
