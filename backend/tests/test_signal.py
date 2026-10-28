"""信号聚合 —— 纯函数单测。

重点：拖延指数公式是用户裁决过的，必须逐边界钉死；
且这些函数必须能在**不启动 Web、不碰数据库**的情况下运行。
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
    EMPTY_COMPLETION_RATE,
    MAX_OVERDUE_DAYS,
    PROCRASTINATION_SCALE,
)
from app.core.time import local_to_utc  # noqa: E402
from app.services.signal import (  # noqa: E402
    aggregate,
    clamp01,
    completion_dates,
    procrastination_index,
    streak_days,
    today_completion_rate,
)

NOW = local_to_utc(datetime.combine(date(2026, 10, 28), time(14, 0)))
D0 = date(2026, 10, 28)


class R:
    def __init__(self, rid, start=None, end=None, status="pending", created_at=None):
        self.id = rid
        self.start_at = start
        self.end_at = end
        self.status = status
        self.created_at = created_at or start


class E:
    def __init__(self, kind, at):
        self.kind = kind
        self.at = at


def L(day, hh, mm=0) -> datetime:
    return local_to_utc(datetime.combine(day, time(hh, mm)))


class TestProcrastinationIndex:
    def test_zero_when_nothing_overdue(self):
        assert procrastination_index(0, 0.0, 10) == 0.0

    def test_zero_when_no_tasks_due(self):
        """近 7 天没有应完成任务 -> 不算拖延。没有分母时不该凭空判定用户有问题。"""
        assert procrastination_index(5, 3.0, 0) == 0.0

    def test_index_is_bounded(self):
        """极端输入也必须落在 0..1，这是喂给 LLM 的值，越界会让 Prompt 失效。"""
        assert procrastination_index(9999, 999.0, 1) == 1.0
        assert procrastination_index(0, 0.0, 1) == 0.0

    def test_all_overdue_and_scale_days_gives_one(self):
        """全部逾期且平均逾期 = SCALE 天 -> 恰好归一到 1.0。"""
        assert procrastination_index(10, float(PROCRASTINATION_SCALE), 10) == pytest.approx(1.0)

    def test_half_scale_gives_half(self):
        """线性段：一半 -> 0.5。"""
        v = procrastination_index(10, PROCRASTINATION_SCALE / 2, 10)
        assert v == pytest.approx(0.5)

    def test_monotonic_in_overdue_count(self):
        a = procrastination_index(1, 5.0, 10)
        b = procrastination_index(5, 5.0, 10)
        c = procrastination_index(10, 5.0, 10)
        assert a < b < c

    def test_negative_days_are_clamped_to_zero(self):
        """脏数据不该让指数变成负数。"""
        assert procrastination_index(3, -5.0, 10) == 0.0


class TestStreak:
    def test_no_history_is_zero(self):
        assert streak_days([], D0) == 0

    def test_today_only_is_one(self):
        assert streak_days([D0], D0) == 1

    def test_yesterday_only_counts_from_yesterday(self):
        """今天还没完成不该破功 —— 从昨天往前数。"""
        days = [D0 - timedelta(days=1), D0 - timedelta(days=2)]
        assert streak_days(days, D0) == 2

    def test_gap_breaks_streak(self):
        days = [D0, D0 - timedelta(days=1), D0 - timedelta(days=3)]
        assert streak_days(days, D0) == 2

    def test_stale_history_is_zero(self):
        """最后一次完成是上周 -> 连续天数归零，但数据不丢。"""
        assert streak_days([D0 - timedelta(days=10)], D0) == 0


class TestCompletionRates:
    def test_empty_sample_is_neutral_not_zero(self):
        """新用户打开第一天不该被判 0 分 —— 最典型的负罪感陷阱。"""
        from app.services.signal import completion_rate

        rate, n = completion_rate([], NOW, timedelta(days=7))
        assert n == 0
        assert rate == EMPTY_COMPLETION_RATE

    def test_today_rate_zero_when_no_plans(self):
        """今日完成率必须是真凭实据：没排就不算完成得好。"""
        assert today_completion_rate([], NOW) == 0.0

    def test_today_rate_all_done(self):
        recs = [
            R("a", L(D0, 9), L(D0, 10), status="done"),
            R("b", L(D0, 10), L(D0, 11), status="done"),
        ]
        assert today_completion_rate(recs, NOW) == 1.0

    def test_today_rate_partial(self):
        recs = [
            R("a", L(D0, 9), L(D0, 10), status="done"),
            R("b", L(D0, 10), L(D0, 11), status="pending"),
        ]
        assert today_completion_rate(recs, NOW) == pytest.approx(0.5)

    def test_completion_dates_union_of_events_and_records(self):
        """事件缺失（未加 --emit-events 的导入数据）时，
        status=done 的记录本身也必须算作完成痕迹，否则历史被抹掉。"""
        recs = [R("a", L(D0 - timedelta(days=2), 9), L(D0 - timedelta(days=2), 10), status="done")]
        events = [E("task_completed", L(D0, 9))]
        days = completion_dates(recs, events)
        assert D0 in days
        assert D0 - timedelta(days=2) in days


class TestAggregate:
    def test_returns_complete_signals(self):
        signals = aggregate([], [], NOW)
        assert signals.procrastination_index == 0.0
        assert signals.overdue_count == 0
        assert signals.conflicts == ()
        assert signals.load_ratio == 0.0

    def test_overdue_records_are_counted(self):
        recs = [
            R("a", L(D0 - timedelta(days=3), 9), L(D0 - timedelta(days=3), 10), status="pending"),
            R("b", L(D0 - timedelta(days=1), 9), L(D0 - timedelta(days=1), 10), status="pending"),
        ]
        signals = aggregate(recs, [], NOW)
        assert signals.overdue_count == 2
        assert signals.avg_overdue_days > 0

    def test_done_records_are_not_overdue(self):
        recs = [R("a", L(D0 - timedelta(days=5), 9), L(D0 - timedelta(days=5), 10), status="done")]
        assert aggregate(recs, [], NOW).overdue_count == 0

    def test_avg_overdue_days_is_capped(self):
        """一条挂了半年的任务不该把平均值拉爆。"""
        recs = [R("a", L(D0 - timedelta(days=365), 9), L(D0 - timedelta(days=365), 10))]
        assert aggregate(recs, [], NOW).avg_overdue_days <= MAX_OVERDUE_DAYS

    def test_clamp01(self):
        assert clamp01(-1.0) == 0.0
        assert clamp01(0.4) == 0.4
        assert clamp01(9.9) == 1.0
