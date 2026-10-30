"""prompt 裁剪第一刀：`LLM_PROMPT_MAX_RECORDS` 40 → 14 + 相关度排序截断。

要钉住的行为只有一句话：**截掉的是"与这次决策无关"的条目，不是随机的 14 条。**

背景（docs/09 §6）：实测推理 token 随记录条数线性膨胀，9 条 905~2737；
40 条几乎必然撞顶 8192。压条数是手段，排序才是保住决策质量的关键 ——
按库序切前 14 条，可能恰好把正在冲突的两条切掉，模型连决策对象都看不到。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import get_args

from app.core.constants import LLM_PROMPT_MAX_RECORDS
from app.llm.contract import DecisionConflict, DecisionSignals, DecisionType
from app.llm.orchestrator import _rank_records, build_request
from tests.llm_helpers import NOW, at, rec


def orm_record(rid, title, start=None, end=None, *, status="pending", priority=3):
    """模拟 ORM Record：build_request 吃的是库对象（start_at/end_at），
    而不是契约对象（start/end）—— 这是它和 llm_helpers.rec 的关键区别。"""
    return SimpleNamespace(
        id=rid, type="schedule", title=title, status=status,
        start_at=start, end_at=end, priority=priority, tags=[],
    )


def _signals(conflicts=(), index=0.0):
    return SimpleNamespace(
        conflicts=list(conflicts), procrastination_index=index,
        load_ratio=0.0, idle_blocks=[], completion_rate_7d=0.8,
        overdue_count=0, avg_overdue_days=0.0, streak_days=0,
    )


def _day(n: int) -> list:
    """n 条日程，30 分钟一条从 8:00 排起 —— 30 条也排得下一天。"""
    return [
        orm_record(
            f"r{i:02d}", f"日程{i}",
            at(0, 8 + i // 2, 30 * (i % 2)), at(0, 8 + (i + 1) // 2, 30 * ((i + 1) % 2)),
        )
        for i in range(n)
    ]


# ==================================================================
# 截断上限
# ==================================================================


def test_cap_is_now_14():
    """常量本身改到 12~15 区间 —— 它是防撞顶的第一道闸，值得单独钉住。"""
    assert 12 <= LLM_PROMPT_MAX_RECORDS <= 15


def test_build_request_truncates_to_cap():
    records = _day(30)
    req = build_request(records, [], _signals(), None, trigger="manual", now=NOW)
    assert len(req.records) == LLM_PROMPT_MAX_RECORDS


# ==================================================================
# 排序：谁必须被留下
# ==================================================================


def test_conflicting_records_survive_even_if_last_in_db_order():
    """冲突的两条排在库序末尾，也必须在被截断后的 prompt 里。"""
    records = _day(20)
    # 冲突对放在库序最后，且时间也很远（第 3 天）—— 不看内容根本选不中它们
    records += [
        orm_record("rX", "撞车A", at(2, 10, 0), at(2, 11, 0)),
        orm_record("rY", "撞车B", at(2, 10, 30), at(2, 11, 30)),
    ]
    sig = _signals([DecisionConflict(
        record_ids=["rX", "rY"], kind="overlap", hard=True, overlap_minutes=30,
    )])
    req = build_request(records, [], sig, None, trigger="manual", now=NOW)
    ids = [r.id for r in req.records]
    assert "rX" in ids and "rY" in ids


def test_soft_conflict_ranks_below_hard():
    records = _day(12)  # 恰好不超上限，两条冲突对全保留，看相对顺序
    records += [
        orm_record("rHard1", "硬撞A", at(2, 10, 0), at(2, 11, 0)),
        orm_record("rHard2", "硬撞B", at(2, 10, 30), at(2, 11, 30)),
        orm_record("rSoft1", "软挤A", at(2, 14, 0), at(2, 15, 0)),
        orm_record("rSoft2", "软挤B", at(2, 13, 40), at(2, 14, 10)),
    ]
    sig = _signals([
        DecisionConflict(record_ids=["rHard1", "rHard2"], kind="overlap",
                         hard=True, overlap_minutes=30),
        DecisionConflict(record_ids=["rSoft1", "rSoft2"], kind="no_buffer",
                         hard=False, overlap_minutes=0),
    ])
    ranked = _rank_records(records, sig, NOW)
    ids = [str(r.id) for r in ranked]
    assert ids.index("rHard1") < ids.index("rSoft1")
    assert ids.index("rHard2") < ids.index("rSoft2")


def test_overdue_unfinished_outranks_far_future():
    """逾期未完成的条目（拖延干预的依据）比远期日程更有决策价值。"""
    records = [
        orm_record("rOverdue", "拖着没做", at(-1, 9, 0), at(-1, 10, 0)),
        orm_record("rFar", "下个月的事", at(20, 9, 0), at(20, 10, 0)),
    ]
    ranked = _rank_records(records, _signals(), NOW)
    assert [str(r.id) for r in ranked] == ["rOverdue", "rFar"]


def test_done_records_rank_last():
    done = orm_record("rDone", "已完成", at(0, 8, 0), at(0, 9, 0), status="done")
    pending_far = orm_record("rFar", "远期待办", at(20, 9, 0), at(20, 10, 0))
    ranked = _rank_records([done, pending_far], _signals(), NOW)
    assert [str(r.id) for r in ranked] == ["rFar", "rDone"]


def test_ranking_is_deterministic():
    records = _day(8) + [orm_record("rOverdue", "旧账", at(-2, 9, 0), at(-2, 10, 0))]
    first = _rank_records(records, _signals(), NOW)
    second = _rank_records(records, _signals(), NOW)
    assert [str(r.id) for r in first] == [str(r.id) for r in second]


def test_prompt_hints_truncation_to_model():
    """prompt 里"另有 N 条未列出"的提示依赖 req.records 的真实长度，
    截断发生在 build_request —— 渲染层拿到的就该已经是 14 条。"""
    records = _day(30)
    sig = _signals()
    req = build_request(records, [], sig, None, trigger="manual", now=NOW)
    from app.llm.prompts import render_user_prompt
    text = render_user_prompt(req)
    # 30 条被截到 14：prompt 不该谎称"另有 16 条"（渲染层不知道原始量）
    assert "另有" not in text
    assert text.count("id=r") == LLM_PROMPT_MAX_RECORDS
