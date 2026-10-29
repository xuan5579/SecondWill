"""编排器与降级引擎（P4 的核心）。

这一组要钉住的只有一件事：**模型挂了，系统照样能用，且用户看得出差别。**

- `source` 必须是 `llm` / `cache` / `fallback` 三选一，且与实际路径一致；
- 降级方案的 `caveats` 里必须承认自己是规则生成的，不许冒充模型；
- 降级**不影响宠物情绪** —— 情绪由确定性状态机算，与这条链路无关（文档 §5.3）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.constants import LLM_MAX_RETRIES, LLM_TOKEN_LADDER
from app.llm.contract import DecisionPayload
from app.llm.orchestrator import (
    clear_cache,
    decide,
    should_trigger,
)
from app.llm.provider import LLMError, LLMErrorKind, LLMResponse
from app.llm.validator import validate_payload
from app.services.fallback import build_fallback
from tests.llm_helpers import (
    NOW,
    FakeProvider,
    UnavailableProvider,
    make_request,
    payload_dict,
    rec,
    suggestion_dict,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    clear_cache()
    yield
    clear_cache()


A = rec("rec_a", "晨会", (14, 0), (15, 0), priority=2)
B = rec("rec_b", "写周报", (14, 30), (15, 30), priority=3)


# ==================================================================
# 触发判定（文档 B3）
# ==================================================================


def _signals(hard: int = 0, index: float = 0.0):
    class S:
        conflicts = [type("C", (), {"hard": True})() for _ in range(hard)]
        procrastination_index = index

    return S()


def test_trigger_manual_always():
    assert should_trigger(_signals(), "manual")[0] is True


def test_trigger_on_hard_conflict():
    ok, reason = should_trigger(_signals(hard=1), "schedule_change")
    assert ok and "硬冲突" in reason


def test_trigger_on_high_index():
    ok, _ = should_trigger(_signals(index=0.6), "schedule_change")
    assert ok


def test_no_trigger_when_calm():
    ok, reason = should_trigger(_signals(), "schedule_change")
    assert ok is False and "未达阈值" in reason


def test_no_trigger_on_soft_conflict_only():
    """软冲突（no_buffer / overload）不该打扰模型 —— 那是最常见的状态。"""
    class S:
        conflicts = [type("C", (), {"hard": False})()]
        procrastination_index = 0.1

    assert should_trigger(S(), "schedule_change")[0] is False


# ==================================================================
# 降级路径
# ==================================================================


def test_no_api_key_falls_back():
    req = make_request([A, B])
    result = _run(decide(req, provider=UnavailableProvider()))
    assert result.source == "fallback"
    assert result.payload.decision_type in ("reschedule", "nudge", "no_action")


def test_network_error_falls_back():
    req = make_request([A, B])
    p = FakeProvider([LLMError(LLMErrorKind.NETWORK, "断网了")])
    result = _run(decide(req, provider=p))
    assert result.source == "fallback"
    assert any("L0" in i for i in result.issues)


def test_empty_content_falls_back():
    req = make_request([A, B])
    p = FakeProvider([LLMError(LLMErrorKind.EMPTY, "空响应")])
    result = _run(decide(req, provider=p))
    assert result.source == "fallback"


def test_schema_error_retries_then_falls_back():
    """结构错 -> 重试 LLM_MAX_RETRIES+1 次 -> 仍失败 -> 降级。"""
    req = make_request([A, B])
    p = FakeProvider([{"decision_type": "瞎编的"} for _ in range(LLM_MAX_RETRIES + 1)])
    result = _run(decide(req, provider=p))
    assert result.source == "fallback"
    assert p.calls == LLM_MAX_RETRIES + 1


def test_second_attempt_succeeds():
    req = make_request([A, B])
    good = payload_dict(
        suggestions=[suggestion_dict("rec_b", start=(16, 0), end=(17, 0))],
        summary="把写周报挪到 16:00",
    )
    p = FakeProvider([{"decision_type": "瞎编的"}, good])
    result = _run(decide(req, provider=p))
    assert result.source == "llm" and result.attempts == 2
    assert result.payload.suggestions[0].target_record_id == "rec_b"


def test_retry_sends_validation_errors_back():
    """第二次请求必须带上第一轮的错误原文，否则重试只是碰运气。"""
    req = make_request([A, B])
    good = payload_dict(suggestions=[suggestion_dict("rec_b", start=(16, 0), end=(17, 0))])
    p = FakeProvider([{"confidence": 9}, good])

    captured: list[str] = []

    async def spy(messages, *, max_tokens=None, timeout=None):
        captured.append(messages[-1]["content"])
        item = p.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return LLMResponse(content=json.dumps(item), data=item)

    p.complete_json = spy  # type: ignore[assignment]
    _run(decide(req, provider=p))
    assert len(captured) == 2
    assert "上一轮输出被拒绝" in captured[1]


def test_truncated_output_retries_with_more_tokens():
    req = make_request([A, B])
    good = payload_dict(suggestions=[suggestion_dict("rec_b", start=(16, 0), end=(17, 0))])
    p = FakeProvider([LLMError(LLMErrorKind.TRUNCATED, "被截断"), good])
    result = _run(decide(req, provider=p))
    assert result.source == "llm"
    # 抬档必须**严格大于**默认值：默认的 4096 再来一次等于原地重试
    assert p.max_tokens_seen[1] > settings.LLM_MAX_TOKENS
    assert p.max_tokens_seen[1] in LLM_TOKEN_LADDER


def test_empty_content_also_raises_token_budget():
    """空 content 与截断是同一个病，必须走同一条抬档路径。

    这条测试是 docs/09 §3② 的守卫：当初把它当"偶发故障直接降级"，
    实测才发现 finish_reason 就是 length、推理 token 吃光了额度。
    """
    req = make_request([A, B])
    good = payload_dict(suggestions=[suggestion_dict("rec_b", start=(16, 0), end=(17, 0))])
    p = FakeProvider([LLMError(LLMErrorKind.EMPTY, "模型返回空 content"), good])
    result = _run(decide(req, provider=p))
    assert result.source == "llm"
    assert p.max_tokens_seen[1] > settings.LLM_MAX_TOKENS


def test_cache_hit_does_not_call_again():
    req = make_request([A, B])
    good = payload_dict(suggestions=[suggestion_dict("rec_b", start=(16, 0), end=(17, 0))])
    p = FakeProvider([good])
    first = _run(decide(req, provider=p))
    second = _run(decide(req, provider=p))
    assert first.source == "llm"
    assert second.source == "cache" and second.attempts == 0
    assert p.calls == 1


def test_demo_fixture_replay(tmp_path: Path):
    from app.llm.cache import DemoRecorder

    req = make_request([A, B])
    recorder = DemoRecorder(base_dir=tmp_path, replay=False, record=True)
    good = payload_dict(suggestions=[suggestion_dict("rec_b", start=(16, 0), end=(17, 0))])
    _run(decide(req, provider=FakeProvider([good]), recorder=recorder))
    assert list(tmp_path.glob("decision_*.json"))

    # 换个"不会响应"的 provider：命中录制就不该再发起调用
    replay = DemoRecorder(base_dir=tmp_path, replay=True, record=False)
    result = _run(decide(req, provider=UnavailableProvider(), recorder=replay))
    assert result.source == "cache"


def test_l3_model_says_no_action_but_signal_is_strong():
    """模型说别管，但拖延指数 0.9 -> 后端仍发规则提醒（文档 §6.1 L3）。

    这里刻意用**没有冲突**的场景：若有冲突，规则引擎本来就会给出重排方案，
    那就验证不到"后端覆盖模型的 no_action"这一层了。
    """
    req = make_request([A], index=0.9, mood="tired")
    p = FakeProvider([payload_dict(decision_type="no_action", summary="先不管")])
    result = _run(decide(req, provider=p))
    assert result.source == "fallback"
    assert result.payload.decision_type == "nudge"
    assert any("L3" in i for i in result.issues)


def test_model_no_action_is_respected_when_signal_is_weak():
    """反过来：信号不强时，模型说"不干预"就真的不干预。

    这条与上一条成对存在 —— 只写"后端可以覆盖模型"会变成过度干预的借口。
    """
    req = make_request([A, B], index=0.1)
    p = FakeProvider([payload_dict(decision_type="no_action")])
    result = _run(decide(req, provider=p))
    assert result.source == "llm" and result.payload.decision_type == "no_action"


# ==================================================================
# 规则降级引擎
# ==================================================================


def test_fallback_resolves_overlap():
    req = make_request([A, B])
    payload = build_fallback(req, now=NOW)
    assert payload.decision_type == "reschedule"
    assert payload.suggestions
    # 降级方案自己也要过一遍校验：它必须真的解决问题，而不只是"有输出"
    check = validate_payload(payload.model_dump(mode="json"), req)
    assert check.ok is True, check.error_text


def test_fallback_prefers_higher_priority():
    """优先级高（数值小）的先占位 -> 被挪走的应当是低优先级那条。"""
    req = make_request([A, B])  # A priority=2, B priority=3
    payload = build_fallback(req, now=NOW)
    assert [s.target_record_id for s in payload.suggestions] == ["rec_b"]


def test_fallback_no_action_when_calm():
    req = make_request([A])
    payload = build_fallback(req, now=NOW)
    assert payload.decision_type == "no_action"


def test_fallback_nudges_on_overload():
    req = make_request([A], load_ratio=1.4)
    payload = build_fallback(req, now=NOW)
    assert payload.decision_type == "nudge"
    assert payload.intervention is not None
    assert "负载" in payload.intervention.message


def test_fallback_admits_being_a_rule():
    """降级文案必须承认自己是规则生成的 —— 冒充模型判断是事故不是技巧。"""
    req = make_request([A, B])
    payload = build_fallback(req, now=NOW)
    assert any("规则引擎" in c for c in payload.caveats)


def test_fallback_keeps_pet_line_without_changing_mood():
    """降级只提供台词，情绪仍由状态机决定（文档 §5.3）。"""
    from app.services.pet import PetMood, PetState, compute

    req = make_request([A, B], mood="alert")
    payload = build_fallback(req, now=NOW)
    assert payload.intervention is not None and payload.intervention.pet_line

    # 同一份输入，情绪不受降级影响：这里显式算一次确认 mood 仍是 alert 分支
    from app.services.conflict import detect_conflicts
    from app.services.signal import Signals
    from app.llm.validator import to_sim_records
    from app.core.constants import FORWARD_WINDOW
    from app.services.conflict import TimeWindow

    sims = to_sim_records(req.records)
    conflicts = tuple(detect_conflicts(sims, TimeWindow(NOW, NOW + FORWARD_WINDOW)))
    signals = Signals(
        conflicts=conflicts, procrastination_index=0.0, load_ratio=0.0,
        idle_blocks=(), completion_rate_7d=0.5, completion_rate_today=0.0,
        overdue_count=0, avg_overdue_days=0.0, streak_days=0, due_total_7d=0,
    )
    state = compute(sims, [], signals, NOW)
    assert state.mood is PetMood.ALERT
    assert payload.intervention.pet_line == __import__(
        "app.services.pet", fromlist=["template_line"]
    ).template_line(state, seed=sum(ord(c) for c in "alert"))


def test_fallback_never_moves_into_night():
    """规则引擎不得把日程塞到工作时间之外。"""
    req = make_request([A, B], working_start="08:00", working_end="23:00")
    payload = build_fallback(req, now=NOW)
    from app.core.time import utc_to_local

    for s in payload.suggestions:
        start_local = utc_to_local(s.proposed_start)
        end_local = utc_to_local(s.proposed_end)
        assert 8 <= start_local.hour < 23
        assert end_local.hour <= 23


def test_fallback_slot_is_aligned_to_quarter_hour():
    req = make_request([A, B])
    payload = build_fallback(req, now=NOW)
    from app.core.time import utc_to_local

    for s in payload.suggestions:
        assert utc_to_local(s.proposed_start).minute % 15 == 0


def test_decision_payload_serializable():
    """最终给前端的东西必须能被 JSON 序列化（时间要带时区）。"""
    req = make_request([A, B])
    payload = build_fallback(req, now=NOW)
    data = json.loads(payload.model_dump_json())
    assert data["decision_type"] == "reschedule"
    assert data["suggestions"][0]["proposed_start"].endswith("+00:00")
    # 反序列化回来仍是同一个契约对象
    assert DecisionPayload.model_validate(data).decision_type == "reschedule"


def test_fingerprint_is_stable_when_whole_scenario_shifts_a_day():
    """场景整体平移一天后，指纹必须不变。

    这是 DEMO fixture 能"隔天还命中"的唯一根据。若指纹里存的是绝对时间，
    今天录的响应到明天演示就会全部落空，现场只能看到降级方案 ——
    那等于录制这件事白做了。
    """
    from datetime import timedelta

    from app.llm.contract import TimeRange
    from app.llm.orchestrator import relativized_payload

    def shifted(days: int):
        a = rec("rec_a", "晨会", (14, 0), (15, 0), priority=2, day_offset=days)
        b = rec("rec_b", "写周报", (14, 30), (15, 30), priority=3, day_offset=days)
        req = make_request([a, b])
        base = NOW + timedelta(days=days)
        # horizon 也要整体平移，否则它相对基准的偏移会变，指纹自然不同
        return req.model_copy(
            update={"triggered_at": base, "horizon": TimeRange(start=base, end=base + timedelta(hours=24))}
        )

    import json

    p0 = json.dumps(relativized_payload(shifted(0)), sort_keys=True)
    p3 = json.dumps(relativized_payload(shifted(3)), sort_keys=True)
    assert p0 == p3
    # 但内容不同（标题变了）时绝不能撞车
    other = make_request([rec("rec_x", "另一件事", (14, 0), (15, 0))])
    assert json.dumps(relativized_payload(other), sort_keys=True) != p0
