"""DEMO_MODE=true 且 fixture 未命中时的降级验收。

**为什么单独立一个文件**：答辩现场跑的就是这条路径 —— `DEMO_MODE=true` 下
观众随手改一条日程（fixture 必然未命中），必须正常降级到规则引擎，
而不是 500 / NameError / 偷偷真调模型。此前这里埋过一个真 bug
（2026-10-30 修复：`decide()` 的 DEMO 分支编辑事故导致 `outcome` 掉出
`else` 块，未命中时直接 NameError），这类 bug 只有"真跑一遍"才暴露得出来，
靠读代码看不出来 —— 三条用例就是把这条路径钉死。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.core.config import settings
from app.core.time import local_to_utc, to_iso
from app.llm.orchestrator import decide
from tests.llm_helpers import UnavailableProvider, make_request, rec


def _miss_request():
    """构造一个指纹必然不命中任何 fixture 的请求。

    fixture 是按 `scripts/seed.py` 的种子场景录的（标题、时间、条数固定）；
    这里用完全不同的标题与记录组合，指纹（相对化后的哈希）必然对不上。
    有意造一处硬冲突 —— 这样规则降级会给出 reschedule + move 建议，
    断言"降级产物是有用的方案"，而不只是"没抛异常"。
    """
    return make_request(
        [
            rec("demo_miss_a", "答辩现场观众随手改的日程甲", (16, 0), (17, 0)),
            rec("demo_miss_b", "和它撞上的日程乙", (16, 30), (17, 30), priority=4),
        ]
    )


def test_orchestrator_demo_miss_falls_back_to_rules(monkeypatch):
    """编排层：DEMO_MODE=true + 未命中 fixture → 规则降级，且绝不真调。

    provider 给的是 `UnavailableProvider` —— DEMO 分支一旦试图真实调用，
    这条测试当场以 AssertionError 失败。这钉死"断网演示不会悄悄变成联网请求"。
    """
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    # get_recorder() 每次现读 settings.DEMO_MODE，此时 replay=True：
    # 第 1 步真的会去 fixtures/demo/ 找 fixture，找不到（指纹对不上）才降级。
    # 这正是现场配置，而不是把 recorder mock 掉绕过命中检查。
    result = asyncio.run(
        decide(_miss_request(), provider=UnavailableProvider(), use_cache=False)
    )

    assert result.source == "fallback", "未命中 fixture 必须降级到规则引擎"
    assert result.attempts == 0, "DEMO 模式下一次模型调用都不该发生"
    assert result.payload.decision_type == "reschedule"
    assert result.payload.suggestions, "降级产物必须有可执行建议，不是空壳"
    assert result.payload.suggestions[0].kind == "move"
    assert any("DEMO_MODE" in (i or "") for i in result.issues), (
        "降级原因要写进 issues，答辩时才能解释这条建议为什么来自规则引擎"
    )


def test_demo_mode_http_endpoint_miss_falls_back(client, monkeypatch):
    """HTTP 端到端：贴答辩现场的完整链路（真实路由 + 真实 DB + SSE）。

    观众视角的操作序列：页面上新增两条互相冲突的日程（fixture 必然未命中）
    → 触发一次手动决策 → 必须拿到规则引擎的建议卡片，HTTP 层不能有任何异常。
    """
    monkeypatch.setattr(settings, "DEMO_MODE", True)

    # 冲突时刻必须落在 24h 前瞻窗口内（now+2h 起的最近工作时段整点），
    # 固定写死钟点会造出"上午跑必挂"的时段性 flaky（2026-10-30 的教训）。
    dt = datetime.now() + timedelta(hours=2)
    dt = dt.replace(minute=0, second=0, microsecond=0)
    while not 8 <= dt.hour <= 21:
        dt += timedelta(hours=1)
    base = to_iso(local_to_utc(dt))

    for title, offset in (("日程甲", 0), ("撞上它的日程乙", 30)):
        r = client.post(
            "/api/records",
            json={
                "type": "schedule",
                "title": title,
                "start_at": base,
                "end_at": to_iso(local_to_utc(dt.replace(minute=0) + timedelta(minutes=60 + offset))),
                "priority": 3,
            },
        )
        assert r.status_code == 201, r.text

    r = client.post("/api/decisions", json={"trigger": "manual"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "fallback", (
        "DEMO_MODE 未命中 fixture 时，HTTP 层返回的必须是规则降级结果"
    )
    assert data["delivered"] is True, "首次决策不应被节奏闸门拦截"
    payload = data["payload"]
    assert payload["decision_type"] == "reschedule"
    assert payload["suggestions"], "卡片必须有建议可点，不能是空卡片"


def test_demo_mode_latest_survives_page_refresh(client, monkeypatch):
    """刷新页面后 `GET /api/decisions/latest` 仍能取回这条规则降级结果。

    答辩演示里"刷新页面"是高频动作 —— 卡片若因刷新消失，观众会把它
    归因于降级链路坏了，而实际是持久化/查询的问题。单独钉一条。
    """
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    r = client.post("/api/decisions", json={"trigger": "manual"})
    assert r.status_code == 200, r.text

    latest = client.get("/api/decisions/latest").json()
    assert latest["has_decision"] is True
    assert latest["source"] == "fallback"
