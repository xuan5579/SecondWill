"""宠物六档状态机 —— 纯函数单测，不启动 Web、不碰数据库。

这里是用户裁决的那张阈值表被钉住的地方。每一档的触发条件、优先级短路、
以及 `alert`/`tired` 信号源分离，都必须有对应用例。
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
    PET_GREAT_COMPLETION_MIN,
    PET_GREAT_STREAK_MIN,
    PET_GUILTY_INDEX_MIN,
    PET_MOOD_PRIORITY,
    PET_TIRED_OVERDUE_DAYS,
)
from app.core.time import local_to_utc  # noqa: E402
from app.services.pet import (  # noqa: E402
    LINES,
    PetMood,
    PetState,
    compute,
    dormant_idle_days,
    energy_of,
    template_line,
)
from app.services.signal import Signals  # noqa: E402

NOW = local_to_utc(datetime.combine(date(2026, 10, 28), time(14, 0)))


class FakeRecord:
    def __init__(self, created_at, end_at=None, status="pending"):
        self.id = f"rec_{id(self) % 10000}"
        self.created_at = created_at
        self.start_at = end_at
        self.end_at = end_at
        self.status = status


class FakeEvent:
    def __init__(self, kind, at):
        self.kind = kind
        self.at = at


def L(day: date, t: time) -> datetime:
    return local_to_utc(datetime.combine(day, t))


D0 = date(2026, 10, 28)      # 今天


def signals(**over) -> Signals:
    """构造一份「一切正常」的 Signals，再用 over 覆盖需要的字段。"""
    base = dict(
        conflicts=(),
        procrastination_index=0.0,
        load_ratio=0.0,
        idle_blocks=(),
        completion_rate_7d=0.7,
        completion_rate_today=0.0,
        overdue_count=0,
        avg_overdue_days=0.0,
        streak_days=0,
        due_total_7d=5,
    )
    base.update(over)
    return Signals(**base)


def hard_conflict():
    from app.services.conflict import Conflict, TimeWindow

    return Conflict(
        record_ids=("a", "b"),
        kind="overlap",
        hard=True,
        overlap_minutes=30,
        window=TimeWindow(NOW, NOW + timedelta(minutes=30)),
    )


class TestMoodPriority:
    """优先级短路：alert > dormant > tired > guilty > great > normal。"""

    def test_priority_order_matches_spec(self):
        assert PET_MOOD_PRIORITY == (
            "alert", "dormant", "tired", "guilty", "great", "normal",
        )

    def test_alert_wins_over_all_others(self):
        """alert 优先级最高：即使同时满足回顾类所有条件，也必须取 alert。"""
        s = signals(
            conflicts=(hard_conflict(),),
            streak_days=0,
            overdue_count=9,
            avg_overdue_days=10.0,
            procrastination_index=1.0,
            completion_rate_7d=0.0,
            completion_rate_today=0.0,
        )
        assert compute([], [], s, NOW).mood is PetMood.ALERT

    def test_dormant_wins_over_tired(self):
        s = signals(
            overdue_count=5,
            avg_overdue_days=float(PET_TIRED_OVERDUE_DAYS),
            due_total_7d=8,
        )
        records = [FakeRecord(created_at=L(D0 - timedelta(days=10), time(10, 0)))]
        assert compute(records, [], s, NOW).mood is PetMood.DORMANT

    def test_tired_wins_over_guilty(self):
        s = signals(
            overdue_count=4,
            avg_overdue_days=float(PET_TIRED_OVERDUE_DAYS),
            procrastination_index=PET_GUILTY_INDEX_MIN,
            completion_rate_7d=0.0,
        )
        # 有近期完成，排除 dormant
        events = [FakeEvent("task_completed", L(D0, time(9, 0)))]
        assert compute([], events, s, NOW).mood is PetMood.TIRED

    def test_normal_is_fallback(self):
        assert compute([], [], signals(), NOW).mood is PetMood.NORMAL

    def test_great(self):
        s = signals(
            completion_rate_today=PET_GREAT_COMPLETION_MIN,
            streak_days=PET_GREAT_STREAK_MIN,
        )
        assert compute([], [], s, NOW).mood is PetMood.GREAT


class TestAlertVsTiredSeparation:
    """用户明确要求：前瞻信号与回顾信号不得混用。"""

    def test_alert_comes_only_from_future_conflict(self):
        """只有前瞻冲突能触发 alert；把回顾指标拉满也不行。"""
        s = signals(
            conflicts=(),
            overdue_count=99,
            avg_overdue_days=30.0,
            procrastination_index=1.0,
            completion_rate_7d=0.0,
        )
        assert compute([], [], s, NOW).mood is not PetMood.ALERT

    def test_soft_conflict_does_not_trigger_alert(self):
        """no_buffer / overload 是软冲突，不该点亮前瞻预警。"""
        from app.services.conflict import Conflict, TimeWindow

        soft = Conflict(
            record_ids=("a", "b"),
            kind="no_buffer",
            hard=False,
            overlap_minutes=0,
            window=TimeWindow(NOW, NOW),
        )
        s = signals(conflicts=(soft,))
        assert compute([], [], s, NOW).mood is not PetMood.ALERT

    def test_tired_ignores_conflicts(self):
        """tired 只看回顾指标：有 soft 冲突不影响 tired 判定。"""
        from app.services.conflict import Conflict, TimeWindow

        soft = Conflict(
            record_ids=("a", "b"), kind="overload", hard=False, overlap_minutes=5,
            window=TimeWindow(NOW, NOW),
        )
        s = signals(
            conflicts=(soft,),
            overdue_count=3,
            avg_overdue_days=float(PET_TIRED_OVERDUE_DAYS),
        )
        events = [FakeEvent("task_completed", L(D0, time(9, 0)))]
        assert compute([], events, s, NOW).mood is PetMood.TIRED


class TestDormantGuard:
    def test_brand_new_user_is_not_dormant(self):
        """全新用户（无任何记录）不能被判为停摆——那是赤裸裸的误伤。"""
        assert dormant_idle_days([], [], NOW) == -1
        assert compute([], [], signals(), NOW).mood is PetMood.NORMAL

    def test_user_who_just_started_is_not_dormant(self):
        """刚开始用：有记录但还没到 N 天冷却，不判停摆。"""
        records = [FakeRecord(created_at=L(D0, time(10, 0)))]
        assert compute(records, [], signals(), NOW).mood is PetMood.NORMAL

    def test_long_idle_is_dormant(self):
        records = [FakeRecord(created_at=L(D0 - timedelta(days=20), time(10, 0)))]
        assert compute(records, [], signals(), NOW).mood is PetMood.DORMANT

    def test_recent_completion_cancels_dormant(self):
        records = [FakeRecord(created_at=L(D0 - timedelta(days=20), time(10, 0)))]
        events = [FakeEvent("task_completed", L(D0 - timedelta(days=1), time(9, 0)))]
        assert compute(records, events, signals(), NOW).mood is not PetMood.DORMANT


class TestEnergy:
    def test_energy_is_bounded_0_1(self):
        lo = energy_of(signals(completion_rate_7d=0.0, procrastination_index=1.0))
        hi = energy_of(signals(completion_rate_7d=1.0, procrastination_index=0.0))
        assert 0.0 <= lo <= 1.0
        assert pytest.approx(hi, abs=0.01) == 1.0

    def test_energy_uses_formula_from_spec(self):
        s = signals(completion_rate_7d=0.5, procrastination_index=0.5)
        assert pytest.approx(energy_of(s), abs=1e-6) == 0.6 * 0.5 + 0.4 * 0.5

    def test_energy_uses_7d_rate_not_today(self):
        """能量不该被「今天还没完成」打到底，否则每天早上都是一只蔫猫。"""
        today_bad = signals(completion_rate_7d=0.9, completion_rate_today=0.0,
                            procrastination_index=0.0)
        assert energy_of(today_bad) > 0.5


class TestTemplateLine:
    def test_every_mood_has_lines(self):
        """LLM 挂掉时每一档都得有话可说，不能出现空白宠物。"""
        for mood in PetMood:
            assert len(LINES[mood]) >= 2
            state = PetState(mood=mood, energy=0.5, streak_days=0, computed_at=NOW)
            assert template_line(state, seed=0).strip()

    def test_seed_is_stable(self):
        """同一 seed 必须永远得到同一句——否则刷新页面会跳不同的话。"""
        state = compute([], [], signals(), NOW)
        assert template_line(state, seed=7) == template_line(state, seed=7)
        assert template_line(state, seed=7) != template_line(state, seed=8)

    def test_line_matches_mood(self):
        s = signals(conflicts=(hard_conflict(),))
        state = compute([], [], s, NOW)
        line = template_line(state, seed=0)
        assert "撞" in line or "塞不开" in line


class TestThresholdsComeFromConstants:
    """用户要求：六档阈值集中在常量区，业务代码零硬编码。

    这里不靠"读源码找魔法数字"那种脆弱的方式验证，而是**改常量看行为是否跟着变**——
    只要行为跟着变，就证明阈值确实是从 constants 读的，而不是写死在判定里。
    """

    def test_guilty_threshold_is_wired(self, monkeypatch):
        import app.services.pet as pet_mod

        s = signals(procrastination_index=0.59, completion_rate_7d=0.2)
        assert compute([], [], s, NOW).mood is not PetMood.GUILTY

        monkeypatch.setattr(pet_mod, "PET_GUILTY_INDEX_MIN", 0.5)
        assert compute([], [], s, NOW).mood is PetMood.GUILTY

    def test_great_thresholds_are_wired(self, monkeypatch):
        import app.services.pet as pet_mod

        s = signals(completion_rate_today=0.7, streak_days=2)
        assert compute([], [], s, NOW).mood is not PetMood.GREAT

        monkeypatch.setattr(pet_mod, "PET_GREAT_COMPLETION_MIN", 0.6)
        monkeypatch.setattr(pet_mod, "PET_GREAT_STREAK_MIN", 2)
        assert compute([], [], s, NOW).mood is PetMood.GREAT

    def test_tired_threshold_is_wired(self, monkeypatch):
        import app.services.pet as pet_mod

        s = signals(overdue_count=2, avg_overdue_days=1.5)
        events = [FakeEvent("task_completed", L(D0, time(9, 0)))]
        assert compute([], events, s, NOW).mood is not PetMood.TIRED

        monkeypatch.setattr(pet_mod, "PET_TIRED_OVERDUE_DAYS", 1)
        assert compute([], events, s, NOW).mood is PetMood.TIRED

    def test_priority_order_is_wired(self, monkeypatch):
        """调换优先级顺序必须真的改变结果 —— 证明短路顺序来自常量而非代码里的 if 链。"""
        import app.services.pet as pet_mod

        s = signals(conflicts=(hard_conflict(),), overdue_count=9, avg_overdue_days=9.0)
        assert compute([], [], s, NOW).mood is PetMood.ALERT

        monkeypatch.setattr(pet_mod, "PET_MOOD_PRIORITY", ("tired",) + tuple(
            m for m in PET_MOOD_PRIORITY if m != "tired"
        ))
        assert compute([], [], s, NOW).mood is PetMood.TIRED

    def test_energy_weights_are_wired(self, monkeypatch):
        import app.services.pet as pet_mod

        s = signals(completion_rate_7d=0.0, procrastination_index=0.0)
        assert pytest.approx(energy_of(s), abs=1e-6) == 0.4
        monkeypatch.setattr(pet_mod, "ENERGY_PROCRASTINATION_WEIGHT", 1.0)
        monkeypatch.setattr(pet_mod, "ENERGY_COMPLETION_WEIGHT", 0.0)
        assert pytest.approx(energy_of(s), abs=1e-6) == 1.0


class TestPurity:
    def test_same_input_same_output(self):
        """仪表盘的底线：同一份数据反复计算必须完全一致。"""
        s = signals(conflicts=(hard_conflict(),))
        a = compute([], [], s, NOW)
        b = compute([], [], s, NOW)
        assert a.mood == b.mood and a.energy == b.energy

    def test_deterministic_across_call_order(self):
        """Python dict/set 的迭代顺序不能影响结果。"""
        s1 = signals(conflicts=(hard_conflict(),), overdue_count=3,
                     avg_overdue_days=5.0)
        s2 = signals(overdue_count=3, avg_overdue_days=5.0,
                     conflicts=(hard_conflict(),))
        assert compute([], [], s1, NOW) == compute([], [], s2, NOW)
