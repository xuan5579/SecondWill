"""干预节奏闸门的纯函数测试（P5-a）。

这一组**完全不碰数据库、不碰 Web**，与 `test_conflict.py` / `test_pet.py` 同一性质：
闸门是纯逻辑，`now` 由调用方注入，因此"下周三还能不能发"这类问题
可以在毫秒级被反复验证，不必伪造时间或等待。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.constants import (
    INTERVENTION_COOLDOWN_MINUTES,
    INTERVENTION_DAILY_LIMIT,
    INTERVENTION_MUTE_THRESHOLD,
)
from app.core.time import local_to_utc
from app.services.rhythm import (
    InterventionTrace,
    accept_counts,
    evaluate_rhythm,
    feedback_profile,
    ignore_counts,
    is_muted,
    muted_types,
)

#: 固定基线：本地 2026-10-30 10:00。所有时刻都由它偏移得出，
#: 测试不依赖"现在几点"，因此任何时区、任何运行时刻结果都一致。
NOW = local_to_utc(datetime(2026, 10, 30, 10, 0))


def trace(
    decision_type: str = "reschedule",
    *,
    minutes_ago: int = 0,
    feedback: str | None = None,
    feedback_minutes_ago: int | None = None,
    metered: bool = True,
) -> InterventionTrace:
    at = NOW - timedelta(minutes=minutes_ago)
    return InterventionTrace(
        decision_type=decision_type,
        created_at=at,
        delivered_at=at,
        feedback=feedback,
        feedback_at=(
            NOW - timedelta(minutes=feedback_minutes_ago)
            if feedback_minutes_ago is not None
            else (at if feedback else None)
        ),
        metered=metered,
    )


# ==================================================================
# B4 · 冷却期
# ==================================================================


def test_no_history_always_passes():
    v = evaluate_rhythm([], "schedule_change", NOW)
    assert v.can_deliver is True
    assert v.blocked_by == ""
    assert v.remaining_today == INTERVENTION_DAILY_LIMIT


def test_cooldown_blocks_recent_auto_trigger():
    """刚发过一条，立刻又来一次 —— 必须拦下。"""
    v = evaluate_rhythm([trace(minutes_ago=10)], "schedule_change", NOW)
    assert v.can_deliver is False
    assert v.blocked_by == "cooldown"
    assert v.next_allowed_at is not None
    assert "冷却期" in v.reason


def test_cooldown_expires_after_threshold():
    just_inside = evaluate_rhythm(
        [trace(minutes_ago=INTERVENTION_COOLDOWN_MINUTES - 1)], "schedule_change", NOW
    )
    just_outside = evaluate_rhythm(
        [trace(minutes_ago=INTERVENTION_COOLDOWN_MINUTES + 1)], "schedule_change", NOW
    )
    assert just_inside.can_deliver is False
    assert just_outside.can_deliver is True


def test_snooze_restarts_the_cooldown_clock():
    """「稍后提醒」从反馈时刻重新起算，而不是从送达时刻。

    否则用户点了稍后、冷却期刚好在几秒后到期，体验上就是"点了没用"。
    """
    t = trace(minutes_ago=60, feedback="snooze", feedback_minutes_ago=5)
    # 送达在 60 分钟前（已过冷却），但反馈在 5 分钟前（未过冷却）
    assert t.effective_at == t.feedback_at
    v = evaluate_rhythm([t], "schedule_change", NOW)
    assert v.can_deliver is False
    assert v.blocked_by == "cooldown"


def test_accept_does_not_restart_the_clock():
    """采纳意味着这次交互已经结束，不该因为它而延后下一次。"""
    t = trace(minutes_ago=60, feedback="accept", feedback_minutes_ago=5)
    assert t.effective_at == t.delivered_at
    assert evaluate_rhythm([t], "schedule_change", NOW).can_deliver is True


# ==================================================================
# B5 · 每日上限
# ==================================================================


def test_daily_limit_blocks_the_next_one():
    history = [trace(minutes_ago=60 * (i + 1)) for i in range(INTERVENTION_DAILY_LIMIT)]
    v = evaluate_rhythm(history, "schedule_change", NOW)
    assert v.can_deliver is False
    assert v.blocked_by == "daily_limit"
    assert v.remaining_today == 0
    assert v.used_today == INTERVENTION_DAILY_LIMIT
    assert "手动触发仍可用" in v.reason


def test_daily_limit_resets_on_the_next_local_day():
    """额度按**本地日**重置，不是按 24 小时滚动。

    用户心里的一天是"今天"，不是"过去 24 小时"。
    """
    tomorrow = local_to_utc(datetime(2026, 10, 31, 9, 0))
    history = [
        trace(minutes_ago=int((NOW - local_to_utc(datetime(2026, 10, 30, 8, 0))).total_seconds() // 60))
        for _ in range(INTERVENTION_DAILY_LIMIT)
    ]
    assert evaluate_rhythm(history, "schedule_change", NOW).can_deliver is False
    assert evaluate_rhythm(history, "schedule_change", tomorrow).can_deliver is True


def test_daily_limit_is_checked_before_cooldown():
    """两条都违反时，报的是"今日用完"而不是"再等 40 分钟" —— 后者会误导。"""
    history = [trace(minutes_ago=1)] + [
        trace(minutes_ago=60 * (i + 2)) for i in range(INTERVENTION_DAILY_LIMIT - 1)
    ]
    v = evaluate_rhythm(history, "schedule_change", NOW)
    assert v.blocked_by == "daily_limit"


# ==================================================================
# 手动触发
# ==================================================================


def test_manual_bypasses_both_limits():
    """用户主动问一次，不该被自己的助手以"额度用完"拒绝。

    那是在惩罚用户使用产品。
    """
    history = [trace(minutes_ago=1)] * INTERVENTION_DAILY_LIMIT
    v = evaluate_rhythm(history, "manual", NOW)
    assert v.can_deliver is True
    assert "绕过节奏闸门" in v.reason


def test_manual_delivery_is_not_metered():
    """手动干预不占今日额度：连续手动问 5 次之后，自动干预仍应有额度。"""
    history = [trace(minutes_ago=60 * (i + 1), metered=False) for i in range(5)]
    v = evaluate_rhythm(history, "schedule_change", NOW)
    assert v.used_today == 0
    assert v.can_deliver is True


def test_manual_still_pushes_the_cooldown_clock():
    """但手动**会**刷新冷却时钟 —— 点一次不该引来一串自动干预。

    next_allowed_at 非 None 正是这个语义的落点。
    """
    v = evaluate_rhythm([trace(minutes_ago=5, metered=False)], "manual", NOW)
    assert v.can_deliver is True
    assert v.next_allowed_at is not None


# ==================================================================
# B6 · 降权
# ==================================================================


def test_ignore_counts_only_snooze_and_mute():
    traces = [
        trace(feedback="accept"),
        trace(feedback="snooze"),
        trace(feedback="mute"),
        trace(feedback=None),
    ]
    assert ignore_counts(traces) == {"reschedule": 2}
    assert accept_counts(traces) == {"reschedule": 1}


def test_type_is_muted_only_after_threshold():
    for n in range(1, INTERVENTION_MUTE_THRESHOLD):
        traces = [trace(feedback="mute") for _ in range(n)]
        assert muted_types(traces) == [], f"忽略 {n} 次不该降权"
        assert is_muted("reschedule", traces) is False

    traces = [trace(feedback="mute") for _ in range(INTERVENTION_MUTE_THRESHOLD)]
    assert muted_types(traces) == ["reschedule"]
    assert is_muted("reschedule", traces) is True


def test_mute_is_per_type():
    """降权按类型隔离：嫌「重排」烦，不代表连「提醒一句」也不想听。"""
    traces = [trace("reschedule", feedback="mute") for _ in range(INTERVENTION_MUTE_THRESHOLD)]
    traces += [trace("nudge", feedback="mute")]
    assert muted_types(traces) == ["reschedule"]
    assert is_muted("nudge", traces) is False


def test_feedback_profile_reports_both_directions():
    traces = [
        trace("reschedule", feedback="mute"),
        trace("reschedule", feedback="snooze"),
        trace("reschedule", feedback="mute"),
        trace("nudge", feedback="accept"),
    ]
    p = feedback_profile(traces)
    assert p.muted_intervention_types == ["reschedule"]
    assert p.accepted_intervention_types == ["nudge"]
    assert p.ignore_counts == {"reschedule": INTERVENTION_MUTE_THRESHOLD}
    assert p.accept_counts == {"nudge": 1}
