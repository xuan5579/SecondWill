"""对抗性验证：枚举归一化兜底真的能接住跑偏的模型输出。

背景：D 组修复后跑了 6 次真实调用，归一化**一次都没触发**（主力 prompt 把
跑偏拦在了前面）。这当然是好事，但也意味着兜底路径**从未被真实执行过**——
一个从没触发过的兜底，等于没有兜底。所以这里用手工构造的"坏响应"喂进
完整编排器，验证三件事：

1. **接得住**：`time_conflict` / `dismiss` 这类同义跑偏被就地修正，
   一次通过校验，不浪费重试；
2. **留得下痕迹**：模型原始输出写进日志（WARNING 级别），
   `DecisionResult.issues` 原样回传给上层 —— "模型到底写了什么"不能消失；
3. **拿得准轻重**：摇摆词（`ignore`）**刻意不映射**，让它校验失败、
   走"回传错误原文重试"的显式路径，而不是替模型猜一个可能错的答案。

这三条分别对应 `docs/01 §9.5` 的 D3 两层设计与"宁轻勿重"硬规则。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.llm.orchestrator import clear_cache, decide
from tests.llm_helpers import (
    FakeProvider,
    make_request,
    payload_dict,
    rec,
    suggestion_dict,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_cache()
    yield
    clear_cache()


A = rec("rec_a", "晨会", (14, 0), (15, 0), priority=2)
B = rec("rec_b", "写周报", (14, 30), (15, 30), priority=3)


def _bad_payload(action_kinds: list[str], tag: str = "time_conflict") -> dict:
    """构造一个"枚举全面跑偏"但其余部分合法的模型输出。

    刻意同时混入合法值（`accept`）与跑偏值 —— 真实跑偏从来不是
    整份输出全错，而是好坏混杂，归一化必须只动错的那几个。
    """
    return payload_dict(
        reasoning_tags=[tag],
        suggestions=[suggestion_dict("rec_b", start=(16, 0), end=(17, 0))],
        intervention={
            "type": "reschedule_proposal",
            "title": "有个时间撞了",
            "message": "「写周报」和晨会重叠了，挪到 16:00 就错开了。",
            "pet_line": "喵，两条日程挤在一起啦。",
            "tone": "gentle",
            "actions": [
                {"label": "采纳", "kind": "accept"},
                *[{"label": "先不了", "kind": k} for k in action_kinds],
            ],
        },
    )


def _all_issue_texts(result) -> list[str]:
    return [str(i) for i in result.issues]


# ==================================================================
# 1. 同义跑偏：归一化接住，一次通过，原文留痕
# ==================================================================


def test_time_conflict_and_dismiss_are_caught_in_one_shot(caplog):
    """`time_conflict`→conflict、`dismiss`→snooze（不是 mute！），一次过。"""
    with caplog.at_level(logging.WARNING, logger="app.llm.normalize"):
        p = FakeProvider([_bad_payload(["dismiss"])])
        result = asyncio.run(decide(make_request([A, B]), provider=p, use_cache=False))

    # ---- 接得住：一次调用就通过，没浪费重试 ----
    assert result.source == "llm"
    assert p.calls == 1, "同义跑偏应该就地归一化，而不是靠重试"
    assert result.payload.reasoning_tags == ["conflict"]
    kinds = [a.kind for a in result.payload.intervention.actions]
    assert kinds == ["accept", "snooze"], "dismiss 必须归一化成 snooze，绝不许是 mute"

    # ---- issues 回传：上层能看到"模型原来写的是什么" ----
    texts = _all_issue_texts(result)
    assert any("time_conflict" in t and "conflict" in t for t in texts), texts
    assert any("'dismiss'" in t and "'snooze'" in t for t in texts), texts
    assert all("L1" not in t for t in texts), "归一化是 LEVEL_OK，不该触发重试级问题"

    # ---- 日志留痕：WARNING 里记了模型原始输出 ----
    log_text = caplog.text
    assert "'time_conflict'" in log_text, "日志必须保留模型原始值，否则跑偏无从查起"
    assert "'dismiss'" in log_text
    assert log_text.count("枚举归一化") >= 2


def test_dismiss_never_becomes_mute():
    """这是 P5 的红线：mute 会写进 muted_intervention_types，永久降权不可逆。

    单独成一条测试而不是并进上一条 —— 这条断言值得在失败报告里独占一行。
    """
    p = FakeProvider([_bad_payload(["dismiss"])])
    result = asyncio.run(decide(make_request([A, B]), provider=p, use_cache=False))
    kinds = [a.kind for a in result.payload.intervention.actions]
    assert "mute" not in kinds


# ==================================================================
# 2. 摇摆词：刻意不映射 -> L1 失败 -> 带错误原文重试
# ==================================================================


def test_ambiguous_word_is_not_guessed_and_retries_with_error_text(caplog):
    """`ignore` 在『忽略一次』与『永久不想看』之间摇摆，不许归一化猜。

    期望路径：第 1 次 L1 失败 -> 编排器把校验错误原文回传给模型
    -> 第 2 次模型写对 -> 采纳。宁可多一次调用，不要静默污染数据。
    """
    good = _bad_payload(["snooze"])
    with caplog.at_level(logging.WARNING, logger="app.llm.normalize"):
        p = FakeProvider([_bad_payload(["ignore"]), good])
        result = asyncio.run(decide(make_request([A, B]), provider=p, use_cache=False))

    assert result.source == "llm"
    assert p.calls == 2, "摇摆词应该失败一次再重试，而不是被猜一个答案"
    assert result.attempts == 2
    kinds = [a.kind for a in result.payload.intervention.actions]
    assert kinds == ["accept", "snooze"]

    # 第一次的校验错误被原文回传：模型能看到具体哪个字段、允许值是什么。
    # （Pydantic 的错误原文是 "Input should be 'accept', 'snooze' or 'mute'"，
    #  回传的是"错在哪 + 该写什么"，比单纯重复错词更有用。）
    assert "intervention.actions.1.kind" in p.prompts[1]
    assert "'snooze'" in p.prompts[1]
    # 归一化对摇摆词保持沉默：日志里没有任何关于 ignore 的映射记录
    assert "ignore" not in caplog.text


# ==================================================================
# 3. 混合跑偏：一次输出里错多个字段，逐个修，互不干扰
# ==================================================================


def test_multiple_fields_drift_together():
    """decision_type / tag / suggestion.kind / tone 四处同时跑偏。"""
    raw = _bad_payload(["later"], tag="overlapping")
    raw["decision_type"] = "rearrange"       # -> reschedule
    raw["suggestions"][0]["kind"] = "relocate"  # -> move
    raw["intervention"]["tone"] = "cheerful"  # -> playful

    p = FakeProvider([raw])
    result = asyncio.run(decide(make_request([A, B]), provider=p, use_cache=False))

    assert result.source == "llm"
    assert p.calls == 1
    assert result.payload.decision_type == "reschedule"
    assert result.payload.reasoning_tags == ["conflict"]
    assert result.payload.suggestions[0].kind == "move"
    assert result.payload.intervention.tone == "playful"
    assert [a.kind for a in result.payload.intervention.actions] == ["accept", "snooze"]
    # 5 处归一化都要在 issues 里留痕
    texts = _all_issue_texts(result)
    assert sum("枚举归一化" in t for t in texts) == 5, texts
