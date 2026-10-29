"""记录写入的边界校验。

这些用例锁的是 P3-a 的修复：
* 空标题、纯空格标题必须被拒（`min_length=1` 拦不住纯空格）
* 倒序时间必须被拒（否则会存进一条负长度日程，冲突检测会算出负数）
* **PATCH 只改一个时间字段时也必须走完整跨字段校验** —— 这是最容易漏的一条

最后一条是这组分叉的价值所在：单看 `RecordUpdate` 模型是不够的，
它只在同一次请求同时给出 start_at 与 end_at 时才能判断顺序。
真正的防线在路由层合并后重新整体验证。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

BASE = "/api/records"
NOW = datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)  # 固定的"现在"，避免跨时区抖动


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make(client: TestClient, title: str = "基准日程", **over) -> dict:
    body = {"title": title, "start_at": _iso(NOW), "end_at": _iso(NOW + timedelta(hours=1))}
    body.update(over)
    resp = client.post(BASE, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreateRejectsBadInput:
    def test_empty_title_rejected(self, client: TestClient):
        assert client.post(BASE, json={"title": ""}).status_code == 422

    def test_whitespace_only_title_rejected(self, client: TestClient):
        """纯空格不是有效标题。`min_length=1` 拦不住它，必须 strip 后判定。"""
        resp = client.post(BASE, json={"title": "   "})
        assert resp.status_code == 422, resp.text

    def test_title_is_trimmed(self, client: TestClient):
        row = _make(client, "   记得去掉首尾空格   ")
        assert row["title"] == "记得去掉首尾空格"

    def test_end_before_start_rejected(self, client: TestClient):
        resp = client.post(
            BASE,
            json={"title": "倒序", "start_at": _iso(NOW), "end_at": _iso(NOW - timedelta(hours=1))},
        )
        assert resp.status_code == 422, resp.text

    def test_end_equal_to_start_rejected(self, client: TestClient):
        """零长度日程同样非法：它会以负长度污染冲突检测。"""
        resp = client.post(
            BASE, json={"title": "零长度", "start_at": _iso(NOW), "end_at": _iso(NOW)}
        )
        assert resp.status_code == 422, resp.text

    def test_unknown_status_rejected(self, client: TestClient):
        assert client.post(BASE, json={"title": "x", "status": "weird"}).status_code == 422

    def test_priority_out_of_range_rejected(self, client: TestClient):
        assert client.post(BASE, json={"title": "x", "priority": 9}).status_code == 422
        assert client.post(BASE, json={"title": "x", "priority": 0}).status_code == 422


class TestPatchCannotBypass:
    """PATCH 语义下的绕过点。"""

    def test_moving_end_before_existing_start_is_rejected(self, client: TestClient):
        row = _make(client)
        start = row["start_at"]
        # 只挪 end_at，start_at 由数据库里的现值补上
        resp = client.patch(f"{BASE}/{row['id']}", json={"end_at": _iso(NOW - timedelta(hours=1))})
        assert resp.status_code == 400, resp.text
        assert "结束时间必须晚于开始时间" in resp.json()["detail"]
        # 确认没有被写入
        assert client.get(f"{BASE}/{row['id']}").json()["start_at"] == start

    def test_moving_start_after_existing_end_is_rejected(self, client: TestClient):
        row = _make(client)
        resp = client.patch(f"{BASE}/{row['id']}", json={"start_at": _iso(NOW + timedelta(hours=2))})
        assert resp.status_code == 400, resp.text

    def test_explicit_null_means_no_change(self, client: TestClient):
        """显式传 null 视作不修改该字段，而不是把字段清空。"""
        row = _make(client, title="原标题")
        resp = client.patch(f"{BASE}/{row['id']}", json={"title": None, "description": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "原标题"

    def test_valid_patch_still_works(self, client: TestClient):
        row = _make(client)
        resp = client.patch(
            f"{BASE}/{row['id']}",
            json={
                "title": "改后",
                "priority": 1,
                "status": "in_progress",
                "end_at": _iso(NOW + timedelta(hours=2)),
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["title"] == "改后"
        assert body["priority"] == 1
        assert body["status"] == "in_progress"

    def test_cross_midnight_allowed(self, client: TestClient):
        """跨零点日程必须被接受 —— 这是 P2 特意修复的旧版缺陷。"""
        start = NOW.replace(hour=15, minute=30)
        resp = client.post(
            BASE,
            json={
                "title": "夜班",
                "start_at": _iso(start),
                "end_at": _iso(start + timedelta(hours=1, minutes=30)),
            },
        )
        assert resp.status_code == 201, resp.text
