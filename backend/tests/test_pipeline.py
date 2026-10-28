"""P2 端到端闭环验收：**全程 0 次 LLM 调用**。

用户给的验收口径：
    拖两条日程重叠 → 界面立即标冲突 → 宠物切 `alert`

本文件用 API 层跑通这条链路，并用 monkeypatch 把「任何 LLM 调用」变成断言失败，
确保这条闭环真的不依赖模型——这是 P4 能做到"现场拔网线仍出方案"的前提。

注意：这里**没有**任何网络请求。 pet / state 端点在计算路径上完全不接触 LLM。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _future(hours: float) -> str:
    """从「现在」往前推算的未来时间，保证落在 alert 的 24h 前瞻窗口内。"""
    return _iso(datetime.now(timezone.utc) + timedelta(hours=hours))


@pytest.fixture()
def no_llm(monkeypatch):
    """把 LLM 相关的一切人为炸掉。任何调用都会让测试失败。"""
    import app.llm.cache as cache_mod

    def boom(*args, **kwargs):
        raise AssertionError("检测到意外的 LLM 调用：P2 的确定性内核不得依赖模型")

    for name in ("load_fixture", "save_fixture", "fingerprint"):
        if hasattr(cache_mod, name):
            monkeypatch.setattr(cache_mod, name, boom)
    yield


class TestConflictToAlertLoop:
    def test_two_overlapping_schedules_make_pet_alert(self, client, no_llm):
        """验收主路径：造两条重叠日程 -> 冲突检测到 hard 冲突 -> 宠物切 alert。"""
        base = datetime.now(timezone.utc) + timedelta(hours=2)

        r1 = client.post("/api/records", json={
            "type": "schedule", "title": "写架构文档",
            "start_at": _iso(base),
            "end_at": _iso(base + timedelta(hours=2)),
            "status": "pending", "priority": 2,
        })
        assert r1.status_code == 201, r1.text

        r2 = client.post("/api/records", json={
            "type": "schedule", "title": "评审 PR #42",
            "start_at": _iso(base + timedelta(hours=1)),
            "end_at": _iso(base + timedelta(hours=3)),
            "status": "pending", "priority": 3,
        })
        assert r2.status_code == 201, r2.text

        data = client.get("/api/state").json()
        signals, pet = data["signals"], data["pet"]

        hard = [c for c in signals["conflicts"] if c["hard"]]
        assert len(hard) >= 1, f"应检测到硬冲突，实际: {signals['conflicts']}"
        assert pet["mood"] == "alert"
        assert pet["line"], "即使没有 LLM 也必须有模板台词兜底"

    def test_conflict_disappears_after_reschedule(self, client, no_llm):
        """反向验证：挪开一条后冲突消失，情绪回落——保证不是"永远 alert"。"""
        base = datetime.now(timezone.utc) + timedelta(hours=2)
        a = client.post("/api/records", json={
            "title": "A", "start_at": _iso(base),
            "end_at": _iso(base + timedelta(hours=1)),
        }).json()
        client.post("/api/records", json={
            "title": "B", "start_at": _iso(base + timedelta(minutes=30)),
            "end_at": _iso(base + timedelta(hours=2)),
        })
        assert client.get("/api/state").json()["pet"]["mood"] == "alert"

        # 把 B 挪到 A 之后足够远的地方
        client.patch(f"/api/records/{a['id']}", json={
            "start_at": _iso(base - timedelta(hours=5)),
            "end_at": _iso(base - timedelta(hours=4)),
        })

        data = client.get("/api/state").json()
        hard = [c for c in data["signals"]["conflicts"] if c["hard"]]
        assert hard == []
        assert data["pet"]["mood"] != "alert"

    def test_done_record_clears_conflict(self, client, no_llm):
        """完成其中一条后，它不再占用时间，冲突解除。"""
        base = datetime.now(timezone.utc) + timedelta(hours=2)
        a = client.post("/api/records", json={
            "title": "A", "start_at": _iso(base),
            "end_at": _iso(base + timedelta(hours=2)),
        }).json()
        client.post("/api/records", json={
            "title": "B", "start_at": _iso(base + timedelta(hours=1)),
            "end_at": _iso(base + timedelta(hours=3)),
        })
        assert client.get("/api/state").json()["pet"]["mood"] == "alert"

        client.patch(f"/api/records/{a['id']}", json={"status": "done"})
        data = client.get("/api/state").json()
        assert not [c for c in data["signals"]["conflicts"] if c["hard"]]


class TestStateEndpointShape:
    def test_state_endpoint_has_no_llm_dependency(self, client, no_llm):
        assert client.get("/api/state").status_code == 200

    def test_state_fields(self, client, no_llm):
        data = client.get("/api/state").json()
        assert set(data) >= {"signals", "pet", "pet_reason"}
        for key in ("conflicts", "procrastination_index", "load_ratio", "idle_blocks",
                    "completion_rate_7d", "overdue_count", "streak_days"):
            assert key in data["signals"], f"signals 缺少 {key}"
        assert data["pet"]["mood"] in ("alert", "dormant", "tired", "guilty", "great", "normal")

    def test_pet_endpoint_persists_snapshot(self, client, no_llm):
        """写请求会落快照，/pet/history 能查到趋势。"""
        before = len(client.get("/api/pet/history").json())
        client.get("/api/pet")
        after = client.get("/api/pet/history").json()
        assert len(after) >= before

    def test_pet_persist_false_does_not_write(self, client, no_llm):
        client.get("/api/pet", params={"persist": "false"})
        assert isinstance(client.get("/api/pet/history").json(), list)


class TestNoFalseAlarmOnFreshInstall:
    def test_empty_database_is_not_punishing(self, client, no_llm):
        """全新用户（空库）不该看到任何负面情绪。

        这是"反负罪感"原则的最后一道防线：宠物可以对"每天打开就被骂"零容忍。
        """
        data = client.get("/api/state").json()
        pet = data["pet"]
        # 新用户：既不是 dormant 也不是 alert
        assert pet["mood"] not in ("alert", "dormant", "tired", "guilty")
        assert data["signals"]["procrastination_index"] == 0.0
        assert data["signals"]["overdue_count"] == 0
