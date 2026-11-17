"""宠物状态机 —— 纯函数，确定性。**情绪是仪表盘，不是模型的输出。**

设计决策（用户裁决，见架构文档 §5.3）
--------------------------------------
这是**主动设计**，不是为了规避 LLM 延迟而做的被动妥协。理由有三：

1. **仪表盘的属性是实时、稳定、可解释。** 用户看到猫的表情，要能在 200ms 内得到反馈，
   并且能在相同数据下反复复现。概率模型天生不保证这两点——同一份输入两次刷新得出不同情绪，
   用户会觉得"这猫在抽风"，仪表盘就失去了意义。
2. **判断"猫该什么表情"用不上语言理解。** 这是算术；判断"该怎么劝这个人重新开工"才用得上语言。
   把算术交给模型，等于用最贵的方式做最便宜的事，还顺手引入了失败面。
3. **降级时不能连情感锚点一起丢。** LLM 挂掉时宠物情绪依然准确，只是台词退回模板；
   反过来如果情绪依赖模型，模型一挂宠物就死了，而它是这个产品的情感层。

由此得到一条推论：**LLM 只贡献台词，不贡献档位。**

档位语义（用户裁决的六档与优先级）
----------------------------------
按 `alert > dormant > tired > guilty > great > normal` 严格短路，
保证同一份输入永远得到同一份输出。

`alert` 与 `tired` 必须严格分离：
    alert  = 前瞻信号 —— 未来 24h 有 hard 冲突（还没发生，但会撞车）
    tired  = 回顾信号 —— 已经连续拖了
两者永不合并判定。把前瞻和回顾混进同一个档位，用户就无法从表情反推
"我该担心未来还是弥补过去"，仪表盘失去可解释性。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Sequence

from app.core.constants import (
    ENERGY_COMPLETION_WEIGHT,
    ENERGY_PROCRASTINATION_WEIGHT,
    PET_DORMANT_IDLE_DAYS,
    PET_GREAT_COMPLETION_MIN,
    PET_GREAT_DONE_MIN,
    PET_GREAT_STREAK_MIN,
    PET_GUILTY_COMPLETION_MAX,
    PET_GUILTY_OVERDUE_MIN,
    PET_MOOD_PRIORITY,
    PET_TIRED_OVERDUE_DAYS,
)
from app.core.time import containing_local_day, utc_to_local
from app.services.signal import Signals, clamp01, completion_dates


class PetMood(str, Enum):
    """六档情绪。`str` 子类，可直接 JSON 序列化。"""

    ALERT = "alert"        # 前瞻预警：未来 24h 存在 hard 冲突
    DORMANT = "dormant"    # 休眠：连续多日无任何完成
    TIRED = "tired"        # 已在拖延：连续多日存在逾期未处理
    GUILTY = "guilty"      # 拖欠累积：逾期条数成规模，且完成率低
    GREAT = "great"        # 状态很好：当日完成率高且连续达标
    NORMAL = "normal"      # 兜底


@dataclass(frozen=True)
class PetState:
    mood: PetMood
    energy: float               # 0..1
    streak_days: int
    computed_at: datetime
    reason: str = ""            # 命中依据。演示与调试面板用，保证情绪可解释


# ==================================================================
# 逐档判定
# ==================================================================
# 统一签名 (records, events, signals, now) -> bool，便于按优先级表遍历。

def _alert(records, events, signals: Signals, now: datetime) -> bool:
    """前瞻：未来 24h 存在硬冲突。信号源只有 `conflicts`，与回顾类指标严格分离。"""
    return any(c.hard for c in signals.conflicts)


def dormant_idle_days(records, events, now: datetime) -> int:
    """距最后一次"完成"过去了几天。

    没有任何完成时退回到「最早一条记录的创建日」作为"开始使用产品"的锚点——
    否则一个昨天才装好、一件事都没勾的用户会被判定为「停摆」，这是纯粹的误伤。
    全新到连记录都没有的用户返回 -1，表示无数据、不参与判定。
    """
    today = containing_local_day(now)
    days = completion_dates(records, events)
    if days:
        return (today - max(days)).days
    created = [
        containing_local_day(r.created_at)
        for r in records
        if getattr(r, "created_at", None) is not None
    ]
    if created:
        return (today - min(created)).days
    return -1


def _dormant(records, events, signals: Signals, now: datetime) -> bool:
    """休眠：连续多日没有任何完成（回顾）。"""
    idle = dormant_idle_days(records, events, now)
    return idle >= PET_DORMANT_IDLE_DAYS


def _tired(records, events, signals: Signals, now: datetime) -> bool:
    """已在拖延：存在逾期任务，且平均逾期天数达到门槛（回顾）。

    用户裁决的表述是「连续 ≥2 天存在逾期未完成任务」。这里用
    「平均逾期天数 ≥ PET_TIRED_OVERDUE_DAYS」落地——逾期仍未处理本身就是
    连续拖延的证据，而平均值比单纯计数更能区分"刚过期的 1 条"与"挂了很久的 10 条"。
    """
    return (
        signals.overdue_count > 0
        and signals.avg_overdue_days >= PET_TIRED_OVERDUE_DAYS
    )


def _guilty(records, events, signals: Signals, now: datetime) -> bool:
    """拖欠累积：逾期**数量**成规模，且近 7 日完成率明显偏低（回顾）。

    为什么看条数而不是拖延指数：指数在 tired 的短路下有一个数学上限
    （≈ 平均逾期天数 ÷ 7），永远到不了 0.6，这一档曾经是死档。
    详见 `PET_GUILTY_OVERDUE_MIN` 的注释。

    语义分工：`tired` 管「拖得久」（平均逾期 ≥2 天），`guilty` 管「欠得多」
    （一批都没处理，但单条还没拖到 2 天）。所以 guilty 只在 tired 之后才可能被读到。
    """
    return (
        signals.overdue_count >= PET_GUILTY_OVERDUE_MIN
        and signals.completion_rate_7d < PET_GUILTY_COMPLETION_MAX
    )


def _done_today_count(records: Sequence, now: datetime) -> int:
    """今天真正完成的条数。口径与 `signal.today_completion_rate` 的分子一致。

    直接从记录算而不进 `Signals`：它是这一档的判据细节，不是供 LLM 或前端消费的信号，
    加进契约会让所有下游都要跟着改。
    """
    today = utc_to_local(now).date()
    return sum(
        1
        for r in records
        if getattr(r, "end_at", None) is not None
        and containing_local_day(r.end_at) == today
        and getattr(r, "status", None) == "done"
    )


def _great(records, events, signals: Signals, now: datetime) -> bool:
    """状态很好：当日完成率高，且有"持续"或"量足"其中之一作证（回顾）。

    两个通道：
    - 连续 ≥3 天有完成 —— 老用户的节奏证据；
    - 今日完成 ≥3 件 —— 新用户的单日证据。

    第二条是为"历史是空的、但今天确实干了很多"的情形准备的：没有它，
    刚导入数据的人无论今天做得多好都只会看到 `normal`。
    同时要求 `completion_rate_today > 0`：今天一条计划都没排时不该被夸，
    虚假的正反馈比没有反馈更伤信任。
    """
    rate = signals.completion_rate_today
    if rate <= 0 or rate < PET_GREAT_COMPLETION_MIN:
        return False
    if signals.streak_days >= PET_GREAT_STREAK_MIN:
        return True
    return _done_today_count(records, now) >= PET_GREAT_DONE_MIN


#: 判定规则表。key 的顺序无意义，遍历顺序由 `PET_MOOD_PRIORITY` 决定——
#: 这样"加一档"和"调优先级"是两件互不干扰的事。
RULES: dict[PetMood, tuple] = {
    PetMood.ALERT: (_alert, "未来 24h 存在硬冲突（前瞻）"),
    PetMood.DORMANT: (_dormant, f"连续 ≥{PET_DORMANT_IDLE_DAYS} 天无任何完成"),
    PetMood.TIRED: (_tired, f"平均逾期 ≥{PET_TIRED_OVERDUE_DAYS} 天"),
    PetMood.GUILTY: (
        _guilty,
        f"逾期 ≥{PET_GUILTY_OVERDUE_MIN} 条且 7 日完成率 <{PET_GUILTY_COMPLETION_MAX}",
    ),
    PetMood.GREAT: (
        _great,
        f"当日完成率 ≥{PET_GREAT_COMPLETION_MIN}，且连续 ≥{PET_GREAT_STREAK_MIN} 天"
        f"或今日完成 ≥{PET_GREAT_DONE_MIN} 件",
    ),
}


# ==================================================================
# 主计算
# ==================================================================

def energy_of(signals: Signals) -> float:
    """宠物能量标量（文档 B11）：0.6 × 7 日完成率 + 0.4 × (1 - 拖延指数)。

    注意这里用 **7 日**完成率而非当日：能量是"体力条"，不该因为今天还没开始
    就被打到谷底，否则每天清晨打开应用都会看到一只蔫猫。
    """
    return clamp01(
        ENERGY_COMPLETION_WEIGHT * signals.completion_rate_7d
        + ENERGY_PROCRASTINATION_WEIGHT * (1.0 - signals.procrastination_index)
    )


def compute(records: Sequence, events: Sequence, signals: Signals, now: datetime) -> PetState:
    """计算宠物状态。**纯函数**：`now` 由调用方注入。

    按 `PET_MOOD_PRIORITY` 严格短路，第一个命中的规则即为结果。
    `normal` 不在 RULES 中，作为兜底——这保证了任何输入都有确定输出。
    """
    energy = round(energy_of(signals), 4)
    for mood_name in PET_MOOD_PRIORITY:
        mood = PetMood(mood_name)
        entry = RULES.get(mood)
        if entry is None:
            continue
        predicate, reason_text = entry
        if predicate(records, events, signals, now):
            return PetState(
                mood=mood,
                energy=energy,
                streak_days=signals.streak_days,
                computed_at=now,
                reason=reason_text,
            )

    return PetState(
        mood=PetMood.NORMAL,
        energy=energy,
        streak_days=signals.streak_days,
        computed_at=now,
        reason="无异常信号",
    )


# ==================================================================
# 台词兜底
# ==================================================================
# LLM 不可用时用这些模板顶上。设计上刻意保持"陈述事实 + 给出一个下一步"，
# 而不是评价：宠物不该在用户已经拖延的时候再补一句"你怎么又没做"。

LINES: dict[PetMood, tuple[str, ...]] = {
    PetMood.ALERT: (
        "接下来 24 小时里有两条日程撞在一起了，要不要现在挪开一条？",
        "我看了下明天，有个时间块塞不开了，现在改比明天救火省事。",
    ),
    PetMood.DORMANT: (
        "好几天没勾掉任务了。今天挑一条最小的先动手，怎么样？",
        "我们不急着补齐所有事，先把今天的清单过一遍吧。",
    ),
    PetMood.TIRED: (
        "有几件事已经过期几天了。要不我们现在处理掉最早的那条？",
        "逾期的事越攒越多就越难开始，我陪你挑一条先动。",
    ),
    PetMood.GUILTY: (
        "最近欠下来的有点多。要不要把其中一条拆小一点？",
        "我看了一下完成率，也许不是懒，是任务排得太满了。",
    ),
    PetMood.GREAT: (
        "连着好几天状态都在线，这个节奏不错，继续。",
        "今天完成得很干净，明天照这个量排就行。",
    ),
    PetMood.NORMAL: (
        "一切正常。有需要就说一声。",
        "今天的安排没什么问题，按计划来。",
    ),
}


def template_line(state: PetState, seed: int | None = None) -> str:
    """按档位取一句模板台词。

    `seed` 决定取哪一句：传入 `record_id` 的哈希可以让同一条任务稳定对应同一句，
    避免刷新页面时同一档位反复跳不同的话（那会显得随机、没有记忆）。
    """
    pool = LINES[state.mood]
    if seed is None:
        return random.choice(pool)
    return pool[seed % len(pool)]
