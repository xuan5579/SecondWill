"""自然语言日程解析（规则层）。

## 定位

这是" AI 执行教练"里 **唯一允许触碰自然语言的地方**，也是 P3 / P4 的共同边界。
约束来自用户提出的两条硬要求：

1. **规则优先**：能用 regex 确定的绝不惊动模型；规则定不了才交给 LLM（P4）。
2. **不许硬编**：解析不出来的字段返回 None / 明确报错，绝不填静默默认值。

## 「不许硬编」具体怎么落地

旧版 `parse_nl_plan` 有 5 处静默兜底，这里逐一对 rewriting：

| 旧版行为 | 本模块做法 |
|---|---|
| 没写日期 → 默认今天 | 预填今天，但进 `assumptions` 标黄告知 |
| 只有一个时间点 → 默认 +60 分钟 | 结束时间进 `missing`，卡片留空让用户填 |
| 跨天 → 截断到 23:59 | 绝对 datetime 天然支持跨天，但要写明日期否则报错 |
| "3点开会" → 03:00 | 按字面理解，但进 `assumptions` 提示未说明上午/下午 |
| 重复语义只加 note | 不支持，直接判给 LLM 或报错 |

`missing` 与 `assumptions` 的分工是这套设计的核心：

- **missing** = 毫无依据，留空白给用户
- **assumptions** = 有约定俗成的解读，预填但必须告知

之所以要分开，是因为"不替用户做决定"和"好用"表面上是冲突的 ——
中间那一层**草稿确认**（constraint ②）正是把两者调和起来的开关：
推断不是不能做，而是必须可见、可改。

## 纯函数约定

`now` 一律由调用方注入，模块内部不读系统时钟。
这样单测可以脱离 Web 跑，也能精确构造跨零点、跨周等边界。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.core import constants as C
from app.core.time import local_to_utc, to_iso, utc_to_local
from app.schemas.naturallang import (
    NLAssumption,
    NLDraft,
    NLError,
    NLParseResult,
)

# ==================================================================
# 归一化
# ==================================================================

_CN_NUM_RE = re.compile(r"[零〇一二两三四五六七八九十]{1,4}(?=[月日号点时分])")
_FULL_TO_HALF_CLOCK = str.maketrans("：", ":")


def cn_to_int(s: str) -> int | None:
    """中文数字 -> int，支持 0-99。非法的返回 None（不猜）。"""
    if s.isdigit():
        return int(s)
    if "十" not in s:
        if len(s) != 1:
            return None
        return C.NL_CN_DIGITS.get(s)
    parts = s.split("十")
    if len(parts) != 2:
        return None
    head = 1 if parts[0] == "" else C.NL_CN_DIGITS.get(parts[0])
    tail = 0 if parts[1] == "" else C.NL_CN_DIGITS.get(parts[1])
    if head is None or tail is None or head == 0 or tail >= 10:
        return None
    return head * 10 + tail


def normalize(text: str) -> str:
    """全角转半角 → 口语词展开 → 中文数字转阿拉伯数字。"""
    out = unicodedata.normalize("NFKC", text or "").strip()
    out = out.translate(_FULL_TO_HALF_CLOCK)
    out = re.sub(r"\s+", " ", out)
    for spoken, expanded in C.NL_COLLOQUIAL_EXPANSIONS:
        out = out.replace(spoken, expanded)

    # 只替换紧邻时间/日期单位的位置，避免动到标题里的汉字数字
    def _repl(m: re.Match) -> str:
        n = cn_to_int(m.group(0))
        return str(n) if n is not None else m.group(0)

    return _CN_NUM_RE.sub(_repl, out)


# ==================================================================
# 匹配
# ==================================================================

_PERIODS = C.NL_PERIOD_PM + C.NL_PERIOD_AM + C.NL_PERIOD_NOON

_DATE_RE = re.compile(
    r"(?P<rel>大后天|后天|今天|今日|明天|明日)|"
    r"(?P<wk>(?:下下|下|本|这)?\s*周\s*[一二三四五六日天1-7])|"
    r"(?P<md>\d{1,2}\s*[月/.\-]\s*\d{1,2}\s*[日号]?)|"
    r"(?P<dom>\d{1,2}\s*[日号])"
)

_TIME_RE = re.compile(
    r"(?P<pd>" + "|".join(_PERIODS) + r")?\s*"
    r"(?P<h>\d{1,2})"
    r"(?:"
    r"\s*[:：]\s*(?P<m_colon>\d{1,2})"
    r"|\s*点\s*(?:(?P<m_cn>\d{1,2})\s*分(?:钟)?|(?P<alias>半|一刻|三刻))?"
    r"|\s*时\s*(?:(?P<m_shi>\d{1,2})\s*分(?:钟)?)?"
    r")"
)


@dataclass
class _Token:
    start: int
    end: int
    kind: str  # "date" | "time"
    payload: dict


def _resolve_relative(word: str) -> int | None:
    for offset, words in C.NL_RELATIVE_DAYS.items():
        if word in words:
            return offset
    return None


def _resolve_weekday(raw: str, today: date) -> tuple[date | None, str | None]:
    """返回 (日期, 修饰词)。按日历周（周一起算）解析。"""
    raw = raw.replace(" ", "")
    prefix = ""
    for p in ("下下", "下", "本", "这"):
        if raw.startswith(p):
            prefix = p
            break
    body = raw[len(prefix):].lstrip("周")
    idx = C.NL_WEEKDAYS.get(body)
    if idx is None:
        return None, prefix

    weekday = today.weekday()  # Monday=0
    if prefix == "下下":
        next_monday = today + timedelta(days=(7 - weekday) + 7)
        return next_monday + timedelta(days=idx), prefix
    if prefix == "下":
        next_monday = today + timedelta(days=(7 - weekday))
        return next_monday + timedelta(days=idx), prefix
    delta = (idx - weekday) % 7
    return today + timedelta(days=delta), prefix or "本"


def _collect_dates(text: str, today: date) -> tuple[list[_Token], NLError | None]:
    tokens: list[_Token] = []
    for m in _DATE_RE.finditer(text):
        kind, value, prefix = None, None, None
        if m.group("rel"):
            off = _resolve_relative(m.group("rel"))
            if off is None:
                continue
            kind, value = "relative", off
        elif m.group("wk"):
            day, prefix = _resolve_weekday(m.group("wk"), today)
            if day is None:
                continue
            kind, value = "week", day
        elif m.group("md"):
            nums = re.findall(r"\d{1,2}", m.group("md"))
            if len(nums) != 2:
                continue
            month, dom = int(nums[0]), int(nums[1])
            if not (1 <= month <= 12 and 1 <= dom <= 31):
                continue
            try:
                value = date(today.year, month, dom)
            except ValueError:
                return tokens, NLError(
                    code="NXC-D1",
                    message=f"{month}月{dom}日不是合法日期",
                    hint="请检查月份与日期",
                )
            kind = "exact"
        elif m.group("dom"):
            dom = int(re.match(r"\d{1,2}", m.group("dom")).group(0))
            if not 1 <= dom <= 31:
                continue
            # 「15号」按最近的未来日期解读 —— 约定俗成，但会告知
            candidates: list[date] = []
            for shift in (0, 1, 2):
                year, month = today.year, today.month + shift
                if month > 12:
                    year, month = year + 1, month - 12
                try:
                    candidates.append(date(year, month, dom))
                except ValueError:
                    return tokens, NLError(
                        code="NXC-D1",
                        message=f"{dom}号不是合法日期",
                        hint="请写明具体的月日",
                    )
            value = next((c for c in candidates if c >= today), candidates[-1])
            kind = "dom"
        tokens.append(_Token(m.start(), m.end(), "date", {"kind": kind, "value": value, "prefix": prefix}))
    return tokens, None


def _apply_period(hour: int, period: str | None) -> int:
    if period in C.NL_PERIOD_PM:
        if hour < 12:
            hour += 12
        elif hour == 12:
            hour = 0  # 「晚上12点」= 次日 00:00 的常见读法
    elif period in C.NL_PERIOD_AM and hour == 12:
        hour = 0
    return 0 if hour == 24 else hour


def _collect_times(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    for m in _TIME_RE.finditer(text):
        hour = int(m.group("h"))
        minute_raw = m.group("m_colon") or m.group("m_cn") or m.group("m_shi")
        if minute_raw is not None:
            minute = int(minute_raw)
        elif m.group("alias"):
            minute = C.NL_MINUTE_ALIASES[m.group("alias")]
        else:
            minute = 0
        if hour > 24 or minute > 59:
            continue  # 明显荒谬，不当时间用，留给残差检查去判
        period = m.group("pd")
        hour = _apply_period(hour, period)
        tokens.append(
            _Token(
                m.start(),
                m.end(),
                "time",
                {
                    "hour": hour,
                    "hour_raw": int(m.group("h")),
                    "minute": minute,
                    "period": period,
                },
            )
        )
    return tokens


# ==================================================================
# 主入口
# ==================================================================

_TITLE_STRIP = " \t\r\n,，。、;；:：!！?？~～-—_()（）[]【】<>《》\"'“”‘’/\\|+*#"


def parse_nl(text: str, now: datetime | None = None) -> NLParseResult:
    """把一句话解析成草稿。**纯函数**：`now` 由调用方注入（UTC naive）。"""
    now_utc = now or datetime.utcnow()
    today_local = utc_to_local(now_utc).date()
    normalized = normalize(text)

    # ---------- 前置拦截：规则明确放弃的句式 ----------
    if len(normalized) > C.NL_TEXT_MAX_LEN:
        return _need_llm(normalized, NLDraft(), "句子过长，超出规则层的可靠处理范围")

    for marker in C.NL_FUZZY_MARKERS:
        if marker in normalized:
            return _need_llm(normalized, NLDraft(), f"『{marker}』没有确定的日期含义")

    for marker in C.NL_MULTI_CLAUSE_MARKERS:
        if marker in normalized:
            return _need_llm(normalized, NLDraft(), "一句话里有多个动作，无法确定主线")

    # ---------- 时间、日期提取 ----------
    times = _collect_times(normalized)
    dates, date_err = _collect_dates(normalized, today_local)
    if date_err:
        return NLParseResult(status="error", source="rule", error=date_err, text=normalized)

    if len(times) > C.NL_TIME_TOKEN_MAX:
        return NLParseResult(
            status="error",
            source="rule",
            text=normalized,
            error=NLError(
                code="NXC-T1",
                message=f"发现 {len(times)} 个时间点，无法确定哪两个构成起止",
                hint="请写成『开始时间到结束时间』两个点",
            ),
        )

    priority = C.NL_PRIORITY_MID
    ptheta = 0  # 命中的优先级词文本，用于从标题里抹掉
    for words, value in (
        (C.NL_PRIORITY_LOW_WORDS, C.NL_PRIORITY_LOW),
        (C.NL_PRIORITY_MID_WORDS, C.NL_PRIORITY_MID),
        (C.NL_PRIORITY_HIGH_WORDS, C.NL_PRIORITY_HIGH),
    ):
        for w in words:
            if w in normalized:
                priority, ptheta = value, max(ptheta, len(w))
                break
        if ptheta:
            break

    # ---------- 为时间片段定位日期 ----------
    # 每个时间点取"最近的前置日期词"；没有则退到句首日期；再没有则默认今天。
    dated: list[tuple[_Token, date, str]] = []
    for t in times:
        preceding = [d for d in dates if d.end <= t.start]
        if preceding:
            chosen = max(preceding, key=lambda d: d.end)
            day, source = _token_date(chosen.payload, today_local), "preceding"
        elif dates:
            chosen = dates[0]
            day, source = _token_date(chosen.payload, today_local), "fallback"
        else:
            day, source = today_local, "default"
        dated.append((t, day, source))

    # ---------- 标题（掩码后残余） ----------
    title = _extract_title(normalized, times, dates)

    draft = NLDraft(title=title or None, priority=priority)
    missing: list[str] = []
    assumptions: list[NLAssumption] = []
    confidence = 1.0

    if not title:
        missing.append("title")
        confidence -= 0.3

    if not dates and times:
        # 有时间没写日期 —— 预填今天，但必须告知
        assumptions.append(NLAssumption(code="date_default_today", label=C.NL_ASSUME_NO_DATE, field="start_at"))
        confidence -= 0.1
    if any(d.payload.get("prefix") in ("下", "下下") for d in dates):
        assumptions.append(NLAssumption(code="week_semantics", label=C.NL_ASSUME_WEEK, field="start_at"))
        confidence -= 0.1
    if any(d.payload.get("kind") == "dom" for d in dates):
        dom_token = next(d for d in dates if d.payload.get("kind") == "dom")
        assumptions.append(
            NLAssumption(
                code="dom_semantics",
                label=C.NL_ASSUME_DOM.format(dom_token.payload["value"].day),
                field="start_at",
            )
        )
        confidence -= 0.1

    # ---------- 时段继承 ----------
    # 「下午3点到4点半」里的第二个时间点没有写时段，但按中文惯例它继承前面的"下午"。
    # 这不是猜测，是语言约定；不过仍然要告知用户（assumptions）。
    #
    # 关键限制：**仅当不继承会导致自相矛盾（结束 ≤ 开始）时才继承**。
    # 若按字面理解本来就是合理的，绝不擅自改写。
    if len(dated) == 2:
        p1, p2 = dated[0][0].payload, dated[1][0].payload
        if p2["period"] is None and p1["period"] is not None:
            start_dt = datetime.combine(dated[0][1], time(p1["hour"], p1["minute"]))
            end_plain = datetime.combine(dated[1][1], time(p2["hour"], p2["minute"]))
            end_inherited = datetime.combine(
                dated[1][1], time(_apply_period(p2["hour_raw"], p1["period"]), p2["minute"])
            )
            if end_plain <= start_dt < end_inherited:
                p2["hour"] = _apply_period(p2["hour_raw"], p1["period"])
                p2["period"] = p1["period"]  # 已继承，不再算作"未说明时段"
                assumptions.append(
                    NLAssumption(
                        code="period_inherited",
                        label=f"结束时间按『{p1['period']}』理解",
                        field="end_at",
                    )
                )

    # 「未说明上午/下午」只在**整句都没有时段词**时才提示。
    # 「下午3点到4点半」的第二个点已继承时段，不该再报 —— 那只是噪音。
    if not any(t.payload["period"] for t in times) and any(
        t.payload["hour"] <= 11 for t, _, _ in dated
    ):
        assumptions.append(
            NLAssumption(code="no_period", label=C.NL_ASSUME_NO_PERIOD, field="start_at")
        )
        confidence -= 0.1

    # ---------- 组装起止时间 ----------
    if len(dated) >= 1:
        start_local = _combine(dated[0])
        if _out_of_range(start_local, today_local):
            return NLParseResult(
                status="error", source="rule", draft=draft, text=normalized,
                error=NLError(code="NXC-3", message="开始时间不在合理范围内",
                              hint=f"只接受 {C.NL_MIN_YEAR} 年之后、{today_local.year + C.NL_MAX_YEAR_OFFSET} 年之前"),
            )
        if start_local is None:
            return NLParseResult(
                status="error", source="rule", draft=draft, text=normalized,
                error=NLError(code="NXC-T2", message="时间点不合法", hint="请写成『9点』『9点30分』『09:30』"),
            )
        # 注意：这一步不能省。draft 里的字段是**本地**日历概念，
        # 而 to_iso 约定入参为 UTC naive —— 缺了 local_to_utc 会整体偏移一个时区。
        draft.start_at = to_iso(local_to_utc(start_local))

    if len(dated) >= 2:
        end_local = _combine(dated[1])
        if end_local is None:
            return NLParseResult(
                status="error", source="rule", draft=draft, text=normalized,
                error=NLError(code="NXC-T2", message="结束时间点不合法", hint="请写成『9点』『9点30分』"),
            )
        start_local_dt = datetime.combine(dated[0][1], time(dated[0][0].payload["hour"], dated[0][0].payload["minute"]))
        if end_local <= start_local_dt:
            return NLParseResult(
                status="error", source="rule", draft=draft, text=normalized,
                error=NLError(
                    code="NXC-1",
                    message="结束时间不晚于开始时间",
                    hint="若跨天，请把结束日期写清楚，例如『今天晚上11点到明天凌晨1点』",
                ),
            )
        if (end_local - start_local_dt) > timedelta(minutes=C.NL_MAX_SPAN_MINUTES):
            return NLParseResult(
                status="error", source="rule", draft=draft, text=normalized,
                error=NLError(code="NXC-2", message="时间跨度超过 24 小时", hint="请把长任务拆成多段"),
            )
        if _out_of_range(end_local, today_local):
            return NLParseResult(
                status="error", source="rule", draft=draft, text=normalized,
                error=NLError(code="NXC-3", message="结束时间不在合理范围内", hint="请检查日期"),
            )
        # 跨天必须自带明确日期
        if dated[1][1] != dated[0][1] and dated[1][2] != "preceding":
            return NLParseResult(
                status="error", source="rule", draft=draft, text=normalized,
                error=NLError(
                    code="NXC-2",
                    message="跨天日程的结束日期不明确",
                    hint="请写明结束日期，例如『晚上11点到明天凌晨1点』",
                ),
            )
        if dated[1][1] != dated[0][1]:
            assumptions.append(NLAssumption(code="cross_day", label=C.NL_ASSUME_CROSS_DAY, field="end_at"))
        draft.end_at = to_iso(local_to_utc(end_local))
    elif times:
        # 只有一个时间点 —— 结束时间留空，绝不默认 +1 小时
        missing.append("end_at")
        confidence -= 0.15
    elif not times:
        missing.extend(["start_at", "end_at"])
        confidence -= 0.15

    status: str = "need_user" if missing else "resolved"
    return NLParseResult(
        status=status,
        source="rule",
        draft=draft,
        missing=missing,
        assumptions=assumptions,
        confidence=round(max(0.0, min(1.0, confidence)), 2),
        text=normalized,
    )


# ==================================================================
# 内部工具
# ==================================================================


def _token_date(payload: dict, today: date) -> date:
    kind = payload["kind"]
    if kind == "relative":
        return today + timedelta(days=payload["value"])
    return payload["value"]  # week / exact / dom 都已解析成具体日期


def _combine(item: tuple[_Token, date, str]) -> datetime | None:
    token, day, _ = item
    h, mi = token.payload["hour"], token.payload["minute"]
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    try:
        return datetime.combine(day, time(h, mi))
    except ValueError:
        return None


def _out_of_range(dt: datetime, today: date) -> bool:
    return not (C.NL_MIN_YEAR <= dt.year <= today.year + C.NL_MAX_YEAR_OFFSET)


def _extract_title(text: str, times: list[_Token], dates: list[_Token]) -> str:
    """抹掉所有时间/日期成分，剩下的就是标题。"""
    chars = list(text)
    for tok in dates + times:
        for i in range(tok.start, tok.end):
            chars[i] = "\x00"

    # 两点之间的连接词也要抹掉：「9点到11点」里的"到"
    if len(times) >= 2:
        lo, hi = times[0].end, times[1].start
        segment = text[lo:hi]
        for sep in C.NL_RANGE_SEPARATORS:
            pos = segment.find(sep)
            while pos != -1:
                for i in range(lo + pos, lo + pos + len(sep)):
                    chars[i] = "\x00"
                pos = segment.find(sep, pos + len(sep))

    for word in C.NL_PRIORITY_HIGH_WORDS + C.NL_PRIORITY_MID_WORDS + C.NL_PRIORITY_LOW_WORDS:
        pos = text.find(word)
        while pos != -1:
            for i in range(pos, pos + len(word)):
                chars[i] = "\x00"
            pos = text.find(word, pos + len(word))

    out = "".join(ch for ch in chars if ch != "\x00")
    out = out.strip(_TITLE_STRIP)
    out = re.sub(r"\s+", " ", out).strip()

    for verb in sorted(C.NL_LEADING_VERBS, key=len, reverse=True):
        if out.startswith(verb) and len(out) > len(verb):
            out = out[len(verb):].strip(_TITLE_STRIP)
            break
    for word in sorted(C.NL_TRAILING_WORDS, key=len, reverse=True):
        if out.endswith(word) and len(out) > len(word):
            out = out[: -len(word)].strip(_TITLE_STRIP)
    return out.strip(_TITLE_STRIP).strip()


def _need_llm(text: str, draft: NLDraft, reason: str) -> NLParseResult:
    """规则层放弃时的降级。

    **P4 之前**没有 LLM 可调用，因此如实降级为 error ——
    宁可让用户看到"这句我解析不了"，也不要猜一个可能错的结果塞进卡片。
    P4 接上 adapter 后，这里会变成真正的模型调用，返回结构完全不变。
    """
    return NLParseResult(
        status="error",
        source="rule",
        draft=draft,
        text=text,
        error=NLError(
            code="NXC-NEED-LLM",
            message=f"规则层无法可靠解析：{reason}",
            hint="请换用《支持的例句清单》里的写法；该功能将在接入模型后自动扩展",
        ),
    )
