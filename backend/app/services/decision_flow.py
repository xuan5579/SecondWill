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
from app.models.record import Record
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

    # ---- 新鲜度复核 ----
    # 模型跑这几秒里，用户可能已经把问题自己解决了（见 _stale_reason）。
    # 过时的建议比没有建议更糟：它会让人怀疑整套判断。
    stale = _stale_reason(db, result, trigger)
    if stale:
        logger.info("决策结果已过时，不送达: %s", stale)
        return {
            "triggered": True,
            "delivered": False,
            "reason": stale,
            "blocked_by": "stale",
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


def _stale_reason(db: Session, result: DecisionResult, trigger: str) -> str | None:
    """模型跑完这几秒里，用户可能已经把问题自己解决了。

    ## 为什么需要这道复核（2026-11-17 实测）

    一次决策要 6～9 秒。在那 9 秒里用户不会停手：实测连着勾掉五条记录，
    第一步触发的决策在第 9.2 秒才落库，这时五条已经全部完成 ——
    卡片却还在说「把整理实验数据挪到明早 08:00」，而那条刚被用户自己勾掉。
    过时的建议比没有建议更糟：它让人怀疑整套判断是不是在看旧数据。

    ## 两条判据

    1. **触发条件是否还在**：撞车被勾掉、逾期降到阈值以下 —— 那就没有理由
       再推"要不要重排"。对手动触发豁免：用户就是想问一次，问完状态没变也照给。
    2. **建议指向的记录是否还活着**：已完成 / 已删除的不能再"挪走"。
       这条对手动触发同样生效。

    复核本身失败时**放行**（返回 None）：宁可送一条可能过时的建议，
    也不要因为一次查询异常把正常建议吞掉。
    """
    try:
        signals, _ = compute_state(db, now=utcnow())
    except Exception as exc:  # noqa: BLE001
        logger.warning("新鲜度复核失败（放行）: %s", exc)
        return None

    if trigger not in RHYTHM_BYPASS_TRIGGERS:
        ok, why = should_trigger(signals, trigger)
        if not ok:
            return f"状态在决策期间已变（{why}）"

    ids = {s.target_record_id for s in result.payload.suggestions if s.target_record_id}
    if ids:
        try:
            rows = db.query(Record).filter(Record.id.in_(ids)).all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("复核建议指向的记录失败（放行）: %s", exc)
            return None
        alive = {r.id for r in rows if r.status not in ("done", "dropped")}
        gone = ids - alive
        if gone:
            return f"建议指向的 {len(gone)} 条记录已完成或已被删除"

    return None


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


# 后台决策的单飞闸门（见 run_decision_bg 的注释）。
# 用普通布尔量而不是 asyncio.Lock：判断与置位之间没有 await，单事件循环下等价于原子；
# 而模块级 Lock 会把创建时的事件循环绑死，测试里每个用例一个新循环，一用就报
# "attached to a different loop"。
_bg_running = False
_bg_pending: str | None = None


async def run_decision_bg(trigger: str = "schedule_change") -> None:
    """后台任务入口：自建会话，绝不复用请求会话。**同一时刻只跑一个决策。**

    ## 为什么要单飞（2026-11-17 实测）

    一次「勾完成」会同时踩两条写路径 —— `PATCH /records` 与随后的 `POST /events`，
    两条都各自 `add_task(run_decision_bg)`。连点五条记录，瞬间就有 8～10 个决策
    同时起跑，每个都占着自己的会话等模型（10 秒量级）。SQLite 的 QueuePool
    默认 5+10，一眨眼就被占满，之后 30 秒内**所有**请求（连 /api/state 都算）
    一起超时；连接恢复后这批积压的决策又在同一毫秒落库 —— 实测一次连点
    就在干预表里砸出 7 条（6 条 reschedule + 1 条 no_action），全是过时状态，
    当天的 3 次自动额度被一次性烧光，界面上还留着一堆指向已完成记录的僵尸卡。

    ## 规则

    跑着的时候来的请求只记下「还有事要跑」，跑完用**最新**状态补跑一次。
    信号不会丢，并发不会雪崩。
    """
    global _bg_running, _bg_pending

    # 手动触发优先级最高，不能被自动触发的待跑位盖掉
    if trigger in RHYTHM_BYPASS_TRIGGERS or _bg_pending is None:
        _bg_pending = trigger

    if _bg_running:
        logger.info("决策已在跑，本次触发合并进补跑（%s）", trigger)
        return

    _bg_running = True
    try:
        while _bg_pending is not None:
            current = _bg_pending
            _bg_pending = None

            from app.db.session import SessionLocal

            db = SessionLocal()
            try:
                await run_decision(db, trigger=current)
            except Exception as exc:  # noqa: BLE001
                logger.warning("后台决策失败（已忽略）: %s", exc)
            finally:
                db.close()
    finally:
        _bg_running = False

