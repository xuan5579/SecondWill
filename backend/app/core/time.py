"""时间工具。

单向约定，避免时区 bug：
    API(ISO8601 带时区) --parse_iso--> DB(UTC naive) --to_iso--> API(ISO8601 带时区)

任何一处直接写 `datetime.now()` 或裸存 aware datetime 都会破坏该约定。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


def to_utc_naive(dt: datetime | None) -> datetime | None:
    """任意 aware/naive datetime -> UTC naive。naive 输入按 UTC 处理。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def to_iso(dt: datetime | None) -> str | None:
    """UTC naive datetime -> 带时区的 ISO8601 字符串。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_iso(value: str | datetime | None) -> datetime | None:
    """ISO8601 字符串 -> UTC naive。已是 datetime 时直接归一化。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_utc_naive(value)
    return to_utc_naive(datetime.fromisoformat(value))


def utc_now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


# ==================================================================
# 本地日历换算
# ==================================================================
# 存储层是 UTC naive，但「今天」「08:00–23:00 窗口」这类概念属于**用户的本地日历**。
# 冲突检测需要按本地日历天分桶（判断 overload），所以必须有这一层换算。
# 除了这里，任何模块都不该自己拿 astimezone 处理本地日——那正是时区 bug 的温床。


def local_tzinfo():
    """系统本地时区。"""
    return datetime.now().astimezone().tzinfo


def utc_to_local(dt: datetime | None) -> datetime | None:
    """DB(UTC naive) -> 本地 naive。用于按本地日历天分桶与展示。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tzinfo()).replace(tzinfo=None)


def local_to_utc(dt: datetime | None) -> datetime | None:
    """本地 naive -> DB(UTC naive)。构造测试数据与时分都走这里。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(tzinfo=local_tzinfo()).astimezone(timezone.utc).replace(tzinfo=None)


def local_midnight_utc(day: date) -> datetime:
    """本地某日 00:00 对应的 UTC naive 时刻。"""
    return local_to_utc(datetime.combine(day, time.min))


def containing_local_day(dt: datetime) -> date:
    """UTC naive 时刻落在本地的哪一天。"""
    local = utc_to_local(dt)
    return local.date()
