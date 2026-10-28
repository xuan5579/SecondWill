"""从旧版 Tkinter 桌面应用（plans.json）导入数据。

✅ 已校准：字段映射基于真实样例 `plans.json` 校准完成（原 §9.2 A3 已闭环）。

旧版结构（已实测）：
    {
      "version": 1,
      "updated_at": "2026-10-28T15:24:16",
      "plans": [
        {
          "title": "复盘", "date": "2026-10-11",
          "start": "19:00", "end": "20:00",
          "priority": "高" | "中" | "低" | "", "note": "",
          "done": true, "exp_awarded": 10, "fish_awarded": 2,
          "postpone_count": 0, "original_date": "2026-10-11",
          "id": "ff1da0e0", "created_at": "2026-10-11T09:00:00"
        }, ...
      ]
    }

三个必须知道的转换点（其余字段见 LEGACY_FIELD_MAP）：
    1. 时间是**拆开的**：`date` + `start`/`end` 三条字段合成一条 Pythondatetime 区间；
       旧版只存 HH:MM 的"当日分钟数"，新版存绝对 UTC naive datetime。
    2. 优先级是**中文字符串**，新版 Record.priority 是 int（1 最高、3 中性、5 最低）。
    3. 完成状态是布尔 `done`，新版是枚举 status。

用法：
    python scripts/import_legacy.py --input plans.json --report    # 结构诊断+字段映射，不写库
    python scripts/import_legacy.py --input plans.json             # 预演，不写库
    python scripts/import_legacy.py --input plans.json --apply     # 写入数据库
    python scripts/import_legacy.py --input plans.json --apply --emit-events
                                                                   # 同时补发 task_completed 事件

已知限制：旧版没有 event 表，`--emit-events` 只能由 done/created_at 推导出
`task_completed` 一条；`postpone_count` 只记录了次数、没有时间戳，合成事件等于编造数据，
因此**默认不生成** `task_delayed` 事件——逾期信号由真实日期天然导出，无需伪造。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.ids import new_id  # noqa: E402
from app.db.base import utcnow  # noqa: E402
from app.models.event import BehaviorEvent  # noqa: E402
from app.db.session import SessionLocal, init_db  # noqa: E402
from app.models.record import Record  # noqa: E402

# ==================================================================
# 可调区 ①：旧版字段映射（已按真实样例校准）
# ==================================================================

# 顶层容器候选键。旧版实际用 "plans"；其余是通用兜底，用于导入别的文件。
CONTAINER_KEYS = ("plans", "schedules", "records", "tasks", "items", "data", "events", "list")

# 旧字段名 -> 新字段语义。左侧是旧版 JSON 里真实出现的键。
LEGACY_FIELD_MAP: dict[str, str] = {
    "title": "title",
    "date": "date",
    "start": "start_at",
    "end": "end_at",
    "priority": "priority",
    "note": "description",
    "done": "status",
    "created_at": "created_at",
}

# 旧版有、新版没有对应字段的键 -> 处理方式说明（仅用于 --report，不影响导入）
LEGACY_UNMAPPED: dict[str, str] = {
    "id": "新版用 rec_ 前缀的自增 ID；旧 8 位短 id 不保留（重复导入会产生重复数据，迁移是一次性操作）",
    "exp_awarded": "宠物经济系统的发奖快照，新版宠物不做经验/等级（见 docs/10-旧功能对照表.md）",
    "fish_awarded": "同上，小鱼干经济闭环已移出 v1.0",
    "postpone_count": "推迟次数——旧版只有计数没有时间戳，无法还原成真实事件；逾期信号改由真实日期导出",
    "original_date": "改期历史，新版暂无「原定日期」字段；若需保留计划 v1.1 的 reschedule 审计表",
}

# 宽松兜底：遇到结构不同（但仍是「一条计划一个对象」）的旧文件时按别名猜。
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    # —— 校准过的真实字段优先 ——
    "title": ("title", "name", "subject", "event", "标题", "任务", "计划"),
    "date": ("date", "day", "plan_date", "日期"),
    "start_at": ("start", "start_at", "start_time", "begin", "from", "开始", "开始时间"),
    "end_at": ("end", "end_at", "end_time", "finish", "to", "结束", "结束时间"),
    "priority": ("priority", "level", "importance", "优先级", "重要程度"),
    "description": ("note", "description", "desc", "remark", "备注", "描述", "说明"),
    "status": ("done", "status", "state", "completed", "is_done", "finished", "状态", "已完成"),
    "created_at": ("created_at", "create_time", "created", "add_time", "创建时间"),
}

# 中文优先级 -> int。新版 priority 语义：1 最高、3 中性(=未设置/默认)、5 最低。
# 取 1/3 制的中间值是为了让「未设置」与「中」落在同一个默认点上，避免凭空抬高旧的未标级任务。
PRIORITY_LEGACY_MAP = {"高": 2, "中": 3, "低": 4}
PRIORITY_NEUTRAL = 3

# ==================================================================
# 可调区 ②：时间格式（已按真实样例校准）
# ==================================================================

DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日")
TIME_FORMATS = (
    "%H:%M",              # ← 关键：旧版 start/end 就长这样，必须靠它配合 date 合成 datetime
    "%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",  # ← created_at 长这样
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)
# 纯 HH:MM 解析出来的哨兵日期，用于判断「这条只给了时间、没给日期」
TIME_ONLY_SENTINEL = datetime(1900, 1, 1)

# 开始缺失时的兜底起点 / 结束缺失时的默认时长
DEFAULT_START_HHMM = "09:00"
DEFAULT_DURATION_MINUTES = 60


# ==================================================================
# 解析工具
# ==================================================================

def locate_items(raw) -> list[dict]:
    """定位条目列表。顶层是 list 直接用；是 dict 则按候选键找。"""
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for key in CONTAINER_KEYS:
            value = raw.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        for key, value in raw.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [x for x in value if isinstance(x, dict)]
    return []


def pick(item: dict, canonical: str):
    """按别名表取字段值，跳过空值。"""
    for alias in FIELD_ALIASES[canonical]:
        if alias in item and item[alias] not in (None, ""):
            return item[alias]
    return None


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _to_utc_naive(dt: datetime) -> datetime:
    """naive datetime 按本地时区解释后转 UTC naive。旧版所有时间都是本地墙钟时间。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(tzinfo=_local_tz()).astimezone(timezone.utc).replace(tzinfo=None)


def parse_clock(value) -> datetime | None:
    """解析 HH:MM / HH:MM:SS。成功返回 1900-01-01 为日期的时间（哨兵日期，供上层识别）。"""
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_day(value) -> datetime | None:
    """解析 YYYY-MM-DD 等日期格式，返回当日 00:00 的 datetime。"""
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_timestamp(value) -> datetime | None:
    """解析完整时间戳（created_at 等非拆分的 datetime 字段）。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:                      # 毫秒级
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    text = str(value).strip()
    try:
        return _to_utc_naive(datetime.fromisoformat(text))
    except ValueError:
        pass
    for fmt in TIME_FORMATS:
        try:
            return _to_utc_naive(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return None


def normalize_priority(value) -> int:
    """中文优先级 -> int。数字串原样取整（并夹到 1..5），无法识别给中性值。"""
    if value in (None, ""):
        return PRIORITY_NEUTRAL
    text = str(value).strip()
    if text in PRIORITY_LEGACY_MAP:
        return PRIORITY_LEGACY_MAP[text]
    try:
        return max(1, min(5, int(float(text))))
    except ValueError:
        return PRIORITY_NEUTRAL


def normalize_status(value) -> str:
    """布尔 done / 各种状态词 -> 新版枚举。"""
    if value in (None, ""):
        return "pending"
    if isinstance(value, bool):
        return "done" if value else "pending"
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "done", "completed", "finished", "已完成", "完成"}:
        return "done"
    if text in {"dropped", "cancelled", "canceled", "abandoned", "放弃", "取消"}:
        return "dropped"
    if text in {"in_progress", "doing", "进行中"}:
        return "in_progress"
    return "pending"


def build_span(date_value, start_value, end_value) -> tuple[datetime | None, datetime | None, list[str]]:
    """
    由 date + start/end 合成 [start_at, end_at)，返回 (start, end, 警告列表)。

    与旧版最大的不同：**end 允许跨零点**。
    旧版把时间存成"当日分钟数"，23:30→01:00 会被算成长度 0 且检测不到任何冲突；
    新版用绝对 datetime，end 早于 start 时自动 +1 天，这是有意为之的修复。
    """
    warnings: list[str] = []
    day = parse_day(date_value)

    start = None
    start_clock = parse_clock(start_value)
    if start_clock is not None:
        if day is not None:
            start = datetime.combine(day.date(), start_clock.time())
        else:
            start = None                       # 只有时间没有日期，无从定位
            warnings.append("有起止时间但缺少 date，已跳过该条")
    elif start_value not in (None, ""):
        # 退而求其次：把整个字段当时间戳解析（适用于别的数据结构）
        parsed = parse_timestamp(start_value)
        if parsed is not None:
            start = parsed.replace(tzinfo=timezone.utc).astimezone(_local_tz()).replace(tzinfo=None)

    if start is None:
        if day is None:
            return None, None, warnings or ["缺少可定位的时间信息"]
        start = datetime.combine(day.date(), datetime.strptime(DEFAULT_START_HHMM, "%H:%M").time())
        warnings.append(f"缺少 start，回退到 {DEFAULT_START_HHMM}")

    end = None
    end_clock = parse_clock(end_value)
    if end_clock is not None:
        end = datetime.combine(start.date(), end_clock.time())
        if end <= start:
            # 跨零点：23:30 → 次日 01:00
            end += timedelta(days=1)
            warnings.append(f"结束时间 {end_clock:%H:%M} 早于开始，已按跨零点处理为次日")
    elif end_value not in (None, ""):
        parsed = parse_timestamp(end_value)
        if parsed is not None:
            end = parsed.replace(tzinfo=timezone.utc).astimezone(_local_tz()).replace(tzinfo=None)
    if end is None or end <= start:
        end = start + timedelta(minutes=DEFAULT_DURATION_MINUTES)
        if end_value in (None, ""):
            warnings.append(f"缺少 end，回退为 {DEFAULT_DURATION_MINUTES} 分钟时长")

    return _to_utc_naive(start), _to_utc_naive(end), warnings


# ==================================================================
# 转换
# ==================================================================

def convert(items: list[dict]) -> tuple[list[Record], list[BehaviorEvent], dict]:
    records: list[Record] = []
    events: list[BehaviorEvent] = []
    known_aliases = {a for aliases in FIELD_ALIASES.values() for a in aliases}
    unmapped_hits: set[str] = set()
    failures: list[str] = []
    notes: list[str] = []

    for index, item in enumerate(items):
        for key in item:
            if key in LEGACY_FIELD_MAP or key in LEGACY_UNMAPPED:
                continue                       # 已明确「映射」或「有意不导入」的旧字段，不算未识别
            if key not in known_aliases:
                unmapped_hits.add(key)

        title = pick(item, "title")
        if not title:
            failures.append(f"#{index} 缺少标题，已跳过")
            continue

        start_at, end_at, warns = build_span(
            pick(item, "date"), pick(item, "start_at"), pick(item, "end_at")
        )
        if start_at is None:
            failures.append(f"#{index}「{title}」无法定位时间，已跳过")
            continue
        for w in warns:
            notes.append(f"#{index}「{title}」: {w}")

        created = parse_timestamp(pick(item, "created_at")) or utcnow()
        status = normalize_status(pick(item, "status"))

        record = Record(
            id=new_id("rec_"),
            type="schedule",
            title=str(title)[:200],
            description=(str(pick(item, "description")).strip() or None),
            start_at=start_at,
            end_at=end_at,
            status=status,
            priority=normalize_priority(pick(item, "priority")),
            tags=[],
            created_at=created,
            updated_at=created,
        )
        records.append(record)

        # 已完成的任务补一条执行痕迹，让新版的拖延信号一开始就有历史可算。
        # 时间戳取 end_at（推测完成时刻），比 created_at 更接近真实。
        if status == "done":
            events.append(
                BehaviorEvent(
                    id=new_id("evt_"),
                    kind="task_completed",
                    record_id=record.id,
                    at=min(end_at, utcnow()),
                    payload={"source": "legacy_import"},
                )
            )

    diagnostics = {
        "total": len(items),
        "converted": len(records),
        "events": len(events),
        "unmapped_fields": sorted(unmapped_hits),
        "failures": failures[:10],
        "notes": notes[:10],
    }
    return records, events, diagnostics


def print_report(items: list[dict], raw_keys: set[str]) -> None:
    """结构诊断与字段映射说明。"""
    print("=" * 60)
    print("结构诊断")
    print("=" * 60)
    print(f"条目数: {len(items)}")
    if items:
        keys: list[str] = []
        for item in items:
            for k in item:
                if k not in keys:
                    keys.append(k)
        print(f"条目字段 ({len(keys)}): {', '.join(keys)}")
        print("\n字段映射:")
        for k in keys:
            target = LEGACY_FIELD_MAP.get(k)
            if target:
                note = {
                    "title": "标题",
                    "date": "与 start/end 合成 start_at/end_at",
                    "start": "合成 start_at（HH:MM）",
                    "end": "合成 end_at（HH:MM，早于 start 时按跨零点 +1 天）",
                    "priority": f"中文映射 {PRIORITY_LEGACY_MAP}，空值→{PRIORITY_NEUTRAL}",
                    "note": "description",
                    "done": "布尔 → status(done/pending)",
                    "created_at": "保留（不再统一记为导入时刻）",
                }.get(k, target)
                print(f"  ✓ {k:16s} -> {target:12s} | {note}")
            elif k in LEGACY_UNMAPPED:
                print(f"  ✗ {k:16s} -> {'（不导入）':12s} | {LEGACY_UNMAPPED[k]}")
            else:
                print(f"  ? {k:16s} -> 未识别（忽略）")
    unmapped = [k for k in LEGACY_UNMAPPED if k in raw_keys]
    if unmapped:
        print(f"\n旧版专有、新版无对应字段的键: {', '.join(unmapped)}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="导入旧版 Tkinter 应用的 plans.json")
    parser.add_argument("--input", required=True, help="旧版 JSON 文件路径")
    parser.add_argument("--apply", action="store_true", help="写入数据库（默认只预演）")
    parser.add_argument("--emit-events", action="store_true", help="为已完成计划补发 task_completed 事件")
    parser.add_argument("--report", action="store_true", help="输出结构诊断与字段映射")
    parser.add_argument("--clear", action="store_true", help="写入前清空现有 records 与 events")
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"文件不存在: {path}")
        return 1

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"JSON 解析失败: {exc}")
        return 1

    items = locate_items(raw)
    if not items:
        print("未能在文件中定位到条目列表。顶层结构：")
        if isinstance(raw, dict):
            for k, v in raw.items():
                print(f"  - {k}: {type(v).__name__}")
        else:
            print(f"  顶层类型: {type(raw).__name__}")
        return 1

    raw_keys = set(raw.keys()) if isinstance(raw, dict) else set()
    if args.report:
        print_report(items, raw_keys)

    records, events, diag = convert(items)

    print(f"读取条目 {diag['total']} 条，成功转换 {diag['converted']} 条，"
          f"派生事件 {diag['events']} 条")
    if diag["unmapped_fields"]:
        print(f"未识别字段（不影响导入）: {', '.join(diag['unmapped_fields'])}")
    for msg in diag["notes"]:
        print(f"  · {msg}")
    for msg in diag["failures"]:
        print(f"  ! {msg}")

    print("\n前 5 条预览（本地时区）:")
    local_tz = _local_tz()
    for r in records[:5]:
        s = r.start_at.replace(tzinfo=timezone.utc).astimezone(local_tz)
        e = r.end_at.replace(tzinfo=timezone.utc).astimezone(local_tz)
        flag = "↔" if s.date() != e.date() else " "
        print(f"  [{r.status:8s}] p{r.priority} {s:%Y-%m-%d %H:%M} → {e:%m-%d %H:%M}{flag}  {r.title}")
    if len(records) > 5:
        print(f"  ... 其余 {len(records) - 5} 条")

    if args.report:
        return 0

    if not args.apply:
        print("\n未加 --apply，未写入数据库")
        return 0

    init_db()
    db = SessionLocal()
    try:
        if args.clear:
            db.query(BehaviorEvent).delete()
            db.query(Record).delete()
        db.add_all(records)
        if args.emit_events:
            db.add_all(events)
        db.commit()
    finally:
        db.close()
    print(f"\n已写入 {len(records)} 条记录"
          + (f"、{len(events)} 条事件" if args.emit_events else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
