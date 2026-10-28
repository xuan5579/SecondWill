"""CSV 导出。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.session import get_db
from app.models.record import Record
from app.services.export import records_to_csv_bytes

router = APIRouter(tags=["export"])


@router.get("/export.csv")
def export_csv(db: Session = Depends(get_db)) -> Response:
    rows = db.query(Record).order_by(Record.start_at.is_(None), Record.start_at).all()
    now = utcnow()
    content = records_to_csv_bytes(rows, now=now)
    filename = f"records_{now:%Y%m%d_%H%M%S}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
