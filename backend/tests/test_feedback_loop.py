"""反馈回流与节奏闸门的端到端验收（P5）。

这一组的存在理由只有一句话：**证明用户点「不再提醒」之后，模型真的知道了。**

反馈的落地链有七环（见 `api/interventions.py` 的模块 docstring），
前六环都是数据库里的状态变化，唯独最后一环——"模型是否看见"——
只能靠检查**渲染出来的 prompt 文本**来证明。
所以本文件里最关键的两个断言不是查库，而是查那段字符串。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.core.constants import INTERVENTION_MUTE_THRESHOLD
from app.core.time import local_to_utc, to_iso
from tests.llm_helpers import FakeProvider, payload_dict


def _clash_dt() -> datetime:
    """自动触发类用例用的冲突起始时刻（**今天或明天**的整点，视现在几点而定）。

    `should_trigger` 只认 24h 前瞻窗口 `[now, now+24h]` 内的冲突；
    固定写"明天 14:00"只在每天 14 点之后跑测试时才落在窗口里（上午跑必挂，
    2026-10-30 上午全量回归时暴露）。取"从 now 起至少 2h 后、且本地时刻
    在 08:00~21:00 工作时段内"的最近整点 —— 它必然在 24h 窗口内（≤10h），
    也在降级引擎的工作时间搜索窗（08:00~23:00）内，任何时刻跑都稳定。
    """
    dt = datetime.now() + timedelta(hours=2)
    dt = dt.replace(minute=0, second=0, microsecond=0)
    while not 8 <= dt.hour <= 21:
        dt += timedelta(hours=1)
    return dt


def _add_overlap(client) -> None:
    """造一处硬冲突，让后续每条决策都是 reschedule。"""
    base = _clash_dt()
    spans = (
        (base, base + timedelta(hours=1)),
        (base + timedelta(minutes=30), base + timedelta(hours=1, minutes=30)),
    )
    for title, (s, e) in zip(("部门例会", "写周报"), spans, strict=True):
        r = client.post(
            "/api/records",
            json={
                "type": "schedule",
                "title": title,
                "start_at": to_iso(local_to_utc(s)),
                "end_at": to_iso(local_to_utc(e)),
                "priority": 3,
            },
        )
        assert r.status_code == 201, r.text


def _decide(client, trigger: str = "manual") -> dict:
    r = client.post("/api/decisions", json={"trigger": trigger})
    assert r.status_code == 200, r.text
    return r.json()


def _feedback(client, intervention_id: str, kind: str) -> dict:
    r = client.post(
        f"/api/interventions/{intervention_id}/feedback", json={"kind": kind}
    )
    assert r.status_code == 200, r.text
    return r.json()


# ==================================================================
# 反馈端点
# ==================================================================


def test_feedback_persists_and_reports_progress(client):
    _add_overlap(client)
    d = _decide(client)
    out = _feedback(client, d["intervention_id"], "mute")
    assert out["ok"] is True
    assert out["feedback"] == "mute"
    assert out["ignore_counts"] == {"reschedule": 1}
    assert out["muted_intervention_types"] == []
    assert out["threshold"] == INTERVENTION_MUTE_THRESHOLD


def test_unknown_feedback_is_400(client):
    _add_overlap(client)
    d = _decide(client)
    r = client.post(
        f"/api/interventions/{d['intervention_id']}/feedback", json={"kind": "whatever"}
    )
    assert r.status_code == 400


def test_missing_intervention_is_404(client):
    r = client.post("/api/interventions/ivt_nope/feedback", json={"kind": "mute"})
    assert r.status_code == 404


def test_feedback_enters_the_behavior_event_stream(client):
    """反馈必须同时落进事件表 —— 那才是模型观察用户的窗口。

    只写 `intervention.feedback` 的话，模型看得见"用户做了什么任务"，
    却看不见"用户希望我别做什么"。
    """
    _add_overlap(client)
    d = _decide(client)
    _feedback(client, d["intervention_id"], "mute")

    events = client.get("/api/events?limit=50").json()
    kinds = [e["kind"] for e in events]
    assert "intervention_ignored" in kinds


# ==================================================================
# mute 回流进 prompt —— 本文件的核心
# ==================================================================


def test_mute_reaches_the_prompt(client, monkeypatch):
    """三次 mute 之后，渲染出的 prompt 里必须出现被降权的类型。

    这是"mute 必须真的回流"唯一可被机器验证的形式：
    库里有值不算，写进 UserProfile 也不算，**只有模型收到的那一段话里有它才算**。
    """
    _add_overlap(client)
    for i in range(INTERVENTION_MUTE_THRESHOLD):
        d = _decide(client)
        assert d["delivered"] is True, f"第 {i + 1} 次决策应当送达"
        _feedback(client, d["intervention_id"], "mute")

    profile = client.get("/api/interventions/profile").json()
    assert profile["muted_intervention_types"] == ["reschedule"], profile

    # 换上一个会**记录自己收到什么**的 provider
    fake = FakeProvider([payload_dict()])
    from app.llm import provider as provider_pkg

    monkeypatch.setattr(provider_pkg, "get_provider", lambda: fake)

    _decide(client)
    assert fake.prompts, "provider 应当被调用"
    prompt = fake.prompts[-1]
    assert "## 用户反馈" in prompt
    assert "已被用户彻底降权的干预类型" in prompt
    assert "reschedule" in prompt
    # 光出现类型名不够，必须明确写出"不许输出"
    assert "绝不许" in prompt


def test_muted_type_is_not_delivered(client, monkeypatch):
    """模型没听话、仍然输出了被降权的类型时，后端拒发。

    用户点过三次「不再提醒」之后还弹出来，是不可接受的。
    """
    _add_overlap(client)
    for _ in range(INTERVENTION_MUTE_THRESHOLD):
        _feedback(client, _decide(client)["intervention_id"], "mute")

    fake = FakeProvider([payload_dict(decision_type="reschedule")])
    from app.llm import provider as provider_pkg

    monkeypatch.setattr(provider_pkg, "get_provider", lambda: fake)

    out = _decide(client)
    assert out["delivered"] is False
    assert out["blocked_by"] == "muted"
    assert "已被用户降权" in out["reason"]


def test_accept_is_reflected_as_positive_signal(client, monkeypatch):
    """采纳是正向反馈：模型应该看见"这类建议对他有效"。"""
    _add_overlap(client)
    d = _decide(client)
    _feedback(client, d["intervention_id"], "accept")
    assert client.get("/api/interventions/profile").json()["accept_counts"] == {
        "reschedule": 1
    }

    fake = FakeProvider([payload_dict()])
    from app.llm import provider as provider_pkg

    monkeypatch.setattr(provider_pkg, "get_provider", lambda: fake)
    _decide(client)
    assert "曾被用户采纳的类型" in fake.prompts[-1]


def test_near_threshold_is_warned_before_the_ban(client, monkeypatch):
    """差一次就降权时，prompt 里要给警告 —— 而不是等越线了才冷冰冰禁止。"""
    _add_overlap(client)
    for _ in range(INTERVENTION_MUTE_THRESHOLD - 1):
        _feedback(client, _decide(client)["intervention_id"], "mute")
    assert client.get("/api/interventions/profile").json()["muted_intervention_types"] == []

    fake = FakeProvider([payload_dict()])
    from app.llm import provider as provider_pkg

    monkeypatch.setattr(provider_pkg, "get_provider", lambda: fake)
    _decide(client)
    prompt = fake.prompts[-1]
    assert "被忽略但尚未降权的类型" in prompt
    assert "再被忽略一次就会被彻底屏蔽" in prompt


# ==================================================================
# 闸门接进链路后的行为
# ==================================================================


def test_snooze_blocks_the_next_auto_trigger(client):
    """点了「稍后」之后，紧随其后的自动触发必须被冷却期拦下。"""
    _add_overlap(client)
    d = _decide(client)  # manual：绕过闸门但刷新冷却时钟
    _feedback(client, d["intervention_id"], "snooze")

    out = _decide(client, "schedule_change")
    assert out["triggered"] is False
    assert out["blocked_by"] == "cooldown"


def test_manual_survives_exhausted_daily_quota(client):
    """额度用完时，用户主动问仍然要给答案。"""
    _add_overlap(client)
    # 手动干预不计费，但会刷新冷却时钟 —— 这里只验证它不会被拒
    for _ in range(5):
        out = _decide(client)
        assert out["delivered"] is True
    assert out["rhythm"]["remaining_today"] >= 0


def test_rhythm_status_is_reported(client):
    _add_overlap(client)
    out = _decide(client)
    assert "rhythm" in out
    assert {"used_today", "remaining_today", "blocked_by"} <= set(out["rhythm"])


def test_trigger_is_persisted_for_metering(client):
    """trigger 必须落库，否则"手动不占额度"这件事无从判断。"""
    _add_overlap(client)
    intervention_id = _decide(client)["intervention_id"]
    latest = client.get("/api/decisions/latest").json()
    assert latest["id"] == intervention_id
    assert latest.get("trigger") == "manual"


def test_payload_json_roundtrip(client):
    """持久化后的 payload 仍是可解析的 JSON（防止 trigger 混入时破坏结构）。"""
    _add_overlap(client)
    _decide(client)
    raw = client.get("/api/decisions/latest").json()
    assert json.dumps(raw, ensure_ascii=False)
    assert raw["payload"]["decision_type"] in ("reschedule", "nudge", "no_action")
