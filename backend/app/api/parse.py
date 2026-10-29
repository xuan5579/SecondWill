"""自然语言解析端点。

**这个端点只读：它绝不写数据库。**
入库必须走 `POST /api/records`（草稿卡片确认后由前端调用），这样才能自动落到
既有的冲突检测与宠物状态更新链路上 —— constraint ④「不另起一条链路」的落点。

## DEMO_MODE 为什么要做"相对化"

常规录制回放直接存绝对值：今天录下"明天上午9点"= 2026-10-30 09:00，
三天后回放就变成一条**过期**日程，演示现场直接穿帮。

所以这里存的是**相对量**：`day_offset`（距基准日的天数）+ `minute`（当日分钟数）。
回放时按当时的 `now` 重新算成绝对时间 —— fixture 因此是"常青"的，
无论哪天演示，"明天上午9点"永远是明天上午9点。
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date as date_cls, datetime, time, timedelta, timezone

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.core.logging import get_logger
from app.core.time import local_to_utc, parse_iso, to_iso, utc_to_local
from app.llm.cache import DemoRecorder, get_recorder
from app.schemas.naturallang import NLParseRequest, NLParseResult
from app.services.naturallang import _DATE_RE, _TIME_RE, parse_nl

logger = get_logger(__name__)
router = APIRouter(prefix="/parse", tags=["parse"])

#: 指纹只取原文，刻意**不含** now —— 否则每次请求指纹都不同，回放永远命中不了
_FIXTURE_KIND = "nl_parse"

#: 模型解析的结果单独存一类。
#: 与规则结果分开是必须的：同一句话在两期里可能分别有录制，
#: 混在一个 key 空间里会出现"P3 录的 error 结果挡住了 P4 的模型结果"。
_FIXTURE_KIND_LLM = "nl_parse_llm"


def _detect_absolute_date(text: str, before_pos: int | None = None) -> tuple[int, int] | None:
    """取 `before_pos` 之前最近的一个「X月X日」绝对日期。

    有 → 回放时按"月+日"还原（今年，已过则顺延到下一年）。
    否则三天后回放会把 11月3日 变成 11月6日。

    `before_pos` 用于跨天句：「11月20日23:30到11月21日1:00」的起点和终点
    各自属于不同的绝对日期，取"整句第一个"会错。
    """
    best: tuple[int, int] | None = None
    for m in _DATE_RE.finditer(text):
        if before_pos is not None and m.start() > before_pos:
            break
        if not m.group("md"):
            continue
        nums = re.findall(r"\d{1,2}", m.group("md"))
        if len(nums) == 2:
            month, day = int(nums[0]), int(nums[1])
            if 1 <= month <= 12 and 1 <= day <= 31:
                best = (month, day)
    return best


def _encode(iso: str | None, today, absolute: tuple[int, int] | None) -> dict | None:
    """把 ISO 时刻编码成 fixture 里的可回放形式。"""
    if not iso:
        return None
    if absolute:
        month, day = absolute
        return {"type": "absolute", "month": month, "day": day,
                "minute": _minute_of_day(iso)}
    local = utc_to_local(parse_iso(iso))
    return {
        "type": "relative",
        "day_offset": (local.date() - today).days,
        "minute": local.hour * 60 + local.minute,
    }


def _minute_of_day(iso: str) -> int:
    local = utc_to_local(parse_iso(iso))
    return local.hour * 60 + local.minute


def _decode(rel: dict | None, today) -> str | None:
    if not rel:
        return None
    minute = int(rel["minute"])
    if rel.get("type") == "absolute":
        month, day = int(rel["month"]), int(rel["day"])
        target = _next_occurrence(today, month, day)
    else:
        target = today + timedelta(days=int(rel["day_offset"]))
    return to_iso(local_to_utc(datetime.combine(target, time(minute // 60, minute % 60))))


def _next_occurrence(today, month: int, day: int):
    """今年的这个日期若已过去，顺延到下一年 —— 保证演示时永远在未来。"""
    import calendar

    max_day = calendar.monthrange(today.year, month)[1]
    try:
        target = date_cls(today.year, month, min(day, max_day))
    except ValueError:
        target = date_cls(today.year, month, max_day)
    if target < today:
        max_day = calendar.monthrange(today.year + 1, month)[1]
        target = date_cls(today.year + 1, month, min(day, max_day))
    return target


def _to_recordable(result: NLParseResult, today) -> dict:
    times = list(_TIME_RE.finditer(result.text))
    start_pos = times[0].start() if times else None
    end_pos = times[1].start() if len(times) > 1 else None
    start_abs = _detect_absolute_date(result.text, start_pos)
    end_abs = _detect_absolute_date(result.text, end_pos)
    payload = json.loads(result.model_dump_json())
    payload.pop("draft", None)
    payload["draft"] = {
        "title": result.draft.title,
        "priority": result.draft.priority,
        "start_at": None,
        "end_at": None,
    }
    payload["_relative"] = {
        "start": _encode(result.draft.start_at, today, start_abs),
        "end": _encode(result.draft.end_at, today, end_abs),
    }
    return payload


def _from_recordable(payload: dict, today) -> NLParseResult:
    payload = dict(payload)
    rel = payload.pop("_relative", {}) or {}
    draft = dict(payload.pop("draft", {}) or {})
    draft["start_at"] = _decode(rel.get("start"), today)
    draft["end_at"] = _decode(rel.get("end"), today)
    payload["draft"] = draft
    # source 要如实标注：这条结果来自录制回放，不是现场算的
    if payload.get("source") == "rule":
        payload["source"] = "cache"
    return NLParseResult(**payload)


@router.post("", response_model=NLParseResult)
async def parse_text(body: NLParseRequest) -> NLParseResult:
    """解析一句话 → 草稿。**不写库**。"""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")

    try:
        now = parse_iso(body.now) if body.now else datetime.now(timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"now 格式非法: {exc}") from exc

    today = utc_to_local(now).date()
    recorder = get_recorder()

    # ---- DEMO_MODE 回放 ----
    cached = recorder.load(_FIXTURE_KIND, {"text": text})
    if cached is not None:
        try:
            result = _from_recordable(cached, today)
            logger.info("DEMO 回放命中: %s", text)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("DEMO fixture 损坏，回退现场解析: %s", exc)

    result = parse_nl(text, now=now)

    # ---- 规则举手说"我不会" -> 交给模型（P4）----
    if result.error is not None and result.error.code == "NXC-NEED-LLM":
        result = await _try_llm(text, now, today, recorder, reason=result.error.message)

    # ---- DEMO_MODE 录制 ----
    if recorder.record:
        kind = _FIXTURE_KIND if result.source in ("rule", "cache") else _FIXTURE_KIND_LLM
        recorder.save(kind, {"text": text}, _to_recordable(result, today))

    logger.info(
        "NL 解析: text=%r status=%s source=%s missing=%s",
        text, result.status, result.source, result.missing,
    )
    return result


async def _try_llm(
    text: str, now, today, recorder: DemoRecorder, *, reason: str
) -> NLParseResult:
    """规则放弃后的兜底链路：DEMO 回放优先，其次真调模型，最后退回规则报错。

    **顺序不能反**：演示时（DEMO_MODE）必须优先走录制，
    否则"离线也能演示"的承诺会在这一类句子上失效。
    """
    cached = recorder.load(_FIXTURE_KIND_LLM, {"text": text})
    if cached is not None:
        try:
            out = _from_recordable(cached, today)
            out.source = "cache"
            logger.info("NL-LLM 回放命中: %s", text)
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("NL-LLM fixture 损坏，改用现场解析: %s", exc)

    if settings.DEMO_MODE:
        # 演示模式没有录到这句 —— 绝不偷偷联网
        return parse_nl(text, now=now)

    from app.llm.provider import get_provider
    from app.services.nl_llm import parse_with_llm

    if not get_provider().available:
        # 没配 Key 就是 P3 的行为，不该把来源标成 llm —— 那会让用户以为模型说了什么
        return parse_nl(text, now=now)

    try:
        return await parse_with_llm(text, now, reason=reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NL-LLM 解析异常，退回规则结果: %s", exc)
        return parse_nl(text, now=now)


@router.post("/dry-run", response_model=NLParseResult, include_in_schema=False)
def parse_dry_run(body: NLParseRequest) -> NLParseResult:
    """强制走现场解析（跳过 DEMO 回放），供测试与录制期使用。"""
    now = parse_iso(body.now) if body.now else datetime.now(timezone.utc)
    return parse_nl(body.text, now=now)


__all__ = ["router", "NLDraft"]
