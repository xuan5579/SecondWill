"""枚举归一化（D3）与 prompt 枚举段。

这一组要钉住的是**边界**，不是"能替换"：

- 该救的必须救回来（`time_conflict`、`dismiss`）；
- 不该猜的**绝不能猜**（`ignore`、`stop`）—— 猜错会静默污染数据，
  而失败重试至少是显式的、且模型通常一次就改对；
- 每一次归一化都必须留下原始值的痕迹，否则下次跑偏无从查起。
"""

from __future__ import annotations

import logging

import pytest

from app.llm.normalize import ALIASES, normalize_enums
from app.llm.validator import validate_payload
from tests.llm_helpers import make_request, rec, suggestion_dict


def test_time_conflict_becomes_conflict():
    raw = {"decision_type": "reschedule", "reasoning_tags": ["time_conflict", "streak"]}
    out, notes = normalize_enums(raw)
    assert out["reasoning_tags"] == ["conflict", "streak"]
    assert notes == [("reasoning_tags[0]", "time_conflict", "conflict")]


def test_dismiss_is_snooze_not_mute():
    """`dismiss` = 这次先不了，映射成 `mute` 会让用户被永久降权。

    这是评审时明确改掉的一条：`mute` 会写进 `UserProfile.muted_intervention_types`，
    后果不可逆；而 `dismiss` 的字面意思就是"这次不要"。宁轻勿重。
    """
    raw = {"intervention": {"actions": [{"label": "先不了", "kind": "dismiss"}]}}
    out, notes = normalize_enums(raw)
    assert out["intervention"]["actions"][0]["kind"] == "snooze"
    assert notes[0][2] == "snooze"


def test_key_is_case_and_separator_insensitive():
    raw = {"decision_type": " No-Action ", "reasoning_tags": ["TIME CONFLICT"]}
    out, _ = normalize_enums(raw)
    assert out["decision_type"] == "no_action"
    assert out["reasoning_tags"] == ["conflict"]


@pytest.mark.parametrize("word", ["ignore", "stop", "normal", "habit", "banana"])
def test_ambiguous_or_unknown_words_are_left_alone(word):
    """拿不准的一律不动 —— 让校验照旧失败并把原文回传，好过替模型猜一个可能错的答案。"""
    raw = {"reasoning_tags": [word], "intervention": {"actions": [{"label": "x", "kind": word}]}}
    out, notes = normalize_enums(raw)
    assert out["reasoning_tags"] == [word]
    assert out["intervention"]["actions"][0]["kind"] == word
    assert notes == []


def test_postpone_is_context_dependent():
    """同一个词在不同字段里含义不同：actions 里是"稍后提醒"，suggestions 里是"挪时间"。"""
    raw = {
        "suggestions": [{"kind": "postpone", "target_record_id": "rec_a"}],
        "intervention": {"actions": [{"label": "稍后", "kind": "postpone"}]},
    }
    out, _ = normalize_enums(raw)
    assert out["suggestions"][0]["kind"] == "move"
    assert out["intervention"]["actions"][0]["kind"] == "snooze"


def test_non_string_values_pass_through():
    raw = {"reasoning_tags": [None, 42], "decision_type": {"a": 1}}
    out, notes = normalize_enums(raw)
    assert out == raw and notes == []


def test_original_value_is_logged(caplog):
    """原始值必须留痕：否则"模型到底写了什么"这件事会彻底消失。"""
    with caplog.at_level(logging.WARNING):
        normalize_enums({"reasoning_tags": ["time_conflict"]})
    assert "time_conflict" in caplog.text
    assert "conflict" in caplog.text


def test_validator_saves_a_payload_that_would_otherwise_fail():
    """端到端：模型写了同义词，校验应该救回来而不是判 L1。"""
    req = make_request([rec("rec_a", "例会", (14, 0), (15, 0)),
                        rec("rec_b", "写周报", (14, 30), (15, 30))])
    raw = {
        "schema_version": "1.0",
        "decision_type": "reschedule",
        "confidence": 0.7,
        "summary": "挪开重叠",
        "reasoning_tags": ["time_conflict"],
        "suggestions": [suggestion_dict("rec_b", kind="reschedule", start=(15, 30), end=(16, 30))],
        "intervention": {
            "type": "reschedule_proposal",
            "title": "撞了",
            "message": "两条撞了 30 分钟",
            "pet_line": "挪一下就好",
            "tone": "warm",
            "actions": [{"label": "好", "kind": "accept"},
                        {"label": "先不了", "kind": "dismiss"}],
        },
    }
    result = validate_payload(raw, req)
    assert result.ok, result.error_text
    assert result.payload is not None
    assert result.payload.reasoning_tags == ["conflict"]
    assert result.payload.suggestions[0].kind == "move"
    assert result.payload.intervention is not None
    assert result.payload.intervention.tone == "gentle"
    assert [a.kind for a in result.payload.intervention.actions] == ["accept", "snooze"]
    # 归一化要留下可审计的痕迹，但不能改变"这次校验通过"的结论
    joined = " ".join(str(i) for i in result.issues)
    assert "time_conflict" in joined and "dismiss" in joined
    assert all(i.level == "ok" for i in result.issues if "归一化" in str(i))


def test_prompt_lists_allowed_values_and_the_common_mistakes():
    """prompt 是主力：允许值必须由契约生成（不手写），并给出易错对照。"""
    from app.llm.prompts.decision import _render_enum_hint

    hint = _render_enum_hint()
    for value in ("reschedule", "nudge", "no_action", "move", "shrink", "drop",
                  "low", "medium", "high", "conflict", "overload", "procrastination",
                  "idle", "streak", "gentle", "direct", "playful", "accept", "snooze", "mute"):
        assert f'"{value}"' in hint, f"允许值 {value} 没出现在 prompt 里"
    assert "time_conflict" in hint and "dismiss" in hint
    assert "snooze" in hint.split("dismiss")[1][:80], "dismiss 的对照必须指向 snooze"


def test_alias_table_maps_every_value_to_a_legal_one():
    """映射表自身的一致性：每个目标值都必须是该字段的合法枚举。"""
    legal = {
        "decision_type": {"reschedule", "nudge", "no_action"},
        "suggestion_kind": {"move", "shrink", "drop"},
        "risk": {"low", "medium", "high"},
        "reasoning_tag": {"conflict", "overload", "procrastination", "idle", "streak"},
        "intervention_type": {"reschedule_proposal", "nudge"},
        "tone": {"gentle", "direct", "playful"},
        "action_kind": {"accept", "snooze", "mute"},
    }
    for field, table in ALIASES.items():
        for src, dst in table.items():
            assert dst in legal[field], f"{field}: {src} -> {dst} 不是合法值"
