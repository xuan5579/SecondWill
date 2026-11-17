"""决策结果的新鲜度复核（2026-11-17 补）。

## 为什么补这一组

录制走查时实测到一个很难看的场面：连着勾掉五条记录，第一步触发的决策要跑
9 秒才落库；落库那一刻五条已经全部完成，卡片却还在建议「把整理实验数据挪到
明早 08:00」——而那条刚被用户自己勾掉。

过时的建议比没有建议更糟：它让人怀疑整套判断在看旧数据。所以落库前要复核，
判据两条：触发条件是否还在、建议指向的记录是否还活着。

这组用例直接打 `_stale_reason`，不经过模型（走 fallback 也行，但那样要多造
一堆冲突数据才能命中同一个分支）。
"""

from __future__ import annotations


def _add(client, title: str, priority: int = 3) -> str:
    r = client.post(
        "/api/records",
        json={
            "type": "schedule",
            "title": title,
            "priority": priority,
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def _result(record_ids: list[str], decision_type: str = "reschedule"):
    from app.llm.contract import DecisionPayload, Suggestion
    from app.llm.orchestrator import DecisionResult

    payload = DecisionPayload(
        decision_type=decision_type,
        summary="测试用",
        suggestions=[
            Suggestion(kind="move", target_record_id=rid, rationale="测试") for rid in record_ids
        ],
    )
    return DecisionResult(payload, "fallback")


def test_done_target_makes_suggestion_stale(client):
    """建议指向的日程已经完成 -> 不送达（手动触发同样拦）。"""
    from app.db.session import SessionLocal
    from app.services.decision_flow import _stale_reason

    rid = _add(client, "整理实验数据")
    client.patch(f"/api/records/{rid}", json={"status": "done"})

    db = SessionLocal()
    try:
        reason = _stale_reason(db, _result([rid]), "manual")
    finally:
        db.close()

    assert reason is not None
    assert "已完成" in reason


def test_deleted_target_makes_suggestion_stale(client):
    """建议指向的日程被删除 -> 不送达。"""
    from app.db.session import SessionLocal
    from app.services.decision_flow import _stale_reason

    rid = _add(client, "临时日程")
    client.delete(f"/api/records/{rid}")

    db = SessionLocal()
    try:
        reason = _stale_reason(db, _result([rid]), "manual")
    finally:
        db.close()

    assert reason is not None


def test_pending_target_passes(client):
    """目标还在待办 -> 放行。手动触发不受触发条件约束。"""
    from app.db.session import SessionLocal
    from app.services.decision_flow import _stale_reason

    rid = _add(client, "正常日程")

    db = SessionLocal()
    try:
        assert _stale_reason(db, _result([rid]), "manual") is None
    finally:
        db.close()


def test_condition_gone_makes_auto_trigger_stale(client):
    """自动触发：库里已经没有冲突也没有逾期 -> 状态变了，不再打扰。"""
    from app.db.session import SessionLocal
    from app.services.decision_flow import _stale_reason

    rid = _add(client, "一条安分的日程")

    db = SessionLocal()
    try:
        # 同上一条数据，但这次是系统自动触发 —— 触发条件已不成立，必须拦下
        reason = _stale_reason(db, _result([rid]), "schedule_change")
    finally:
        db.close()

    assert reason is not None
    assert "状态在决策期间已变" in reason


def test_no_action_payload_is_not_stale_by_target(client):
    """no_action 没有建议，不该因为"目标不存在"被误拦 —— 它本来就不用目标。"""
    from app.db.session import SessionLocal
    from app.services.decision_flow import _stale_reason

    _add(client, "随便一条")

    db = SessionLocal()
    try:
        assert _stale_reason(db, _result([], decision_type="no_action"), "manual") is None
    finally:
        db.close()
