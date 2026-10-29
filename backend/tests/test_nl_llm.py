"""NL 接 LLM 兜底（P4）—— 兑现"规则优先，规则不会才问模型"。

这一组最想钉住的是：**模型不是免检通道。**
它给出的时间同样要过区间校验、顺序校验、跨度校验；
拿不到就 `need_user`，绝不悄悄填一个默认值（constraint ①不许硬编）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.time import local_to_utc, utc_to_local
from app.llm.provider import LLMError, LLMErrorKind
from app.services.nl_llm import parse_with_llm
from tests.llm_helpers import FakeProvider, UnavailableProvider

NOW = local_to_utc(__import__("datetime").datetime(2026, 10, 30, 10, 0))


def _run(coro):
    return asyncio.run(coro)


def _ok(**kw) -> dict:
    base = {
        "title": "写周报",
        "start": "2026-10-30T15:00:00",
        "end": "2026-10-30T16:00:00",
        "priority": 3,
        "missing": [],
        "assumptions": [],
    }
    base.update(kw)
    return base


def test_unavailable_provider_returns_error():
    result = _run(parse_with_llm("月底前交报告", NOW, provider=UnavailableProvider()))
    assert result.status == "error"
    assert result.error.code == "NXC-LLM-NA"


def test_full_result_is_resolved():
    p = FakeProvider([_ok()])
    result = _run(parse_with_llm("下个月三号之前把周报写完交上去", NOW, provider=p))
    assert result.status == "resolved"
    assert result.source == "llm"
    assert result.draft.title == "写周报"
    # 模型写的是本地墙上时间，必须被解释成本地再转 UTC
    assert utc_to_local(local_to_utc(__import__("datetime").datetime(2026, 10, 30, 15, 0))).hour == 15
    assert result.draft.start_at.startswith("2026-10-30T07:00:00")
    assert result.draft.end_at.startswith("2026-10-30T08:00:00")


def test_missing_end_becomes_need_user():
    p = FakeProvider([_ok(end=None, missing=["end_at"])])
    result = _run(parse_with_llm("月底前交报告", NOW, provider=p))
    assert result.status == "need_user"
    assert "end_at" in result.missing
    assert result.draft.end_at is None


def test_absent_time_is_never_invented():
    """模型没给时间 = 没有时间，**不是**给个"一小时后"。"""
    p = FakeProvider([_ok(start=None, end=None, missing=["start_at", "end_at"])])
    result = _run(parse_with_llm("月底前交报告", NOW, provider=p))
    assert result.draft.start_at is None and result.draft.end_at is None
    assert set(result.missing) == {"start_at", "end_at"}


def test_reversed_interval_is_error():
    p = FakeProvider([_ok(start="2026-10-30T16:00:00", end="2026-10-30T15:00:00")])
    result = _run(parse_with_llm("下午四点开始三点结束", NOW, provider=p))
    assert result.status == "error"
    assert result.error.code == "NXC-LLM-RANGE"


def test_out_of_range_year_is_dropped():
    """年份离谱（模型幻觉的典型形态）-> 当作没解析到，交回给用户。"""
    p = FakeProvider([_ok(start="1999-01-01T10:00:00", end="1999-01-01T11:00:00")])
    result = _run(parse_with_llm("很久以前的某件事", NOW, provider=p))
    assert result.status == "need_user"
    assert result.draft.start_at is None


def test_too_long_span_is_error():
    p = FakeProvider([_ok(start="2026-10-30T08:00:00", end="2026-11-01T09:00:00")])
    result = _run(parse_with_llm("从今天早上一直干到大后天", NOW, provider=p))
    assert result.status == "error"
    assert result.error.code == "NXC-LLM-SPAN"


def test_assumptions_are_passed_through():
    p = FakeProvider([
        _ok(assumptions=[{"code": "date_default_today", "label": "未提日期，按今天",
                          "field": "start_at"}])
    ])
    result = _run(parse_with_llm("下午三点开会", NOW, provider=p))
    assert result.status == "resolved"
    assert result.assumptions and result.assumptions[0].field == "start_at"


def test_provider_failure_is_error_not_exception():
    p = FakeProvider([LLMError(LLMErrorKind.NETWORK, "断网")])
    result = _run(parse_with_llm("月底前交报告", NOW, provider=p))
    assert result.status == "error"
    assert result.error.code == "NXC-LLM-FAIL"
    assert result.error.hint


def test_priority_is_clamped():
    p = FakeProvider([_ok(priority=99)])
    result = _run(parse_with_llm("最重要的事", NOW, provider=p))
    assert result.draft.priority == 5


def test_reason_is_sent_to_model():
    """规则放弃的原因要告诉模型 —— 否则它会重犯同样的错。"""
    p = FakeProvider([_ok()])
    _run(parse_with_llm("月底前交报告", NOW, provider=p, reason="『月底』没有确定的日期含义"))
    assert "月底" in p.prompts[0]


# ==================================================================
# 端到端：清单外的句子走模型，清单内的仍走规则
# ==================================================================


def test_endpoint_falls_back_to_rule_without_key(client):
    """没配 Key 时，行为必须与 P3 完全一致（source=rule）。"""
    body = client.post("/api/parse", json={"text": "月底前交报告"}).json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "NXC-NEED-LLM"
    assert body["source"] == "rule"


def test_endpoint_uses_llm_when_available(client, monkeypatch):
    fake = FakeProvider([_ok(title="交报告")])
    monkeypatch.setattr("app.llm.provider.get_provider", lambda **kw: fake)
    body = client.post("/api/parse", json={"text": "月底前交报告"}).json()
    assert body["status"] == "resolved"
    assert body["source"] == "llm"
    assert body["draft"]["title"] == "交报告"


def test_supported_sentence_still_uses_rule(client, monkeypatch):
    """清单内的句子不该惊动模型 —— 规则能解的就规则解（constraint ①）。"""
    fake = FakeProvider([_ok()])
    monkeypatch.setattr("app.llm.provider.get_provider", lambda **kw: fake)
    body = client.post("/api/parse", json={"text": "明天下午3点到5点开会"}).json()
    assert body["source"] == "rule"
    assert fake.calls == 0
