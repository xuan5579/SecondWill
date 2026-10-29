"""决策链路的端到端验收（P4）。

这一组不 mock provider —— 环境里没有 API Key，所以走的必然是降级路径。
这不是"测不到 LLM"，而是**把最坏情况当成默认情况来测**：
答辩现场断网时跑的就是这条路径，它必须是通的。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from app.core.time import local_to_utc


def _tomorrow(hour: int, minute: int = 0) -> str:
    """明天本地 HH:MM 的 ISO 字符串（带时区）。"""
    day = datetime.now().date() + timedelta(days=1)
    dt = datetime.combine(day, time(hour, minute))
    from app.core.time import to_iso

    return to_iso(local_to_utc(dt))


def _clash_iso(offset_minutes: int = 0) -> str:
    """自动触发类用例用的冲突时刻（ISO 字符串）。

    `should_trigger` 只认 24h 前瞻窗口 `[now, now+24h]` 内的冲突；固定写
    `_tomorrow(14, 0)` 只在每天 14 点之后跑测试时才落在窗口里 —— 上午跑必挂
    （2026-10-30 上午全量回归时暴露）。取"从 now 起至少 2h 后、且本地时刻
    在 08:00~21:00 工作时段内"的最近整点，必然 ≤10h，任何时刻跑都稳定。
    """
    dt = datetime.now() + timedelta(hours=2)
    dt = dt.replace(minute=0, second=0, microsecond=0)
    while not 8 <= dt.hour <= 21:
        dt += timedelta(hours=1)
    from app.core.time import to_iso

    return to_iso(local_to_utc(dt + timedelta(minutes=offset_minutes)))


def _add(client, title: str, start: str, end: str, priority: int = 3) -> dict:
    r = client.post(
        "/api/records",
        json={
            "type": "schedule",
            "title": title,
            "start_at": start,
            "end_at": end,
            "priority": priority,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_manual_decision_without_conflict_is_not_triggered(client):
    r = client.post("/api/decisions", json={"trigger": "procrastination_signal"})
    assert r.status_code == 200
    data = r.json()
    assert data["triggered"] is False


def test_manual_trigger_always_runs(client):
    r = client.post("/api/decisions", json={"trigger": "manual"})
    assert r.status_code == 200
    data = r.json()
    assert data["triggered"] is True
    assert data["source"] == "fallback"  # 无 API Key 时的确定路径


def test_bad_trigger_is_400(client):
    r = client.post("/api/decisions", json={"trigger": "unknown"})
    assert r.status_code == 400


def test_conflict_produces_reschedule_and_persists(client):
    """改日程 -> 冲突 -> 决策 -> 落库 -> 可回看。"""
    _add(client, "部门例会", _tomorrow(14, 0), _tomorrow(15, 0), priority=2)
    _add(client, "写周报", _tomorrow(14, 30), _tomorrow(15, 30), priority=3)

    r = client.post("/api/decisions", json={"trigger": "manual"})
    data = r.json()
    assert data["triggered"] is True
    assert data["source"] == "fallback"
    payload = data["payload"]
    assert payload["decision_type"] == "reschedule"
    assert payload["suggestions"], "有硬冲突时降级引擎必须给出可执行的重排"
    assert payload["suggestions"][0]["target_record_id"]
    assert payload["intervention"]["pet_line"], "台词兜底必须有内容"

    latest = client.get("/api/decisions/latest").json()
    assert latest["has_decision"] is True
    assert latest["id"] == data["intervention_id"]
    assert latest["source"] == "fallback"


def test_record_change_triggers_decision_in_background(client):
    """约束④的落点：NL/表单入库后走既有链路，不另起一条。"""
    _add(client, "部门例会", _clash_iso(0), _clash_iso(60), priority=2)
    r = client.post(
        "/api/records",
        json={
            "type": "schedule",
            "title": "写周报",
            "start_at": _clash_iso(30),
            "end_at": _clash_iso(90),
            "priority": 3,
        },
    )
    assert r.status_code == 201

    # 后台任务在响应返回后执行，TestClient 会把它跑完
    latest = client.get("/api/decisions/latest").json()
    assert latest["has_decision"] is True
    assert latest["decision_type"] == "reschedule"
    assert latest["source"] in ("llm", "cache", "fallback")


def test_decision_history_is_listed(client):
    client.post("/api/decisions", json={"trigger": "manual"})
    rows = client.get("/api/decisions?limit=5").json()
    assert isinstance(rows, list) and rows
    assert {"id", "source", "decision_type"} <= set(rows[0])


def test_pet_mood_is_reported_alongside(client):
    """情绪始终由确定性状态机给出，与降级无关（文档 §5.3）。"""
    _add(client, "部门例会", _tomorrow(14, 0), _tomorrow(15, 0), priority=2)
    _add(client, "写周报", _tomorrow(14, 30), _tomorrow(15, 30), priority=3)
    data = client.post("/api/decisions", json={"trigger": "manual"}).json()
    # 前瞻窗口 24h，两条日程在"明天下午"，可能落在窗口外，
    # 因此这里只断言字段存在且取值合法，不锁档位。
    assert data["pet_mood"] in ("alert", "dormant", "tired", "guilty", "great", "normal")


def test_state_endpoint_still_zero_llm(client):
    """P2 的验收标准必须继续成立：/api/state 永远不调用模型。"""
    _add(client, "部门例会", _tomorrow(14, 0), _tomorrow(15, 0), priority=2)
    r = client.get("/api/state")
    assert r.status_code == 200
    assert "signals" in r.json() and "pet" in r.json()
