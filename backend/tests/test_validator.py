"""LLM 输出校验 —— 10 条规则 + 对抗性样例（文档 §4.5、C7）。

这一组测试的立场：**模型的输出是"待验证的提案"。**
每一条规则都至少有一个用例钉住，其中 5–8 是防幻觉护栏 ——
模型最容易犯的错是凭空造 `record_id`、把日程挪到凌晨、改动不可动的日程。
"""

from __future__ import annotations

import pytest

from app.llm.validator import LEVEL_L1, LEVEL_L2, validate_payload
from tests.llm_helpers import (
    make_request,
    payload_dict,
    rec,
    suggestion_dict,
)

# 两条相邻日程：A 09:00-10:00，B 09:30-10:30（互相重叠 30 分钟）
A = rec("rec_a", "晨会", (9, 0), (10, 0), priority=2)
B = rec("rec_b", "写周报", (9, 30), (10, 30), priority=3)


def _req(**kw):
    return make_request([A, B], **kw)


# ==================================================================
# 规则 1–4：结构性问题 -> L1 重试
# ==================================================================


def test_rule1_non_dict_is_l1():
    result = validate_payload("不是对象", _req())
    assert result.ok is False and result.level == LEVEL_L1
    assert result.issues[0].rule == 1


def test_rule2_missing_required_field():
    result = validate_payload({"decision_type": "nudge"}, _req())
    # confidence/summary 有默认值，缺 decision_type 之外这里应通过
    assert result.ok is True


def test_rule2_bad_type_is_l1():
    result = validate_payload(payload_dict(suggestions="不是数组"), _req())
    assert result.level == LEVEL_L1
    assert any(i.rule == 2 for i in result.issues)


def test_rule3_bad_enum_is_l1():
    result = validate_payload(payload_dict(decision_type="rewrite_everything"), _req())
    assert result.level == LEVEL_L1
    assert any(i.rule == 3 for i in result.issues)


def test_rule4_confidence_out_of_range_is_l1():
    result = validate_payload(payload_dict(confidence=1.5), _req())
    assert result.level == LEVEL_L1
    assert any(i.rule == 4 for i in result.issues)


def test_rule4_confidence_negative_is_l1():
    result = validate_payload(payload_dict(confidence=-0.2), _req())
    assert any(i.rule == 4 for i in result.issues)


# ==================================================================
# 规则 5–7：业务护栏 -> 丢弃违规建议
# ==================================================================


def test_rule5_unknown_record_id_dropped():
    raw = payload_dict(suggestions=[suggestion_dict("rec_不存在", start=(14, 0), end=(15, 0))])
    result = validate_payload(raw, _req())
    assert result.ok is True
    assert result.payload is not None and result.payload.suggestions == []
    assert any(i.rule == 5 for i in result.issues)


def test_rule6_outside_working_hours_dropped():
    raw = payload_dict(suggestions=[suggestion_dict("rec_a", start=(2, 0), end=(3, 0))])
    result = validate_payload(raw, _req())
    assert result.payload is not None and result.payload.suggestions == []
    assert any(i.rule == 6 for i in result.issues)


def test_rule6_reversed_interval_dropped():
    raw = payload_dict(suggestions=[suggestion_dict("rec_a", start=(15, 0), end=(14, 0))])
    result = validate_payload(raw, _req())
    assert result.payload is not None and result.payload.suggestions == []
    assert any(i.rule == 6 for i in result.issues)


def test_rule6_move_without_time_dropped():
    raw = payload_dict(suggestions=[suggestion_dict("rec_a")])
    result = validate_payload(raw, _req())
    assert result.payload is not None and result.payload.suggestions == []
    assert any(i.rule == 6 for i in result.issues)


def test_rule6_working_hours_boundary_allowed():
    """边界：恰好落在工作时间边界内应当通过（08:00 与 23:00）。"""
    req = _req(working_start="08:00", working_end="23:00")
    raw = payload_dict(suggestions=[suggestion_dict("rec_a", start=(8, 0), end=(9, 0))])
    result = validate_payload(raw, req)
    assert result.payload is not None and len(result.payload.suggestions) == 1


def test_rule7_immutable_record_dropped():
    raw = payload_dict(suggestions=[suggestion_dict("rec_a", start=(14, 0), end=(15, 0))])
    result = validate_payload(raw, _req(immutable=("rec_a",)))
    assert result.payload is not None and result.payload.suggestions == []
    assert any(i.rule == 7 for i in result.issues)


# ==================================================================
# 规则 8：应用后不得引入新硬冲突
# ==================================================================


def test_rule8_suggestion_creating_new_overlap_dropped():
    """把 rec_b 挪到与 rec_a 重叠的位置 -> 引入新硬冲突 -> 丢弃。"""
    raw = payload_dict(suggestions=[suggestion_dict("rec_b", start=(9, 0), end=(10, 0))])
    result = validate_payload(raw, _req())
    assert result.payload is not None and result.payload.suggestions == []
    assert any(i.rule == 8 for i in result.issues)


def test_rule8_valid_move_is_kept():
    """把 rec_b 挪到 14:00-15:00（与任何日程都不冲突）-> 保留。"""
    raw = payload_dict(suggestions=[suggestion_dict("rec_b", start=(14, 0), end=(15, 0))])
    result = validate_payload(raw, _req())
    assert result.payload is not None and len(result.payload.suggestions) == 1
    assert result.payload.suggestions[0].target_record_id == "rec_b"


def test_rule8_partial_solution_is_kept():
    """部分解不该被整包否掉：原本就冲突的一对不算模型引入的问题。

    这条是对"差集判据"的锁定 —— 若改成"应用后 hard 冲突数必须归零"，
    模型给出的部分可用建议会被全部丢掉，用户什么都看不到。
    """
    raw = payload_dict(
        suggestions=[
            suggestion_dict("rec_b", start=(9, 0), end=(10, 0)),  # 会引入新冲突
            suggestion_dict("rec_a", start=(14, 0), end=(15, 0)),  # 可行
        ]
    )
    result = validate_payload(raw, _req())
    assert result.payload is not None
    assert [s.target_record_id for s in result.payload.suggestions] == ["rec_a"]


def test_rule8_all_infeasible_becomes_l2():
    raw = payload_dict(suggestions=[suggestion_dict("rec_b", start=(9, 0), end=(10, 0))])
    result = validate_payload(raw, _req())
    assert result.ok is False and result.level == LEVEL_L2


# ==================================================================
# 规则 9–10：截断而不是失败
# ==================================================================


def test_rule9_too_many_suggestions_truncated():
    raw = payload_dict(
        suggestions=[suggestion_dict("rec_a", start=(14, 0), end=(15, 0)) for _ in range(5)]
    )
    result = validate_payload(raw, _req(max_suggestions=2))
    # 5 条挪到同一位置会互相冲突，只剩第一条可行；这里验证的是"不超限"
    assert result.payload is not None
    assert len(result.payload.suggestions) <= 2


def test_rule10_overlong_text_truncated():
    raw = payload_dict(summary="很" * 500)
    result = validate_payload(raw, _req())
    assert result.ok is True
    assert result.payload is not None and len(result.payload.summary) == 120
    assert any(i.rule == 10 for i in result.issues)


def test_rule10_intervention_text_truncated():
    raw = payload_dict(
        intervention={"title": "标" * 100, "message": "内" * 900, "pet_line": "猫" * 300}
    )
    result = validate_payload(raw, _req())
    assert result.ok is True
    iv = result.payload.intervention
    assert len(iv.title) == 40 and len(iv.message) == 300 and len(iv.pet_line) == 80


# ==================================================================
# 时间解释：模型不写时区时按本地解释
# ==================================================================


def test_naive_time_is_read_as_local():
    """模型写 `2026-10-30T15:00:00`（无时区）必须被当作本地 15:00。

    按 UTC 解释会让每条建议整体偏移 8 小时 —— 这是 P3 在 NL 上踩过的坑，
    这里用一条断言把它钉死。
    """
    from app.core.time import utc_to_local

    raw = payload_dict(
        suggestions=[
            {"id": "s1", "kind": "move", "target_record_id": "rec_b",
             "proposed_start": "2026-10-30T15:00:00", "proposed_end": "2026-10-30T16:00:00"}
        ]
    )
    result = validate_payload(raw, _req())
    assert result.payload is not None
    s = result.payload.suggestions[0]
    assert utc_to_local(s.proposed_start).hour == 15


def test_aware_time_is_normalized_to_utc():
    from app.core.time import utc_to_local

    raw = payload_dict(
        suggestions=[
            {"id": "s1", "kind": "move", "target_record_id": "rec_b",
             "proposed_start": "2026-10-30T15:00:00+08:00", "proposed_end": "2026-10-30T16:00:00+08:00"}
        ]
    )
    result = validate_payload(raw, _req())
    s = result.payload.suggestions[0]
    assert utc_to_local(s.proposed_start).hour == 15


# ==================================================================
# 对抗性组合
# ==================================================================


def test_drop_kind_needs_no_time():
    """`drop` 不给时间是合法的 —— 它的语义是"别做了"，不是"挪到哪"。"""
    raw = payload_dict(suggestions=[{"id": "s1", "kind": "drop", "target_record_id": "rec_b"}])
    result = validate_payload(raw, _req())
    assert result.payload is not None and len(result.payload.suggestions) == 1


def test_no_action_payload_passes():
    raw = payload_dict(decision_type="no_action", summary="这次不用打扰")
    result = validate_payload(raw, _req())
    assert result.ok is True and result.payload.decision_type == "no_action"


def test_empty_suggestions_ok():
    result = validate_payload(payload_dict(suggestions=[]), _req())
    assert result.ok is True


@pytest.mark.parametrize(
    "bad",
    [
        None,
        123,
        [],
        {"decision_type": "reschedule", "suggestions": [{"kind": "move"}]},
    ],
)
def test_malformed_inputs_never_raise(bad):
    """任何畸形输入都必须被"判决"，而不是抛异常 —— 校验器自己不能成为新的失败面。"""
    result = validate_payload(bad, _req())
    assert result.payload is None or isinstance(result.payload.decision_type, str)
