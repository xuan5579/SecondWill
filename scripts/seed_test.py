"""测试数据生成器 —— 一次只造出"刚好命中某一档"的场景。

和 seed.py 的分工
-----------------
`seed.py` 是**固定演示包**：写一套数据，保证 alert 与 tired 都能被试出来。
本脚本是**可切换的场景**：一次只造一个档位，能一键清掉，便于逐项验收。

为什么必须一次一个档
--------------------
档位判定按 `alert > dormant > tired > guilty > great > normal` 严格短路。
若把"有冲突"和"有逾期"同时写进去，你永远只看得到 alert ——
另外几档的数字在算，但从没机会被读出来。所以切换场景前要清场。

安全性
------
默认先在 `data/backups/` 里备份一份数据库，再清空 records / events /
interventions / pet_snapshots 四张表。加 `--keep-real` 则保留不带「测试」
标签的记录（判定可能被真实数据干扰，只建议快速试跑时用）。

所有时间都以「现在」为锚点，不写死钟点。原因见 seed.py 的模块注释：
alert 的前瞻窗口是 [now, now+24h]，写死钟点的话换个人换个时辰跑就看不到了。

用法
----
    python scripts/seed_test.py --list
    python scripts/seed_test.py --scene alert
    python scripts/seed_test.py --scene tired
    python scripts/seed_test.py --scene great --keep-real
    python scripts/seed_test.py --clean        # 只删测试数据，不写入
"""

from __future__ import annotations

import argparse
import shutil
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

#: 测试数据的身份标记。清理靠它识别，所以必须写进 tags。
MARK = "测试"

LOCAL_TZ = datetime.now().astimezone().tzinfo
DB_PATH = ROOT / "data" / "app.db"


# ==================================================================
# 时间锚点工具
# ==================================================================

def _round30(dt: datetime) -> datetime:
    """把时刻对齐到半小时，避免 UI 上出现 02:37 这种"机器造的时间"。"""
    return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)


def _in(hours: float, minutes: float = 0) -> datetime:
    """相对「现在」的未来时刻（UTC naive，与库内存储一致）。

    先对齐到半小时**再**加 minutes：像 `_in(7, 5)` 这种"整点后再偏 5 分钟"的写法，
    如果先加分钟再对齐，5 分钟会被抹平成 0 —— no_buffer 场景就变成首尾相接了。
    """
    aware = _round30(datetime.now(LOCAL_TZ) + timedelta(hours=hours)) + timedelta(minutes=minutes)
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _ago(days: float, hours: float = 0) -> datetime:
    """相对「现在」的过去时刻。用相对量而不是「昨天 18:00」——
    凌晨跑脚本时「昨天 18:00」只逾期了几小时，平均逾期天数会掉到门槛以下。"""
    aware = datetime.now(LOCAL_TZ) - timedelta(days=days, hours=hours)
    return _round30(aware).astimezone(timezone.utc).replace(tzinfo=None)


def _days_ago(days: int, hour: int, minute: int = 0) -> datetime:
    """N 天前的某个钟点。用于制造「连续几天都有完成」的历史。"""
    aware = datetime.now(LOCAL_TZ) - timedelta(days=days)
    aware = aware.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _today_at(hour: int, minute: int = 0) -> datetime:
    """今天某个绝对钟点（UTC 存储）。

    只给 journey 的 A/B/C 用：硬冲突只活到 min(end_at)，`_today_ahead` 把时间
    压在「现在 → 午夜」之间，22 点之后跑 seed 时 A 结束在 30 分钟内，
    还没点开录 A/B 就过期，起点的 alert 直接掉到 tired。
    改成傍晚 → 临近午夜的绝对钟点，任何钟点跑 seed 都留出到 ~23:50 的录制窗口。
    """
    aware = datetime.now(LOCAL_TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _today_ahead(frac: float) -> datetime:
    """今天**尚未到来**时段的 frac 位置（0..1，现在 → 午夜）。

    与 `_today_frac` 相对：那条铺在「00:00 → 现在」，这条铺在「现在 → 24:00」。
    用来造「今天稍晚、尚未到期」的任务：它们此刻不算逾期，
    但一旦被完成，就会同时计入当日完成率与「今日完成件数」。

    不能写成 `_in(3)` 这种相对小时数 —— 傍晚跑会跨到明天，
    `containing_local_day(end_at)` 变成明天，done_today 就数不到它们了。
    """
    local_now = datetime.now(LOCAL_TZ)
    midnight = (local_now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (local_now + (midnight - local_now) * frac).astimezone(timezone.utc).replace(tzinfo=None)


def _today_frac(frac: float) -> datetime:
    """今天**已过去**时段的 frac 位置（0..1）。

    当日完成率的分母只算 `end_at <= now` 的任务（否则白天永远够不到 great），
    所以演示用的「今天已完成」记录必须落在 00:00 到此刻之间 ——
    凌晨跑脚本时这个窗口很窄，只能按比例铺开。
    """
    local_now = datetime.now(LOCAL_TZ)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight + (local_now - midnight) * frac).astimezone(timezone.utc).replace(tzinfo=None)


# ==================================================================
# 场景定义
# ==================================================================
# 每个 build_* 返回 (records, events)。records 的 tags 必须含 MARK。


def _rec(title, start, end, status="pending", priority=3, tags=()) -> Record:
    now = utcnow()
    return Record(
        id=new_id("rec_"),
        type="schedule",
        title=title,
        description=None,
        start_at=start,
        end_at=end,
        status=status,
        priority=priority,
        tags=[*tags, MARK],
        created_at=now,
        updated_at=now,
    )


def _evt(rec: Record, kind: str, at: datetime, payload: dict | None = None) -> BehaviorEvent:
    return BehaviorEvent(
        id=new_id("evt_"),
        kind=kind,
        record_id=rec.id,
        at=at,
        payload=payload or {},
    )


def scene_alert() -> tuple[list[Record], list[BehaviorEvent]]:
    """alert：未来 24h 内有 **hard 冲突**（物理重叠）。

    这是唯一能真实触发干预光流的场景 —— 后端的规则兜底会把重排建议的
    target_record_id 指向冲突里的第一张卡片，光流才有终点可飞。

    数据刻意分三组：
      ① 两条重叠 1 小时          → overlap（hard）
      ② 两条间隔 5 分钟          → no_buffer（soft，不改变档位，但会出现在诊断列表里）
      ③ 一条干净的               → 对照组，不该被标记
    """
    a = _rec("写项目架构文档", _in(2), _in(4), priority=2, tags=["深度工作"])
    b = _rec("评审 PR #42", _in(3), _in(5), priority=2, tags=["协作"])
    c = _rec("团队周会", _in(6), _in(7), priority=1, tags=["会议"])
    d = _rec("整理实验数据", _in(7, 5), _in(8), priority=3, tags=["深度工作"])
    e = _rec("回复邮件", _in(9), _in(9.5), priority=4, tags=["杂务"])
    return [a, b, c, d, e], []


def scene_tired() -> tuple[list[Record], list[BehaviorEvent]]:
    """tired：平均逾期 ≥2 天，且不是 dormant（昨天刚有完成）。

    两条逾期分别约 1.1 天与 3.1 天 → 平均 ≈2.1 天，刚好越过门槛。
    故意留一条昨天的完成：否则连续 3 天无完成会先命中 dormant。
    """
    a = _rec("整理季度复盘材料", _ago(1.1, 1), _ago(1.1, -1), priority=2, tags=["复盘"])
    b = _rec("季度 OKR 对齐会", _ago(3.1, 1), _ago(3.1, -0.5), priority=2, tags=["复盘"])
    done = _rec("晨间阅读", _ago(1, 6), _ago(1, 5.5), status="done", priority=5, tags=["习惯"])
    future = _rec("回复邮件", _in(20), _in(21), priority=4, tags=["杂务"])
    return [a, b, done, future], [_evt(done, "task_completed", done.end_at)]


def scene_dormant() -> tuple[list[Record], list[BehaviorEvent]]:
    """dormant：连续 ≥3 天没有任何完成。

    最后一条完成放在 5 天前，且之后既不完成也不逾期 ——
    逾期会先命中 tired（tired 优先级高于 dormant？不，dormant 在前）。
    这里让 pending 都排在将来，是为了让场景只演示"停摆"这一个信号。
    """
    old = _rec("写月度总结", _ago(5, 8), _ago(5, 6), status="done", priority=3, tags=["复盘"])
    a = _rec("整理实验数据", _in(3), _in(4), priority=3, tags=["深度工作"])
    b = _rec("回复邮件", _in(6), _in(6.5), priority=4, tags=["杂务"])
    return [old, a, b], [_evt(old, "task_completed", old.end_at)]


def scene_great() -> tuple[list[Record], list[BehaviorEvent]]:
    """great：当日完成率 ≥0.8 且连续 ≥3 天有完成。

    今天 4 条全部完成（4/4 = 1.0），再补昨天/前天/大前天各一条，凑出 4 天连续。

    这些已完成记录必须落在**今天已过去的时段**里（`_today_frac`）：
    当日完成率的分母只算已到期的任务，写成 7:30 这种未来钟点会被正确地排除 —
    凌晨跑脚本时就会变成「0/0」而永远测不出 great。
    """
    today_done = [
        _rec("晨间阅读", _today_frac(0.10), _today_frac(0.20), status="done", priority=5, tags=["习惯"]),
        _rec("修复登录跳转 bug", _today_frac(0.25), _today_frac(0.40), status="done", priority=2, tags=["开发"]),
        _rec("代码评审", _today_frac(0.45), _today_frac(0.60), status="done", priority=2, tags=["协作"]),
        _rec("写周报", _today_frac(0.65), _today_frac(0.80), status="done", priority=3, tags=["复盘"]),
    ]
    pending_today = _rec("整理实验数据", _in(4), _in(5), priority=3, tags=["深度工作"])
    past = [
        _rec("晨间阅读", _days_ago(1, 7, 30), _days_ago(1, 8), status="done", priority=5, tags=["习惯"]),
        _rec("写周报", _days_ago(2, 17), _days_ago(2, 18), status="done", priority=3, tags=["复盘"]),
        _rec("代码评审", _days_ago(3, 16), _days_ago(3, 17), status="done", priority=2, tags=["协作"]),
    ]
    records = [*today_done, pending_today, *past]
    events = [_evt(r, "task_completed", r.end_at) for r in [*today_done, *past]]
    return records, events


def scene_guilty() -> tuple[list[Record], list[BehaviorEvent]]:
    """guilty：逾期**条数**成规模（≥3），但单条都没拖到 2 天。

    这组数据在旧实现下会得到 `normal` —— 拖延指数只有 0.15 上下，
    永远够不到旧的 0.6 门槛，于是"拖了三件事、完成率 25%"却显示一切正常。
    现在由条数判定，它稳定命中 guilty。

    刻意让平均逾期 ≈1.4 天（低于 tired 的 2 天），才能越过 tired 短路。
    """
    a = _rec("整理季度复盘材料", _ago(1.0, 1), _ago(1.0, -0.5), priority=2, tags=["复盘"])
    b = _rec("季度 OKR 对齐会", _ago(1.4, 1), _ago(1.4, -0.5), priority=2, tags=["复盘"])
    c = _rec("补充竞品分析", _ago(1.8, 1), _ago(1.8, -0.5), priority=3, tags=["调研"])
    done = _rec("晨间阅读", _ago(0.2, 1), _ago(0.2, -0.5), status="done", priority=5, tags=["习惯"])
    future = _rec("回复邮件", _in(6), _in(6.5), priority=4, tags=["杂务"])
    return [a, b, c, done, future], [_evt(done, "task_completed", done.end_at)]


def scene_normal() -> tuple[list[Record], list[BehaviorEvent]]:
    """normal：兜底档。未来两条干净日程，无冲突、无逾期、无完成记录。"""
    a = _rec("整理实验数据", _in(3), _in(4), priority=3, tags=["深度工作"])
    b = _rec("回复邮件", _in(6), _in(6.5), priority=4, tags=["杂务"])
    return [a, b], []


def scene_journey() -> tuple[list[Record], list[BehaviorEvent]]:
    """journey：一条**按顺序勾完成**就能走完五档的剧本。

    为什么不是六档：`dormant` 的判据是「连续 ≥3 天没有任何完成」，
    勾掉第一条任务的那一刻它就永久消失 —— 它只能当起点，进不了"完成过程"。
    六档同时可读在逻辑上不成立，这是优先级表的必然结果，不是数据没造好。

    五档的数学依据（顺序不能换）：

    | 步骤            | 剩下什么                          | 命中档位 |
    |-----------------|-----------------------------------|----------|
    | 初始            | 今天 A/B 重叠 + 4 条逾期（均值2.1）| alert    |
    | 勾掉 A          | 重叠消失；4 条逾期均值仍 2.1      | tired    |
    | 勾掉最久那条    | 剩 3 条、均值 1.42                | guilty   |
    | 再勾一条逾期    | 剩 2 条、均值 1.41                | normal   |
    | 勾掉 B / C      | 今日完成 3 件、完成率 1.0         | great    |

    两个易错点，都是"看着像、数着不像"：

    1. A/B/C 必须都**结束于今天**。great 的第二通道数的是
       `_done_today_count`（end_at 落在今天的已完成条数），
       任务若跨到明天，勾得再多也只算 0 件 —— 这正是之前
       "全部完成还是 normal" 的成因。
    2. 逾期四条要卡在 `tired` 与 `guilty` 的夹缝里：均值必须 ≥2（tired），
       但勾掉最久那条后要掉到 <2 且条数仍 ≥3（guilty）。
       4.2/1.5/1.5/1.4 这组数刚好满足：整体 2.11，去掉最长后 1.42。
    """
    # 今天三条。A 与 B 硬重叠 → alert。
    # 三条时间的四个硬约束（实测踩过，全是档位被污染的坑）：
    # 1. A×B 必须硬重叠，且冲突活到录制开始 —— alert 是前瞻判定，
    #    horizon 从 now 起算，min(end_at) 一过冲突就蒸发，起点直接掉 tired。
    # 2. B、C 在被勾掉之前**不能过期**：四条逾期的均值 2.11 刚好卡在
    #    tired 门槛（≥2）上，多一条 ~0 天的逾期就会把它拖到 2 以下，
    #    tired 整档消失、直接跳 guilty。
    # 3. C 不能与 B 重叠：conflict 检测只算未完成条目，勾掉 A 之后
    #    B×C 若还重叠，alert 会一路锁到第 4 步（实测）。
    # 4. 三条都必须 end_at 落在今天（great 的 done_today 口径）——
    #    午夜是绕不开的死线，录制必须在 0 点前完成。
    # 满足全部约束的解：A/B 并肩到 23:30，C 收尾在午夜前。
    a = _rec("写项目架构文档", _today_at(20, 0), _today_at(23, 30), priority=2, tags=["深度工作"])
    b = _rec("评审 PR #42", _today_at(21, 30), _today_at(23, 30), priority=2, tags=["协作"])
    c = _rec("整理实验数据", _today_at(23, 31), _today_at(23, 59), priority=3, tags=["深度工作"])

    # 四条逾期：起点均值 (4.16+1.46+1.46+1.36)/4 ≈ 2.11 → tired
    o1 = _rec("整理季度复盘材料", _ago(4.2, 1), _ago(4.2, -1), priority=2, tags=["复盘"])
    o2 = _rec("季度 OKR 对齐会", _ago(1.5, 1), _ago(1.5, -1), priority=2, tags=["复盘"])
    o3 = _rec("补充竞品分析", _ago(1.5, 1), _ago(1.5, -1), priority=3, tags=["调研"])
    o4 = _rec("竞品数据核对", _ago(1.4, 1), _ago(1.4, -1), priority=3, tags=["调研"])

    return [a, b, c, o1, o2, o3, o4], []


#: 剧本的推荐勾选顺序。只对 journey 生效，其余场景没有顺序概念。
JOURNEY_STEPS = [
    ("写项目架构文档", "alert → tired", "两条日程的重叠消失，露出真正的问题：4 条活平均欠了 2 天"),
    ("整理季度复盘材料", "tired → guilty", "最久那条清掉，剩 3 条但平均只欠 1.4 天（欠得多、拖不久）"),
    ("季度 OKR 对齐会", "guilty → normal", "逾期降到 2 条，两个门槛都够不着"),
    ("评审 PR #42", "normal → normal", "今日完成 2 件，还差一件"),
    ("整理实验数据", "normal → great", "今日完成满 3 件、完成率 1.0，正反馈触发"),
]


def _print_journey_guide() -> None:
    print("\n按这个顺序勾「完成」，情绪会依次变化：\n")
    for i, (title, shift, why) in enumerate(JOURNEY_STEPS, 1):
        print(f"  {i}. 勾「{title}」")
        print(f"     {shift}   — {why}")
    print("\n  说明：dormant 不在这条路径上 —— 它的判据是「连续 3 天无完成」，")
    print("  勾掉第一条任务就永久解除了。要单独看它用：--scene dormant")
    print("\n  时间窗：A/B 并肩到 23:30，C 在 23:31–23:59。")
    print("  · 第 1–3 步必须在 23:30 前勾完 —— 之后 B/C 过期，")
    print("    会把 tired 的均值压到门槛下、把 guilty 的条数顶回去。")
    print("  · 全部五步必须在午夜前勾完（great 按 end_at 的当日口径计数）。")
    print("  · 错过窗口就重跑一次本场景，时间会自动重排到今天。")


#: 场景名 -> (构造函数, 预期档位, 一句话说明)
SCENES: dict[str, tuple] = {
    "journey": (scene_journey, "alert→great", "五档连播：按顺序勾完成，情绪依次变化"),
    "alert": (scene_alert, "alert", "未来 24h 硬冲突（光流的唯一真实触发场景）"),
    "tired": (scene_tired, "tired", "平均逾期约 2.1 天"),
    "guilty": (scene_guilty, "guilty", "逾期 3 条但都不到 2 天（欠得多）"),
    "dormant": (scene_dormant, "dormant", "距上次完成 5 天"),
    "great": (scene_great, "great", "今日到期全做完 + 连续 4 天"),
    "normal": (scene_normal, "normal", "无任何异常信号"),
}


# ==================================================================
# 写库
# ==================================================================

def backup_db() -> Path | None:
    if not DB_PATH.exists():
        return None
    dest_dir = ROOT / "data" / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"app-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(DB_PATH, dest)
    return dest


def list_backups() -> list[Path]:
    dest_dir = ROOT / "data" / "backups"
    if not dest_dir.exists():
        return []
    return sorted(dest_dir.glob("app-*.db"), key=lambda p: p.name)


def restore_backup(name: str) -> int:
    """把某个备份覆盖回 data/app.db。

    SQLite 在 Windows 上会锁住正在使用的文件，所以后端必须先停 ——
    这不是脚本能绕过的限制，只能把话说清楚。
    """
    backups = list_backups()
    if not backups:
        print("data/backups 下没有备份")
        return 1
    matches = [b for b in backups if b.name == name or b.name.startswith(name)]
    if len(matches) != 1:
        print(f"匹配到 {len(matches)} 个备份，请写得更具体。可用备份：")
        for b in backups:
            print(f"  {b.name}")
        return 1
    src = matches[0]
    try:
        shutil.copy2(src, DB_PATH)
    except PermissionError:
        print("后端正在占用 data/app.db，恢复失败。\n"
              "请先停掉后端（双击「SecondWill 停止.bat」，或 python scripts/dev.py --stop），再重试。")
        return 1
    print(f"已用 {src.name} 覆盖 data/app.db")
    print("下一步：重新启动后端，让它读到新数据。")
    return 0


def _is_test_row(rec: Record) -> bool:
    tags = rec.tags or []
    return MARK in tags


def _clear(db, keep_real: bool) -> tuple[int, int]:
    """清场。返回 (删除记录数, 删除事件数)。

    顺序很重要：先按 record_id 删事件，再删记录，否则 --keep-real 时
    真实记录的事件会被无差别留下/删掉。
    """
    records = db.query(Record).all()
    victims = [r for r in records if _is_test_row(r)] if keep_real else records
    ids = [r.id for r in victims]
    if keep_real and not ids:
        return 0, 0
    evt_q = db.query(BehaviorEvent)
    if keep_real:
        evt_q = evt_q.filter(BehaviorEvent.record_id.in_(ids))
    n_evt = evt_q.delete(synchronize_session=False)
    for r in victims:
        db.delete(r)
    return len(victims), n_evt


def _write(records: list[Record], events: list[BehaviorEvent], keep_real: bool) -> None:
    init_db()
    db = SessionLocal()
    try:
        n_rec, n_evt = _clear(db, keep_real)
        if not keep_real:
            # 干预历史必须一起清：节奏闸门按「今日已送达几条」计数，
            # 留着旧的会让演示一开始就显示「今日还剩 0 次」，而你不知道那是哪来的。
            db.query(Intervention).delete()
            db.query(PetSnapshot).delete()
        db.add_all(records)
        db.add_all(events)
        db.add(
            PetSnapshot(
                id=new_id("pet_"),
                mood="normal",
                energy=0.5,
                streak_days=0,
                line="测试数据已就位。",
                computed_at=utcnow(),
            )
        )
        db.commit()
        print(f"已清掉 {n_rec} 条记录 / {n_evt} 条事件，写入 {len(records)} 条记录 / {len(events)} 条事件")
    finally:
        db.close()


def _print_rows(records: list[Record]) -> None:
    for r in sorted(records, key=lambda x: x.start_at):
        s = r.start_at.replace(tzinfo=timezone.utc).astimezone()
        e = r.end_at.replace(tzinfo=timezone.utc).astimezone()
        print(f"  [{r.status:11s}] {s:%m-%d %H:%M} → {e:%H:%M}  {r.title}")


# ==================================================================
# CLI
# ==================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="写入/清理测试数据（一次一个宠物档位场景）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="场景列表见 --list",
    )
    parser.add_argument("--scene", choices=sorted(SCENES), help="要写入的场景")
    parser.add_argument("--list", action="store_true", help="列出所有场景")
    parser.add_argument("--clean", action="store_true", help="只删除测试数据，不写入新场景")
    parser.add_argument(
        "--keep-real",
        action="store_true",
        help="保留不带「测试」标签的记录（判定可能被真实数据干扰）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    parser.add_argument("--list-backups", action="store_true", help="列出 data/backups 里的备份")
    parser.add_argument(
        "--restore",
        metavar="NAME",
        help="用某个备份覆盖回 data/app.db（需先停后端）；NAME 可写前缀",
    )
    args = parser.parse_args()

    if args.list_backups:
        backups = list_backups()
        if not backups:
            print("data/backups 下没有备份")
            return 0
        for b in backups:
            size = b.stat().st_size / 1024
            print(f"  {b.name}  ({size:.0f} KB)")
        return 0

    if args.restore:
        return restore_backup(args.restore)

    if args.list:
        print("可用场景：\n")
        for name, (_, expect, desc) in SCENES.items():
            print(f"  {name:8s} → 预期 {expect:8s} {desc}")
        return 0

    if args.clean:
        init_db()
        db = SessionLocal()
        try:
            records = db.query(Record).all()
            victims = [r for r in records if _is_test_row(r)]
            ids = [r.id for r in victims]
            n_evt = 0
            if ids:
                n_evt = (
                    db.query(BehaviorEvent)
                    .filter(BehaviorEvent.record_id.in_(ids))
                    .delete(synchronize_session=False)
                )
            for r in victims:
                db.delete(r)
            db.commit()
            print(f"已删除 {len(victims)} 条测试记录 / {n_evt} 条事件")
        finally:
            db.close()
        return 0

    if not args.scene:
        parser.print_help()
        return 2

    build, expect, desc = SCENES[args.scene]
    records, events = build()

    print(f"场景 {args.scene} —— {desc}")
    print(f"预期档位：{expect}\n")
    _print_rows(records)
    print(f"\n共 {len(records)} 条记录、{len(events)} 条行为事件")

    if args.dry_run:
        print("\n(--dry-run) 未写入数据库")
        return 0

    if not args.keep_real:
        dest = backup_db()
        if dest:
            print(f"\n已备份原库 → {dest.relative_to(ROOT)}")
        else:
            print("\n原库不存在，直接新建")

    _write(records, events, args.keep_real)
    if args.keep_real:
        print("（保留了非测试记录，实际档位可能与本场景预期不同）")
    if args.scene == "journey":
        _print_journey_guide()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
