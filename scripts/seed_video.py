#!/usr/bin/env python3
"""拍摄专用种子数据：清空 → 灌入 5 条互相冲突的日程 → 回到干净初始态。

**这是拍摄支撑脚本，不是产品功能。** 它不改任何业务规则，也不参与
运行时链路，只在开拍前手动跑一次，作用等同于"把道具摆好"。

与 `scripts/seed.py` 的分工：

- `seed.py` 是**功能演示**数据：10 条，覆盖完成率、连续达标天数、
  逾期、行为事件等多种指标，用于证明系统"看得懂复杂状态"；
- 本脚本只服务**镜头**：5 条全 `pending`，1 处硬冲突（重叠 60 分钟）
  + 1 处软冲突（间隔 5 分钟），让宠物稳定落在 `alert` 档。
  条数刻意压到 5 条 —— 画面里的日程列表要一屏装得下，不能滚动。

时刻一律**相对「现在」锚定**（原因同 `seed.py`）：`alert` 档的前瞻窗口是
`[now, now+24h]`，写死钟点的话换个时辰再拍，冲突就落进过去了，猫不会变 alert。

用法::

    python scripts/seed_video.py            # 清库 + 写入 + 打印预期画面
    python scripts/seed_video.py --dry-run  # 只打印，不写库

拍摄中途要重来一遍（十几遍也不怕）：再跑一次本脚本即可，
它会**连同干预历史一起清掉** —— 否则节奏闸门的"今日还剩 N 次"
会带着上一遍的残留，观众不知道那几次是哪儿来的。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.ids import new_id  # noqa: E402
from app.db.base import utcnow  # noqa: E402
from app.db.session import SessionLocal, init_db  # noqa: E402
from app.models.event import BehaviorEvent  # noqa: E402
from app.models.intervention import Intervention  # noqa: E402
from app.models.pet_snapshot import PetSnapshot  # noqa: E402
from app.models.record import Record  # noqa: E402


def _anchor() -> datetime:
    """整批未来日程的起点。

    两个约束必须同时满足，否则镜头里会出戏或演示失效：

    1. **要落在工作时段** —— 相对「现在」直接 +2h 的话，晚上开拍就会生成
       「团队周会 00:23」这种荒唐钟点。判断为"放不下"时，整批滚到**次日 09:00**；
    2. **要落进 alert 的前瞻窗口 [now, now+24h]** —— 次日 09:00 起、跨度 6.5 小时，
       最坏情形（现在 23:59 开拍）结束于次日 15:30，距现在约 15.5 小时，仍在窗口内。

    这就是「相对锚定」与「画面可信」之间的折中：宁可把冲突放在明天上午，
    也不能让宠物因为窗口过期而不变 alert。
    """
    local_tz = datetime.now().astimezone().tzinfo
    now = datetime.now(local_tz)
    cand = now + timedelta(hours=1)
    span_end = cand + timedelta(hours=6, minutes=30)
    day_end = datetime.combine(now.date(), datetime.min.time(), tzinfo=local_tz).replace(hour=23)
    if not (8 <= cand.hour < 21) or span_end > day_end:
        # 今天放不下了 —— 整批挪到明天上午 09:00
        cand = datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time(), tzinfo=local_tz
        ).replace(hour=9)
    return cand.astimezone(timezone.utc).replace(tzinfo=None)


def _in(anchor: datetime, hours: float, minutes: float = 0) -> datetime:
    """以 `_anchor()` 为起点构造未来时刻。"""
    return anchor + timedelta(hours=hours, minutes=minutes)


def _hours_ago(days: int, hour: int, minute: int = 0) -> datetime:
    """构造过去时刻（用于逾期记录，让拖延指数不为零）。"""
    local_tz = datetime.now().astimezone().tzinfo
    day = datetime.now(local_tz).date() - timedelta(days=days)
    aware = datetime.combine(day, datetime.min.time(), tzinfo=local_tz).replace(
        hour=hour, minute=minute
    )
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _local(dt: datetime | None) -> str:
    """库里存 UTC naive，打印前转回本地，否则镜头里的钟点是错的。"""
    if dt is None:
        return "—"
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime("%m-%d %H:%M")


def build_records() -> list[Record]:
    now = utcnow()
    a = _anchor()
    specs = [
        # ① 硬冲突：两条重叠 60 分钟 → overlap(hard) → 宠物 alert
        ("写项目架构文档", _in(a, 0), _in(a, 2), 2, ["深度工作"], "与 ② 重叠 60 分钟"),
        ("评审 PR #42", _in(a, 1), _in(a, 3), 2, ["协作"], "与 ① 重叠 60 分钟"),
        # ② 软冲突：与上一条间隔 5 分钟 → no_buffer(soft)
        ("团队周会", _in(a, 4), _in(a, 5), 1, ["会议"], "与 ④ 只隔 5 分钟"),
        ("整理实验数据", _in(a, 5, 5), _in(a, 6), 3, ["深度工作"], "与 ③ 只隔 5 分钟"),
        # ③ 逾期：给拖延指数一个非零值，方便第 4 章讲 tired
        ("整理季度复盘材料", _hours_ago(2, 16, 0), _hours_ago(2, 18, 0), 2, ["复盘"], "逾期 2 天"),
    ]
    return [
        Record(
            id=new_id("rec_"),
            type="schedule",
            title=title,
            description=None,
            start_at=start,
            end_at=end,
            status="pending",
            priority=priority,
            tags=tags,
            created_at=now,
            updated_at=now,
        )
        for title, start, end, priority, tags, _note in specs
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="拍摄专用演示数据（清空 + 灌 5 条冲突日程）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    args = parser.parse_args()

    records = build_records()
    notes = [
        "与 ② 重叠 60 分钟",
        "与 ① 重叠 60 分钟",
        "与 ④ 只隔 5 分钟",
        "与 ③ 只隔 5 分钟",
        "逾期 2 天",
    ]

    print(f"将写入 {len(records)} 条日程（先清空 records / events / interventions / pet_snapshot）\n")
    for r, note in zip(records, notes):
        print(f"  {_local(r.start_at)} → {_local(r.end_at)[-5:]}  {r.title}   [{note}]")

    print("\n预期画面：宠物 alert（前瞻 24h 内有硬冲突）· 硬冲突 1 · 软冲突 1 · 拖延指数 > 0")

    if args.dry_run:
        print("\n(--dry-run) 未写入数据库")
        return 0

    init_db()
    db = SessionLocal()
    try:
        # 顺序有讲究：先清事件与干预（引用侧），再清记录
        db.query(BehaviorEvent).delete()
        db.query(Intervention).delete()
        db.query(PetSnapshot).delete()
        db.query(Record).delete()
        db.add_all(records)
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

    print("\n已写入，回到干净初始态。可以开拍。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
