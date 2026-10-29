"""自然语言解析：支持清单、防硬编护栏、契约稳定性。

这个文件承载三个职责：

1. **锁定《确定支持的例句清单》** —— 每一条都是表格驱动断言，
   清单外的写法不承诺支持。这是用户提出的要求 ② 的可执行形态。
2. **锁死「不许硬编」** —— 旧版那 5 处静默兜底，每一条都有一个测试盯着，
   谁要是再加回默认值，测试会红。
3. **锁住对外契约** —— P4 接 LLM 时字段必须完全不变，这里是护栏。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import constants as C  # noqa: E402
from app.core.time import local_to_utc, to_iso, utc_to_local  # noqa: E402
from app.schemas.naturallang import NLParseResult  # noqa: E402
from app.services.naturallang import cn_to_int, normalize, parse_nl  # noqa: E402
from app.services.nl_spec import SUPPORTED_EXAMPLES, UNSUPPORTED_EXAMPLES  # noqa: E402

TODAY = date(2026, 10, 29)  # 周四
NOW = local_to_utc(datetime.combine(TODAY, time(10, 0)))


def L(off: int, hour: int, minute: int = 0) -> str:
    """按本地日历构造 ISO（相对今天的第 off 天）。"""
    return to_iso(local_to_utc(datetime.combine(TODAY + timedelta(days=off), time(hour, minute))))


def at(iso: str | None) -> str:
    """把 ISO 转回本地可读串，便于断言与报错。"""
    return utc_to_local(datetime.fromisoformat(iso)).strftime("%m-%d %H:%M") if iso else None


# ==================================================================
# 1. 确定支持的例句清单（数据源：services/nl_spec.py）
# ==================================================================


def _build_table() -> list[tuple]:
    """把相对量清单换算成针对固定基准日的绝对期望值。"""
    rows = []
    for ex in SUPPORTED_EXAMPLES:
        s_off, s_h, s_m = ex.start_tuple(TODAY)
        start = L(s_off, s_h, s_m)
        end = None
        if ex.end:
            e_off, e_h, e_m = ex.end
            end = L(e_off, e_h, e_m)
        rows.append((ex.text, ex.title, start, end, ex.priority, ex.status))
    return rows


SUPPORTED = _build_table()


@pytest.mark.parametrize("text,title,start,end,prio,status", SUPPORTED)
def test_supported_sentences(text, title, start, end, prio, status):
    """清单里的句子必须解析出完全一致的结果 —— 演示现场靠的就是这些。"""
    r = parse_nl(text, NOW)
    assert r.status == status, f"{text}: status={r.status} error={r.error}"
    assert r.draft.title == title, f"{text}: title={r.draft.title!r}"
    assert r.draft.start_at == start, f"{text}: start={at(r.draft.start_at)}"
    assert r.draft.end_at == end, f"{text}: end={at(r.draft.end_at)}"
    assert r.draft.priority == prio, f"{text}: priority={r.draft.priority}"


def test_supported_list_has_enough_coverage():
    """清单至少 10 条，且必须覆盖跨零点 —— 那是旧版彻底失效的场景。"""
    assert len(SUPPORTED) >= 10
    assert any("凌晨" in ex.text for ex in SUPPORTED_EXAMPLES)


# ==================================================================
# 2. 旧版 5 处静默硬编的护栏
# ==================================================================


class TestNoSilentDefaults:
    """旧版 `parse_nl_plan` 的每一次"贴心兜底"，这里都有一发子弹等着。"""

    def test_no_end_time_is_left_blank_not_plus_60min(self):
        """旧版：只有一个时间点 → 默认 +60 分钟。新版：留空 + missing。"""
        r = parse_nl("明天早上7点跑步", NOW)
        assert r.draft.start_at == L(1, 7)
        assert r.draft.end_at is None, "结束时间不该被默认填充"
        assert "end_at" in r.missing
        assert r.status == "need_user"

    def test_missing_date_is_disclosed_not_silent(self):
        """旧版：没写日期 → 静默认今天。新版：预填但可以追溯。"""
        r = parse_nl("下午3点提交周报", NOW)
        assert r.draft.start_at == L(0, 15)
        codes = [a.code for a in r.assumptions]
        assert "date_default_today" in codes, "必须有显式披露"

    def test_cross_midnight_is_not_truncated_to_2359(self):
        """旧版：end>23:59 → 静默截断。新版：跨天必须写清日期，否则报错。

        这条同时覆盖了 P2 修掉的同一个根因（用当日分钟数表示时间）。
        """
        r = parse_nl("今天晚上11点到明天凌晨1点值班", NOW)
        assert r.status == "resolved"
        assert at(r.draft.start_at) == "10-29 23:00"
        assert at(r.draft.end_at) == "10-30 01:00", "跨零点必须是次日 01:00，不是 23:59"

    def test_cross_day_without_explicit_date_is_rejected(self):
        """跨天但没写结束日期 —— 不能猜，必须拦下并告诉用户怎么写。"""
        r = parse_nl("今天晚上11点到1点值班", NOW)
        assert r.status == "error"
        assert r.error is not None
        assert r.error.code in {"NXC-1", "NXC-2"}
        assert r.error.hint is not None and "明天" in r.error.hint

    def test_no_period_word_is_disclosed(self):
        """旧版：『3点开会』→ 03:00 且不吭声。新版：按字面理解但告知。"""
        r = parse_nl("明天9:00到10:30写项目方案", NOW)
        assert "no_period" in [a.code for a in r.assumptions]
        assert r.draft.start_at == L(1, 9)

    def test_reverse_time_range_raises_error(self):
        r = parse_nl("明天上午9点到8点开会", NOW)
        assert r.status == "error"
        assert r.error.code == "NXC-1"

    def test_three_timepoints_is_error_not_guess(self):
        """旧版会随便挑两个；新版明确报错。"""
        r = parse_nl("明天9点10点11点各开一个会", NOW)
        assert r.status == "error"
        assert r.error.code == "NXC-T1"

    def test_fuzzy_date_words_degrade_instead_of_guessing(self):
        r = parse_nl("月底前交季度报告", NOW)
        assert r.status == "error"
        assert r.error.code == "NXC-NEED-LLM"
        assert r.error.hint is not None

    def test_multi_clause_sentence_is_not_truncated(self):
        """旧版会把多件事砍成一件事；新版直接放弃给 LLM。"""
        r = parse_nl("明天上午10点开会然后下午3点写文档", NOW)
        assert r.status == "error"
        assert "多个动作" in (r.error.message or "")

    def test_overlong_sentence_is_degraded(self):
        # 刻意不含多子句标记：这里要验的是长度阈值本身，而不是复杂度检测
        text = "明天上午十点到十二点讨论" + "产品架构演进" * 9
        assert len(text) > C.NL_TEXT_MAX_LEN
        r = parse_nl(text, NOW)
        assert r.status == "error"
        assert r.error.code == "NXC-NEED-LLM"


# ==================================================================
# 3. 确定性 / 纯函数
# ==================================================================


class TestPurity:
    def test_same_input_same_output(self):
        a = parse_nl("明天下午3点到5点开会", NOW)
        b = parse_nl("明天下午3点到5点开会", NOW)
        assert a.model_dump() == b.model_dump()

    def test_now_is_injected_not_read_from_clock(self):
        """同一句话在不同基准日解析出的本地时刻必须一致（只差日期）。"""
        later = local_to_utc(datetime.combine(TODAY + timedelta(days=10), time(10, 0)))
        r1 = parse_nl("明天下午3点到5点开会", NOW)
        r2 = parse_nl("明天下午3点到5点开会", later)
        assert at(r1.draft.start_at) == "10-30 15:00"
        assert at(r2.draft.start_at) == "11-09 15:00"

    def test_normalize_expands_colloquial_and_cn_numerals(self):
        assert normalize("今晚8点") == "今天晚上8点"
        assert normalize("三点半") == "3点半"
        assert normalize("九点十五分") == "9点15分"
        assert normalize("十五号") == "15号"

    @pytest.mark.parametrize(
        "word,expected", [("十", 10), ("十五", 15), ("二十", 20), ("二十三", 23), ("三", 3)]
    )
    def test_cn_to_int(self, word, expected):
        assert cn_to_int(word) == expected

    def test_cn_to_int_rejects_garbage(self):
        assert cn_to_int("") is None
        assert cn_to_int("十七八") is None


# ==================================================================
# 4. 契约稳定性（P4 接 LLM 时不许变）
# ==================================================================


class TestContractStability:
    def test_result_fields_are_stable(self):
        r = parse_nl("明天下午3点到5点开会", NOW)
        assert set(NLParseResult.model_fields) == {
            "status", "source", "draft", "missing", "assumptions", "error",
            "confidence", "text",
        }
        keys = set(r.draft.model_dump())
        assert keys == {"title", "start_at", "end_at", "priority"}, (
            "P4 接 LLM 时 draft 字段必须与 P3 完全一致 —— 前端卡片因此不用重写"
        )

    def test_source_is_rule_in_p3(self):
        r = parse_nl("明天下午3点到5点开会", NOW)
        assert r.source == "rule"

    def test_error_always_carries_hint(self):
        """报错是为了让用户知道怎么改 —— 只给 message 不给 hint 属于半成品。"""
        for bad, _reason in UNSUPPORTED_EXAMPLES:
            r = parse_nl(bad, NOW)
            assert r.status == "error", f"{bad} 应当报错"
            assert r.error is not None and r.error.hint, f"{bad} 缺少 hint"

    def test_resolved_never_has_error(self):
        for text, *_ in SUPPORTED:
            r = parse_nl(text, NOW)
            if r.status == "resolved":
                assert r.error is None
