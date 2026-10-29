"""决策编排器 —— 触发判定 / 组装 / 校验 / 重试 / 降级。

这个文件的职责可以概括成一句话：**让"调模型"这件事变得可失败。**

触发判定（文档 B3）：
    产生 hard 冲突 / 拖延指数 ≥ LLM_TRIGGER_INDEX / 手动触发 —— 才会真的去调模型。
    改日程但没冲突**不调用**：那是最常见的操作，每次都问模型既烧钱又慢，
    而且"你改了个日程，模型说：好的"这种交互没有任何价值。

失败阶梯：
    缓存/DEMO 命中 > 真实调用 > （L1 结构错 → 回传错误重试 2 次）
    > （截断 → 加 token 重试 1 次）> L2 部分采纳 > L0 规则降级
每一步都在 `DecisionResult.source` 里留痕，`llm` / `cache` / `fallback` 三选一，
前端可以标注、答辩可以讲。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Sequence

from app.core.config import settings
from app.core.constants import (
    DECISION_MAX_SUGGESTIONS,
    FORWARD_WINDOW,
    LLM_CACHE_TTL_MINUTES,
    LLM_L3_INDEX,
    LLM_MAX_RETRIES,
    LLM_PROMPT_MAX_EVENTS,
    LLM_PROMPT_MAX_RECORDS,
    LLM_TOKEN_LADDER,
    LLM_TRIGGER_INDEX,
    WORKING_HOURS_END,
    WORKING_HOURS_START,
)
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.time import parse_iso
from app.llm.cache import DemoRecorder, fingerprint, get_recorder
from app.llm.contract import (
    DecisionConstraints,
    DecisionEvent,
    DecisionPayload,
    DecisionRecord,
    DecisionRequest,
    DecisionSignals,
    DecisionConflict,
    PetStatePayload,
    TimeRange,
    UserProfile,
)
from app.llm import provider as provider_pkg
from app.llm.provider import LLMError, LLMErrorKind
from app.llm.validator import LEVEL_L1, validate_payload
from app.services.fallback import build_fallback

logger = get_logger(__name__)

Source = Literal["llm", "cache", "fallback"]

#: 决策指纹的类别前缀。与 NL 的 `nl_parse` 分开，避免撞 key。
_FIXTURE_KIND = "decision"

#: ISO8601 时间字符串（可带时区）
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)


def _relativize(obj: Any, base: datetime) -> Any:
    """把 payload 里的绝对时间换成"距基准时刻多少分钟"。

    **为什么必须做这件事**：决策请求里全是绝对时间，直接取哈希的话，
    今天录的 fixture 到明天演示就再也不会命中 —— 指纹随日期漂移。
    这与 NL 录制时"存 day_offset 而不是日期"是同一个问题的两种形态。

    代价是两个"形状相同、时间整体平移"的请求会共享一个 fixture，
    这在本项目里正是想要的行为（演示数据本身就是相对 now 生成的）。
    """
    if isinstance(obj, dict):
        return {k: _relativize(v, base) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_relativize(v, base) for v in obj]
    if isinstance(obj, str) and _ISO_RE.match(obj):
        dt = parse_iso(obj)
        if dt is None:
            return obj
        return {"__min_from_now": int(round((dt - base).total_seconds() / 60.0))}
    return obj


def relativized_payload(req: DecisionRequest) -> dict:
    """指纹与录制共用的规范化请求体。"""
    return _relativize(req.to_fingerprint_payload(), req.triggered_at)


@dataclass
class DecisionResult:
    payload: DecisionPayload
    source: Source
    attempts: int = 1
    issues: list[str] = field(default_factory=list)
    request_id: str = ""

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "source": self.source,
            "attempts": self.attempts,
            "issues": self.issues,
            "payload": self.payload.model_dump(mode="json"),
        }


# ==================================================================
# 触发判定
# ==================================================================


def should_trigger(
    signals,
    trigger: str,
    *,
    index_threshold: float = LLM_TRIGGER_INDEX,
) -> tuple[bool, str]:
    """是否值得打扰模型（文档 B3）。

    返回 (是否触发, 原因)。原因会写进日志 —— 排查"为什么没出建议"时，
    第一件事就是看这里。
    """
    if trigger == "manual":
        return True, "手动触发"
    hard = [c for c in getattr(signals, "conflicts", ()) if c.hard]
    if hard:
        return True, f"存在 {len(hard)} 处硬冲突"
    index = getattr(signals, "procrastination_index", 0.0)
    if index >= index_threshold:
        return True, f"拖延指数 {index:.2f} ≥ {index_threshold}"
    return False, "无硬冲突且拖延指数未达阈值"


# ==================================================================
# 请求组装
# ==================================================================


def _rank_records(records: Sequence, signals, now: datetime) -> list:
    """按"对这次决策的相关度"排序 —— 截断必须发生在排序之后。

    ## 为什么不能按库序直接切前 N 条（2026-10-30 的教训）

    实测推理 token 与 prompt 里的记录条数强相关：9 条要 905~2737 token，
    40 条几乎必然撞顶 8192（docs/09 §6）。所以条数必须压下来；而一旦压下来，
    "留哪几条"就直接决定决策质量 —— 按库序切前 14 条，被截掉的可能是
    正在冲突的那两条，模型连决策的对象都看不到。

    ## 分层规则（每层内部按"离 now 最近"排，最后按 id 保证确定性）

    0. 硬冲突涉及的两条 —— 决策的核心对象，永远最先保留；
    1. 软冲突涉及的条；
    2. 逾期且未完成的 —— 拖延干预的依据（start < now 且未 done/dropped）；
    3. 未来 24h 内的（离 now 越近越相关）；
    4. 远期日程（24h 之外）；
    5. 已完成 / 已放弃 —— 完成情况信号已单独给过模型，条目本身信息量最低，
       永远排最末（否则"离 now 最近"的排序会把刚完成的条目顶到远期待办前面）。
    """
    hard_ids: set[str] = set()
    soft_ids: set[str] = set()
    for c in getattr(signals, "conflicts", ()) or ():
        (hard_ids if getattr(c, "hard", False) else soft_ids).update(
            str(i) for i in getattr(c, "record_ids", ())
        )

    def tier(r) -> int:
        rid = str(getattr(r, "id", ""))
        if rid in hard_ids:
            return 0
        if rid in soft_ids:
            return 1
        start = getattr(r, "start_at", None)
        status = str(getattr(r, "status", "pending"))
        if status in ("done", "dropped"):
            return 5
        if start is not None and start < now:
            return 2
        if start is not None and now <= start < now + FORWARD_WINDOW:
            return 3
        return 4

    def key(r):
        start = getattr(r, "start_at", None)
        proximity = abs((start - now).total_seconds()) if start is not None else float("inf")
        return (tier(r), proximity, str(getattr(r, "id", "")))

    return sorted(records, key=key)


def build_request(
    records: Sequence,
    events: Sequence,
    signals,
    pet_state,
    *,
    trigger: str,
    now: datetime,
    request_id: str | None = None,
    immutable_record_ids: Sequence[str] = (),
    user_profile: dict | None = None,
) -> DecisionRequest:
    """把库里的 ORM 对象与纯函数信号组装成契约请求。

    这也是**唯一**把数据交给模型的地方：字段在这里被显式挑出来，
    而不是把 ORM 对象整个丢进去 —— 后者会把内部字段一起泄出去。
    """
    profile = UserProfile(**(user_profile or {}))

    ranked = _rank_records(records, signals, now)
    items: list[DecisionRecord] = []
    for r in ranked[:LLM_PROMPT_MAX_RECORDS]:
        items.append(
            DecisionRecord(
                id=str(r.id),
                type=getattr(r, "type", "schedule"),
                title=getattr(r, "title", ""),
                status=getattr(r, "status", "pending"),
                start=getattr(r, "start_at", None),
                end=getattr(r, "end_at", None),
                priority=getattr(r, "priority", 3),
                tags=list(getattr(r, "tags", []) or []),
            )
        )

    ev_items: list[DecisionEvent] = []
    for e in events[:LLM_PROMPT_MAX_EVENTS]:
        try:
            ev_items.append(
                DecisionEvent(
                    at=getattr(e, "at"),
                    kind=getattr(e, "kind"),
                    record_id=getattr(e, "record_id", None),
                )
            )
        except Exception:  # noqa: BLE001 - 单条脏事件不该拖垮整次决策
            continue

    conflicts = [
        DecisionConflict(
            record_ids=[str(i) for i in c.record_ids],
            kind=c.kind,
            hard=c.hard,
            overlap_minutes=c.overlap_minutes,
        )
        for c in getattr(signals, "conflicts", ())
    ]

    sig = DecisionSignals(
        conflicts=conflicts,
        procrastination_index=getattr(signals, "procrastination_index", 0.0),
        load_ratio=getattr(signals, "load_ratio", 0.0),
        idle_blocks=[
            TimeRange(start=b.start, end=b.end)
            for b in getattr(signals, "idle_blocks", ())
        ][:5],
        completion_rate_7d=getattr(signals, "completion_rate_7d", 0.0),
        overdue_count=getattr(signals, "overdue_count", 0),
        avg_overdue_days=getattr(signals, "avg_overdue_days", 0.0),
        streak_days=getattr(signals, "streak_days", 0),
    )

    pet = None
    if pet_state is not None:
        pet = PetStatePayload(
            mood=getattr(pet_state, "mood").value
            if hasattr(getattr(pet_state, "mood", None), "value")
            else getattr(pet_state, "mood", "normal"),
            energy=getattr(pet_state, "energy", 0.5),
            streak_days=getattr(pet_state, "streak_days", 0),
        )

    tzname = datetime.now().astimezone().tzname() or "local"

    return DecisionRequest(
        request_id=request_id or new_id("req_"),
        trigger=trigger,  # type: ignore[arg-type]
        triggered_at=now,
        timezone=str(tzname),
        horizon=TimeRange(start=now, end=now + FORWARD_WINDOW),
        user_profile=profile,
        records=items,
        behavior_events=ev_items,
        signals=sig,
        pet_state=pet,
        constraints=DecisionConstraints(
            max_suggestions=DECISION_MAX_SUGGESTIONS,
            immutable_record_ids=list(immutable_record_ids),
            working_hours={"start": WORKING_HOURS_START, "end": WORKING_HOURS_END},
        ),
    )


# ==================================================================
# 结果缓存
# ==================================================================

_CACHE: dict[str, tuple[float, DecisionPayload]] = {}


def _cache_get(key: str) -> DecisionPayload | None:
    hit = _CACHE.get(key)
    if hit is None:
        return None
    ts, payload = hit
    if time.monotonic() - ts > LLM_CACHE_TTL_MINUTES * 60:
        _CACHE.pop(key, None)
        return None
    return payload


def _cache_put(key: str, payload: DecisionPayload) -> None:
    _CACHE[key] = (time.monotonic(), payload)


def clear_cache() -> None:
    """清空决策缓存。测试用，也用于"数据变了但指纹恰好没变"的极端情况。"""
    _CACHE.clear()


# ==================================================================
# 主入口
# ==================================================================


async def decide(
    req: DecisionRequest,
    *,
    provider=None,
    recorder: DemoRecorder | None = None,
    use_cache: bool = True,
) -> DecisionResult:
    """跑一次决策。无论是模型、缓存还是规则，**返回同一个契约**。"""
    recorder = recorder or get_recorder()
    # 走模块属性而不是模块级绑定：测试要能替换掉 get_provider
    provider = provider if provider is not None else provider_pkg.get_provider()
    fp_payload = relativized_payload(req)

    # ---------- 1. DEMO 录制回放 ----------
    cached = recorder.load(_FIXTURE_KIND, fp_payload)
    if cached is not None:
        try:
            payload = DecisionPayload.model_validate(cached)
            return DecisionResult(payload, "cache", attempts=0, request_id=req.request_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo fixture 结构失效，改走真实链路: %s", exc)

    # ---------- 2. 内存缓存 ----------
    key = fingerprint(_FIXTURE_KIND, fp_payload)
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return DecisionResult(hit, "cache", attempts=0, request_id=req.request_id)

    # ---------- 3. 真实调用 ----------
    if settings.DEMO_MODE:
        # 演示模式未命中录制 —— 绝不能偷偷真调。
        # 否则"断网演示"会在某一次请求上悄悄变成"联网请求"，现场直接穿帮。
        logger.info("DEMO_MODE 下未命中 fixture，直接转规则降级")
        outcome = None
        stats: dict = {"attempts": 0, "reason": "L0: DEMO_MODE 未命中 fixture"}
    else:
        stats = {}
        outcome = await _call_with_retry(req, provider, stats)

    if outcome is not None:
        payload, attempts, issues = outcome

        # ---------- L3：模型说别管，但信号很强 ----------
        if (
            payload.decision_type == "no_action"
            and req.signals.procrastination_index >= LLM_L3_INDEX
        ):
            logger.info(
                "L3 降级：模型返回 no_action，但拖延指数 %.2f ≥ %.2f，改发规则提醒",
                req.signals.procrastination_index,
                LLM_L3_INDEX,
            )
            fb = build_fallback(req, now=req.triggered_at)
            fb.caveats = list(fb.caveats) + [
                f"模型建议不干预，但拖延指数 {req.signals.procrastination_index:.2f}，"
                "后端改为发送规则提醒"
            ]
            result = DecisionResult(
                fb, "fallback", attempts=attempts,
                issues=issues + ["L3: 模型 no_action 但信号过强"],
                request_id=req.request_id,
            )
        else:
            result = DecisionResult(
                payload, "llm", attempts=attempts, issues=issues,
                request_id=req.request_id,
            )
    else:
        # ---------- L0：规则降级 ----------
        fb = build_fallback(req, now=req.triggered_at)
        result = DecisionResult(
            # attempts 必须如实：provider 不可用（没配 Key / DEMO）时一次都没调过，
            # 写成 MAX_RETRIES+1 会让"调用次数"这个可观测字段失去意义。
            fb, "fallback", attempts=int(stats.get("attempts", 0)),
            issues=[stats.get("reason", "L0: 模型不可用或输出不可用，已转规则降级")],
            request_id=req.request_id,
        )

    # ---------- 落缓存 / 录制 ----------
    if use_cache:
        _cache_put(key, result.payload)
    if recorder.record:
        recorder.save(_FIXTURE_KIND, fp_payload, result.payload.model_dump(mode="json"))

    return result


async def _call_with_retry(
    req: DecisionRequest, provider, stats: dict | None = None
) -> tuple[DecisionPayload, int, list[str]] | None:
    """调用模型并在必要时重试。返回 None 表示"这条路走不通，请降级"。

    `stats` 用于把实际调用次数与失败原因回传给调用方 —— 决策结果里的
    `attempts` 是要给人看的，不能填一个看起来合理的假数字。
    """
    stats = stats if stats is not None else {}

    if not getattr(provider, "available", True):
        logger.info("未提供 API Key 或 provider 不可用，直接走规则降级")
        stats.update(attempts=0, reason="L0: provider 不可用（未配置 API Key）")
        return None

    from app.llm.prompts import SYSTEM_PROMPT, render_user_prompt

    base_user = render_user_prompt(req)
    issues: list[str] = []
    attempts = 0
    max_tokens: int | None = None
    # 抬档序列按"比当前默认值更大"筛一遍：默认已经是 4096 时，
    # 再从 4096 开始抬等于原地重试一次，白花一次调用。
    ladder = [t for t in LLM_TOKEN_LADDER if t > settings.LLM_MAX_TOKENS]
    tier = 0

    for attempt in range(LLM_MAX_RETRIES + 1):
        attempts = attempt + 1
        user_prompt = base_user
        if issues:
            # 把上一轮的校验错误原文回传 —— 比"请重新输出"有效得多
            user_prompt = (
                base_user
                + "\n\n## 上一轮输出被拒绝，原因如下，请修正后重新输出 JSON：\n"
                + "\n".join(f"- {i}" for i in issues[-5:])
            )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        try:
            resp = await provider.complete_json(messages, max_tokens=max_tokens)
        except LLMError as exc:
            issues.append(f"{exc.kind.value}: {exc.detail}")
            logger.warning("LLM 调用失败（%s）: %s", exc.kind.value, exc.detail)
            stats.update(attempts=attempts, reason=f"L0: {exc.kind.value} — {exc.detail}")
            # 截断与空 content 走**同一条**抬档路径。
            # 空 content 不是玄学偶发：实测它的 finish_reason 就是 length、
            # 推理 token 把额度吃光了（docs/09 §3②）。不改额度地重试 = 再撞一次墙。
            if exc.kind in (LLMErrorKind.TRUNCATED, LLMErrorKind.EMPTY) and tier < len(ladder):
                max_tokens = ladder[tier]
                tier += 1
                logger.info(
                    "%s，提高 max_tokens 至 %d 重试",
                    "输出被截断" if exc.kind is LLMErrorKind.TRUNCATED else "返回空 content",
                    max_tokens,
                )
                continue
            return None

        result = validate_payload(resp.data, req)
        if result.ok:
            return result.payload, attempts, [str(i) for i in result.issues]

        issues.extend(str(i) for i in result.issues)
        if result.level == LEVEL_L1:
            logger.warning("LLM 输出未通过结构校验（第 %d 次），准备重试", attempts)
            continue

        # L2：业务护栏拦下了一部分，但还有可用的建议 -> 采纳剩下的
        if result.payload is not None and result.payload.suggestions:
            logger.info("部分建议被业务护栏拦下，保留可行的 %d 条", len(result.payload.suggestions))
            return result.payload, attempts, [str(i) for i in result.issues]

        logger.warning("全部建议不可行（L2），转规则降级")
        stats.update(attempts=attempts, reason="L2: 建议全部不可行，转规则降级")
        return None

    stats.update(attempts=attempts, reason="L1: 结构校验连续失败，转规则降级")
    return None
