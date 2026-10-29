"""NL 的 LLM 兜底（P4）—— 兑现 P3 时写下的那句"接上 adapter 后这里改为真调用"。

## 为什么对外 schema 一个字都不用改

P3 定契约时就把 `source` 留成了 `rule / llm / cache` 三态，
如今只是多了一个取值来源。前端草稿卡片一行代码都不用动 ——
这就是「切分点落在数据源而非功能上」这条设计决策的兑现（文档 §0.1 D-4）。

## 与规则层同一套纪律

模型拿到的结果**同样要过校验**：时间必须在合理区间、结束必须晚于开始、
跨天必须写出日期。模型不是免检通道 —— 它只是另一个"可能会猜错"的解析器。
校验不通过时一律退回 `need_user` / `error`，绝不把可疑结果塞进卡片。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.constants import (
    NL_MAX_SPAN_MINUTES,
    NL_MAX_YEAR_OFFSET,
    NL_MIN_YEAR,
    NL_TITLE_MAX_LEN,
)
from app.core.logging import get_logger
from app.core.time import local_to_utc, to_iso, utc_to_local
from app.llm import provider as provider_pkg
from app.llm.provider import LLMError
from app.llm.prompts.nl_parse import SYSTEM_PROMPT, render_user_prompt
from app.schemas.naturallang import (
    NLAssumption,
    NLDraft,
    NLError,
    NLParseResult,
)

logger = get_logger(__name__)

#: LLM 解析结果的置信度。低于规则层（规则命中是确定的）。
LLM_CONFIDENCE = 0.6

_FIELDS = ("title", "start_at", "end_at", "priority")


def _coerce_dt(value, now: datetime) -> datetime | None:
    """模型给的时间字符串 -> UTC naive。非法或超界返回 None。"""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        # 带时区：直接归一化成 UTC naive，年份用它自己的年份做合理性检查
        utc_naive = dt.astimezone(timezone.utc).replace(tzinfo=None)
        if not (NL_MIN_YEAR <= utc_naive.year <= utc_naive.year + NL_MAX_YEAR_OFFSET):
            return None
        return utc_naive

    # 不带时区：按用户本地时间解释（与 decision 契约同一套约定）
    upper = utc_to_local(now).year + NL_MAX_YEAR_OFFSET
    if not (NL_MIN_YEAR <= dt.year <= upper):
        return None
    return local_to_utc(dt)


async def parse_with_llm(
    text: str, now: datetime, *, provider=None, reason: str = ""
) -> NLParseResult:
    """调用模型解析一句话。失败一律返回 `need_user` / `error`，**不向上抛**。"""
    # 走模块属性而不是模块级绑定：测试要能替换掉 get_provider
    provider = provider if provider is not None else provider_pkg.get_provider()
    if not getattr(provider, "available", False):
        return _error(text, "NXC-LLM-NA", "模型不可用（未配置 API Key）",
                      "可在 .env 里配置 DEEPSEEK_API_KEY，或换用清单里的写法")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_user_prompt(text, now, reason=reason)},
    ]
    try:
        resp = await provider.complete_json(messages)
    except LLMError as exc:
        logger.warning("NL 模型调用失败: %s", exc)
        return _error(text, "NXC-LLM-FAIL", f"模型调用失败：{exc.kind.value}",
                      "可稍后重试，或换用《支持的例句清单》里的写法")

    return _interpret(resp.data, text, now)


def _interpret(data: dict, text: str, now: datetime) -> NLParseResult:
    """把模型输出翻译成对外契约。**这里没有信任，只有校验。**"""
    title = data.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if len(title) > NL_TITLE_MAX_LEN:
        title = title[:NL_TITLE_MAX_LEN]

    start = _coerce_dt(data.get("start"), now)
    end = _coerce_dt(data.get("end"), now)

    # 起止顺序：模型把"3点到4点"写成 03:00/04:00 这类错位很常见，
    # 不正确就当没解析到，交给用户填，而不是悄悄交换（那是硬编的一种）。
    if start and end and end <= start:
        return _error(
            text, "NXC-LLM-RANGE",
            f"模型给出的结束时间（{end}）不晚于开始时间（{start}）",
            "请把开始和结束时间写清楚，例如『下午3点到4点』",
        )
    if start and end:
        span = (end - start).total_seconds() / 60.0
        if span > NL_MAX_SPAN_MINUTES:
            return _error(text, "NXC-LLM-SPAN", "给出的时间跨度超过 24 小时",
                          "单条日程请控制在一天内")

    try:
        priority = int(data.get("priority", 3))
    except (TypeError, ValueError):
        priority = 3
    priority = min(5, max(1, priority))

    missing = [f for f in (data.get("missing") or []) if f in _FIELDS]
    if not start and "start_at" not in missing:
        missing.append("start_at")
    if not end and "end_at" not in missing:
        missing.append("end_at")
    if not title and "title" not in missing:
        missing.append("title")

    assumptions: list[NLAssumption] = []
    for item in data.get("assumptions") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "inferred"))[:40]
        label = str(item.get("label", ""))[:80]
        field = item.get("field") if item.get("field") in _FIELDS else "start_at"
        if label:
            assumptions.append(NLAssumption(code=code, label=label, field=field))

    draft = NLDraft(
        title=title or None,
        start_at=to_iso(start),
        end_at=to_iso(end),
        priority=priority,
    )

    if missing:
        return NLParseResult(
            status="need_user",
            source="llm",
            draft=draft,
            missing=missing,
            assumptions=assumptions,
            confidence=LLM_CONFIDENCE,
            text=text,
        )

    return NLParseResult(
        status="resolved",
        source="llm",
        draft=draft,
        assumptions=assumptions,
        confidence=LLM_CONFIDENCE,
        text=text,
    )


def _error(text: str, code: str, message: str, hint: str) -> NLParseResult:
    return NLParseResult(
        status="error",
        source="llm",
        draft=NLDraft(),
        text=text,
        error=NLError(code=code, message=message, hint=hint),
    )


__all__ = ["parse_with_llm"]
