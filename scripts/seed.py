"""生成演示数据。

⏱️ **演示用的日程一律相对「现在」锚定，不用固定钟点。**

原因：`alert` 档的前瞻窗口是 `[now, now+24h]`。
如果像第一版那样把冲突写成「今天 09:00–11:00」，那么上午生成的种子数据
到下午再演示，冲突已经落进过去——前瞻窗口看不到它，猫就不会变 alert。
相对锚定让这份数据在任何时刻跑都能演示同一条闭环。

数据刻意包含的场景：
1. 两条日程**重叠**           → 演示 overlap 类硬冲突 → 宠物切 `alert`
2. 两条日程**间隔 5 分钟**     → 演示 no_buffer 类软冲突（不算重叠，但零缓冲）
3. 两条**逾期未完成**（1 天前、3 天前）→ 平均逾期 2 天 → 删掉上面的冲突即可演示 `tired`
4. 若干已完成记录 + 行为事件   → 让完成率、连续达标天数不为零

用法：
    python scripts/seed.py            # 写入示例数据（先清空 records/events）
    python scripts/seed.py --dry-run  # 只打印，不写库
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.ids import new_id  # noqa: E402
from app.db.base import utcnow  # noqa: E402
from app.db.session import SessionLocal, init_db  # noqa: E402
from app.models.event import BehaviorEvent  # noqa: E402
from app.models.intervention import Intervention  # noqa: E402
from app.models.record import Record  # noqa: E402
from app.models.pet_snapshot import PetSnapshot  # noqa: E402


def _in(hours: float, minutes: float = 0) -> datetime:
    """相对「现在」构造未来时刻。

    演示数据必须始终落在前瞻窗口 `[now, now+24h]` 内，否则换台机器、
    换个时辰演示，冲突就悄悄消失了。
    """
    local_tz = datetime.now().astimezone().tzinfo
    aware = datetime.now(local_tz) + timedelta(hours=hours, minutes=minutes)
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _today_at(hour: int, minute: int = 0, offset_days: int = 0) -> datetime:
    """基于今天的固定钟点构造 datetime。用于**过去**的记录（逾期、已完成）。"""
    local_tz = datetime.now().astimezone().tzinfo
    day = datetime.now(local_tz).date() + timedelta(days=offset_days)
    aware = datetime.combine(day, time(hour, minute), tzinfo=local_tz)
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _to_local(dt: datetime | None) -> datetime | None:
    """库里存的是 UTC naive，打印前转回本地，否则看到的小时数是错的。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone()


def build_records() -> list[Record]:
    now = utcnow()
    specs = [
        # ===== 未来：均在 [now, now+24h] 内，保证任意时刻都能演示 =====
        # ① 硬冲突：两条重叠 1 小时 -> overlap -> alert
        (
            "写项目架构文档",
            _in(2),
            _in(4),
            "pending",
            2,
            ["深度工作"],
        ),
        (
            "评审 PR #42",
            _in(3),
            _in(5),
            "pending",
            2,
            ["协作"],
        ),
        # ② 软冲突：与上面的周会只隔 5 分钟 -> no_buffer（不重叠，但零缓冲）
        (
            "团队周会",
            _in(6),
            _in(7),
            "pending",
            1,
            ["会议"],
        ),
        (
            "整理实验数据",
            _in(7, 5),
            _in(8),
            "pending",
            3,
            ["深度工作"],
        ),
        # ③ 正常：与上一条间隔 1 小时，不该报任何冲突
        (
            "回复邮件",
            _in(9),
            _in(9, 30),
            "pending",
            4,
            ["杂务"],
        ),
        # ===== 过去 =====
        # ④ 逾期 1 天 + 逾期 3 天 -> 平均逾期 2 天，删掉上面的冲突即可演示 tired
        (
            "整理季度复盘材料",
            _today_at(16, 0, offset_days=-1),
            _today_at(18, 0, offset_days=-1),
            "pending",
            2,
            ["复盘"],
        ),
        (
            "季度 OKR 对齐会",
            _today_at(10, 0, offset_days=-3),
            _today_at(11, 30, offset_days=-3),
            "pending",
            2,
            ["复盘"],
        ),
        # ⑤ 已完成：让完成率与连续达标天数不为零
        (
            "晨间阅读",
            _today_at(7, 30, offset_days=-1),
            _today_at(8, 0, offset_days=-1),
            "done",
            5,
            ["习惯"],
        ),
        (
            "修复登录跳转 bug",
            _today_at(20, 0, offset_days=-2),
            _today_at(21, 30, offset_days=-2),
            "done",
            2,
            ["开发"],
        ),
    ]

    records: list[Record] = []
    for title, start, end, status, priority, tags in specs:
        records.append(
            Record(
                id=new_id("rec_"),
                type="schedule",
                title=title,
                description=None,
                start_at=start,
                end_at=end,
                status=status,
                priority=priority,
                tags=tags,
                created_at=now,
                updated_at=now,
            )
        )
    return records


def build_events(records: list[Record]) -> list[BehaviorEvent]:
    """为已完成/拖延的记录补上执行痕迹。"""
    by_title = {r.title: r for r in records}
    events: list[BehaviorEvent] = []

    def add(kind: str, title: str, at: datetime, payload: dict | None = None):
        r = by_title.get(title)
        events.append(
            BehaviorEvent(
                id=new_id("evt_"),
                kind=kind,
                record_id=r.id if r else None,
                at=at,
                payload=payload or {},
            )
        )

    add("task_completed", "晨间阅读", _today_at(8, 0, offset_days=-1))
    add("task_completed", "修复登录跳转 bug", _today_at(21, 20, offset_days=-2))
    # 昨天到期未完成的材料被推迟了一次
    add(
        "task_delayed",
        "整理季度复盘材料",
        _today_at(18, 10, offset_days=-1),
        {"reason": "时间不够"},
    )
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description="生成演示数据")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    args = parser.parse_args()

    records = build_records()
    events = build_events(records)

    print(f"将写入 {len(records)} 条记录、{len(events)} 条行为事件")
    for r in records:
        s, e = _to_local(r.start_at), _to_local(r.end_at)
        print(f"  - [{r.status:11s}] {s:%m-%d %H:%M} → {e:%H:%M}  {r.title}")

    if args.dry_run:
        print("\n(--dry-run) 未写入数据库")
        return 0

    init_db()
    db = SessionLocal()
    try:
        db.query(BehaviorEvent).delete()
        db.query(Record).delete()
        db.query(PetSnapshot).delete()
        # 干预历史也要清：节奏闸门按"今日已送达几条"计数，
        # 上一次调试留下的干预会让演示一开始就显示"今日还剩 0 次"，
        # 而观众并不知道那 14 条是哪儿来的。
        db.query(Intervention).delete()
        db.add_all(records)
        db.add_all(events)
        db.add(
            PetSnapshot(
                id=new_id("pet_"),
                mood="normal",
                energy=0.5,
                streak_days=0,
                line="先记下今天要做的事吧。",
                computed_at=utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()

    print("\n已写入数据库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
