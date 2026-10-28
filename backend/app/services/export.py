"""CSV 导出。

沿用旧版可导出的基本字段，另加 `status` 与 `is_overdue`（文档 §9.3 B12）。
输出带 UTF-8 BOM，否则 Excel 打开中文会乱码。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from app.core.time import to_iso

CSV_FIELDS = (
    "id",
    "type",
    "title",
    "description",
    "start_at",
    "end_at",
    "status",
    "priority",
    "tags",
    "is_overdue",
    "created_at",
    "updated_at",
)


def _is_overdue(record, now: datetime) -> bool:
    """计划结束时间已过且未完成，即视为逾期（文档 §9.3 B14）。"""
    if record.end_at is None:
        return False
    if record.status in ("done", "dropped"):
        return False
    return record.end_at < now


def records_to_csv(records, now: datetime | None = None) -> str:
    """把记录序列序列化为 CSV 文本。"""
    now = now or datetime.utcnow()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in records:
        writer.writerow(
            {
                "id": r.id,
                "type": r.type,
                "title": r.title,
                "description": r.description or "",
                "start_at": to_iso(r.start_at) or "",
                "end_at": to_iso(r.end_at) or "",
                "status": r.status,
                "priority": r.priority,
                "tags": "|".join(r.tags or []),
                "is_overdue": "1" if _is_overdue(r, now) else "0",
                "created_at": to_iso(r.created_at) or "",
                "updated_at": to_iso(r.updated_at) or "",
            }
        )
    return buf.getvalue()


def records_to_csv_bytes(records, now: datetime | None = None) -> bytes:
    return ("\ufeff" + records_to_csv(records, now)).encode("utf-8")
