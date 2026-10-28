"""P1 冒烟测试：记录 CRUD、事件上报、CSV 导出。

重点覆盖"时间必须带时区"这条跨层约定——它一旦破坏，
P2 的冲突检测与 P4 的 LLM 契约都会跟着错。
"""

from __future__ import annotations

import csv
import io


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_create_and_list_record(client):
    payload = {
        "title": "写测试",
        "start_at": "2026-10-28T09:00:00+08:00",
        "end_at": "2026-10-28T10:00:00+08:00",
        "priority": 2,
        "tags": ["开发"],
    }
    created = client.post("/api/records", json=payload)
    assert created.status_code == 201, created.text
    body = created.json()

    # 存 UTC，出带时区
    assert body["start_at"] == "2026-10-28T01:00:00+00:00"
    assert body["end_at"] == "2026-10-28T02:00:00+00:00"
    assert body["status"] == "pending"
    assert body["tags"] == ["开发"]

    listed = client.get("/api/records").json()
    assert any(r["id"] == body["id"] for r in listed)


def test_update_record(client):
    created = client.post("/api/records", json={"title": "待修改"}).json()
    updated = client.patch(f"/api/records/{created['id']}", json={"status": "done"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "done"


def test_update_missing_record_returns_404(client):
    assert client.patch("/api/records/nope", json={"status": "done"}).status_code == 404


def test_delete_record(client):
    rec = client.post("/api/records", json={"title": "待删除"}).json()
    assert client.delete(f"/api/records/{rec['id']}").status_code == 204
    assert client.get(f"/api/records/{rec['id']}").status_code == 404


def test_report_events(client):
    rec = client.post("/api/records", json={"title": "有事件的记录"}).json()
    res = client.post(
        "/api/events",
        json={
            "events": [
                {"kind": "task_started", "record_id": rec["id"]},
                {"kind": "task_completed", "record_id": rec["id"], "payload": {"early": True}},
            ]
        },
    )
    assert res.status_code == 201, res.text
    assert len(res.json()) == 2

    stored = client.get("/api/events").json()
    assert {e["kind"] for e in stored} == {"task_started", "task_completed"}


def test_unknown_event_kind_rejected(client):
    res = client.post("/api/events", json={"events": [{"kind": "made_up_kind"}]})
    assert res.status_code == 400


def test_pet_default_state(client):
    body = client.get("/api/pet").json()
    assert body["mood"] == "normal"


def test_export_csv_has_expected_columns(client):
    client.post(
        "/api/records",
        json={"title": "导出测试", "start_at": "2026-10-28T09:00:00+08:00"},
    )
    res = client.get("/api/export.csv")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]

    text = res.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    assert reader.fieldnames is not None
    for col in ("id", "title", "start_at", "status", "is_overdue"):
        assert col in reader.fieldnames

    rows = list(reader)
    assert any(r["title"] == "导出测试" for r in rows)
