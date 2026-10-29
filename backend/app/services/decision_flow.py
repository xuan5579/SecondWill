"""决策链路的 IO 边界。

与 `services/state.py` 的角色相同：**纯逻辑不碰库，碰库的只在这里。**
编排器（`llm/orchestrator.py`）不读数据库也不写数据库，
它只认契约对象 —— 这条分层让"换模型"和"换存储"互不干扰。

链路：
    读库 -> 信号聚合 -> 触发判定 -> **节奏闸门** -> 组装请求 -> 编排器决策
        -> **降权兜底拒发** -> 落干预记录 -> SSE 广播

P5 新增的两道闸门都在这一层（IO 边界）执行，因为只有这里读得到反馈历史；
判定逻辑本身在 `services/rhythm.py`，是纯函数，可脱离数据库测试。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.constants import (
    INTERVENTION_FEEDBACK_WINDOW_DAYS,
    INTERVENTION_HISTORY_LIMIT,
    RHYTHM_BYPASS_TRIGGERS,
)
from app.core.logging import get_logger
from app.db.base import utcnow
from app.llm.orchestrator import DecisionResult, build_request, decide, should_trigger
from app.models.intervention import Intervention
from app.services.events import publish_decision
from app.services.rhythm import (
    InterventionTrace,
    evaluate_rhythm,
    feedback_profile,
    is_muted,
)
from app.services.state import compute_state, load_inputs

logger = get_logger(__name__)


def load_traces(
    db: Session,
    *,
    now: datetime | None = None,
    window_days: int = INTERVENTION_FEEDBACK_WINDOW_DAYS,
    limit: int = INTERVENTION_HISTORY_LIMIT,
) -> list[InterventionTrace]:
    """读取反馈窗口内的干预历史，映射成闸门的纯输入。

    `trigger` 存在 payload 里而不是新增一列：v1.0 的库已经在用户机器上建好了，
    为一个只用于统计的标记去做列迁移不划算，而 JSON 列本来就是为这类
    "将来可能有用"的附属信息准备的。
    """
    now = now or utcnow()
    since = now - timedelta(days=window_days)
    rows = (
        db.query(Intervention)
        .filter(Intervention.created_at >= since)
        .order_by(Intervention.created_at.desc())
        .limit(limit)
        .all()
    )
    traces: list[InterventionTrace] = []
    for r in rows:
        payload = r.payload or {}
        trigger = str(payload.get("trigger") or "")
        traces.append(
            InterventionTrace(
                decision_type=r.decision_type,
                created_at=r.created_at,
                delivered_at=r.delivered_at,
                feedback=r.feedback,
                feedback_at=r.feedback_at,
                # 手动触发不占今日额度：用户主动问一次就被扣额度，是在惩罚使用
                metered=trigger not in RHYTHM_BYPASS_TRIGGERS,
            )
        )
    return traces


async def run_decision(
    db: Session,
    *,
    trigger: str = "schedule_change",
    now: datetime | None = None,
    persist: bool = True,
    broadcast: bool = True,
    provider=None,
) -> dict[str, Any]:
    """跑一次完整决策。返回可直接给前端的字典。

    绝不向上抛异常：决策是旁路，它失败最多让"这次没有建议"，
    绝不能回滚用户刚刚完成的保存动作（与 `notify.py` 同一条原则）。
    """
    now = now or utcnow()
    try:
        records, events = load_inputs(db)
        signals, pet = compute_state(db, now=now)
        traces = load_traces(db, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("决策前置计算失败: %s", exc)
        return {"triggered": False, "reason": f"状态计算失败: {exc}"}

    ok, reason = should_trigger(signals, trigger)
    if not ok:
        logger.info("决策未触发: %s", reason)
        return {"triggered": False, "reason": reason, "pet_mood": pet.mood.value}

    # ---- 节奏闸门（B4 冷却 / B5 每日上限）----
    # 放在调用模型**之前**：被拦下时一次调用都不烧。
    # 这是它与降权兜底（只能放在决策之后）最本质的区别。
    verdict = evaluate_rhythm(traces, trigger, now)
    if not verdict.can_deliver:
        logger.info("节奏闸门拦下: %s", verdict.reason)
        return {
            "triggered": False,
            "delivered": False,
            "reason": verdict.reason,
            "blocked_by": verdict.blocked_by,
            "rhythm": _rhythm_dict(verdict),
            "pet_mood": pet.mood.value,
        }

    req = build_request(
        records,
        events,
        signals,
        pet,
        trigger=trigger,
        now=now,
        user_profile=_profile_dict(traces),
    )
    try:
        result: DecisionResult = await decide(req, provider=provider)
    except Exception as exc:  # noqa: BLE001
        logger.warning("决策编排异常，转规则降级: %s", exc)
        from app.services.fallback import build_fallback

        payload = build_fallback(req, now=now)
        result = DecisionResult(payload, "fallback", attempts=0, issues=[f"编排异常: {exc}"],
                                request_id=req.request_id)

    # ---- 降权兜底拒发（B6）----
    # 正常路径不会走到这里：被降权的类型已经写进 prompt 的负面清单，
    # 模型自己就不会再提。这一道是"模型没听话"时的最后一道保险 ——
    # 用户点过三次"不再提醒"之后还弹出来，是不可接受的。
    if is_muted(result.payload.decision_type, traces):
        reason = f"类型 {result.payload.decision_type} 已被用户降权，本次不予送达"
        logger.info("降权兜底拒发: %s", reason)
        return {
            "triggered": True,
            "delivered": False,
            "reason": reason,
            "blocked_by": "muted",
            "rhythm": _rhythm_dict(verdict),
            "pet_mood": pet.mood.value,
        }

    intervention_id: str | None = None
    if persist:
        try:
            row = Intervention(
                decision_type=result.payload.decision_type,
                # trigger 一并落库：闸门要区分"用户主动问的"和"系统主动推的"，
                # 前者不占每日额度。
                payload={**result.to_dict(), "trigger": trigger},
                source=result.source,
                delivered_at=utcnow(),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            intervention_id = row.id
        except Exception as exc:  # noqa: BLE001
            logger.warning("干预记录落库失败（不影响展示）: %s", exc)
            db.rollback()

    if broadcast:
        try:
            publish_decision(
                reason=f"decision:{trigger}",
                source=result.source,
                decision_type=result.payload.decision_type,
                intervention_id=intervention_id,
                suggestions=[s.model_dump(mode="json") for s in result.payload.suggestions],
                intervention=(
                    result.payload.intervention.model_dump(mode="json")
                    if result.payload.intervention
                    else None
                ),
                summary=result.payload.summary,
                confidence=result.payload.confidence,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("决策广播失败（已忽略）: %s", exc)

    return {
        "triggered": True,
        "delivered": True,
        "reason": reason,
        **result.to_dict(),
        "intervention_id": intervention_id,
        "rhythm": _rhythm_dict(verdict),
        "pet_mood": pet.mood.value,
    }


def _rhythm_dict(verdict) -> dict:
    """闸门结论的可序列化形式。前端用它显示"今日还剩几次"。"""
    return {
        "used_today": verdict.used_today,
        "remaining_today": verdict.remaining_today,
        "blocked_by": verdict.blocked_by,
        "next_allowed_at": (
            verdict.next_allowed_at.isoformat() + "+00:00"
            if verdict.next_allowed_at
            else None
        ),
    }


def _profile_dict(traces) -> dict:
    """反馈历史 -> `UserProfile` 的构造参数。**mute 回流进 prompt 的入口**。"""
    p = feedback_profile(traces)
    return {
        "muted_intervention_types": p.muted_intervention_types,
        "accepted_intervention_types": p.accepted_intervention_types,
        "ignore_counts": p.ignore_counts,
        "accept_counts": p.accept_counts,
    }


async def run_decision_bg(trigger: str = "schedule_change") -> None:
    """后台任务入口：自建会话，绝不复用请求会话。

    请求会话在响应返回后就会被关闭，后台任务里用它必然踩到
    "Session is closed" —— 这个坑在这里被显式绕开。
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        await run_decision(db, trigger=trigger)
    except Exception as exc:  # noqa: BLE001
        logger.warning("后台决策失败（已忽略）: %s", exc)
    finally:
        db.close()
