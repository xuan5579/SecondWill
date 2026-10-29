"""枚举同义词归一化 —— **兜底**，不是主力。

## 为什么需要它

DeepSeek 的 `json_object` 只保证"是合法 JSON"，枚举值照样会跑偏。实测 6 次裸调用里
3 次因此被判 L1 失败：模型写了 `time_conflict`（应为 `conflict`）、`dismiss`（应为 `snooze`）。

## 为什么它是兜底而不是主力

主力是 prompt：把允许值显式列出来 + 给几个"不要这样写"的对照（`prompts/decision.py`
的枚举段，由 `ALIAS_EXAMPLES` 生成）。**让模型一次就答对，比事后猜它的心思便宜。**
归一化只负责接住那些 prompt 没拦住的漏网之鱼 —— 所以它必须**保守**：
只映射语义无歧义的同义词，拿不准的一律不映射，让校验照旧失败并把原文回传给模型重试。

## 一条硬规则：宁轻勿重

同一个词在两个枚举值之间摇摆时，选**后果更轻**的那个。最典型的例子是 `dismiss`：

- `snooze` = 这次先不了，一次性的；
- `mute` = 写进 `UserProfile.muted_intervention_types`，是**永久降权**（P5 反馈回流）。

`dismiss` 的字面意思是"这次不要"，映射成 `mute` 会让用户因为模型用词随意
而被永久屏蔽一类建议 —— 这种错误不可逆，所以这里一律映射成 `snooze`。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


# ==================================================================
# 映射表
#
# 每条都写了判断依据。**不确定语义的一律不进表**（见下方 _AMBIGUOUS），
# 宁可让校验失败重试，也不要猜错 —— 猜错会静默污染数据，失败重试至少是显式的。
# ==================================================================

ALIASES: dict[str, dict[str, str]] = {
    # ---- decision_type：这次要不要干预 ----
    "decision_type": {
        # 都是"给出可执行的调整"：改时间/换位置，本质都是重排
        "reschedule": "reschedule", "rearrange": "reschedule",
        "replan": "reschedule", "adjust": "reschedule",
        "move": "reschedule", "reschedule_proposal": "reschedule",
        # 只是提醒一句，没有可执行的调整
        "nudge": "nudge", "remind": "nudge", "reminder": "nudge",
        "prompt": "nudge", "encourage": "nudge",
        # 沉默是合法出口；skip 在这里=这次不干预，不是"跳过某条日程"
        "no_action": "no_action", "none": "no_action", "nothing": "no_action",
        "noop": "no_action", "no_change": "no_action", "skip": "no_action",
    },
    # ---- suggestions[].kind：对这条日程做什么 ----
    "suggestion_kind": {
        # 改到别的时段。postpone/delay 是"往后挪"，不是"不做"
        "move": "move", "reschedule": "move", "shift": "move",
        "relocate": "move", "postpone": "move", "delay": "move",
        # 压缩时长
        "shrink": "shrink", "shorten": "shrink", "reduce": "shrink",
        "trim": "shrink", "compress": "shrink",
        # 取消这条。cancel/delete 在这里指"从今天的安排里去掉"，与 drop 同义
        "drop": "drop", "remove": "drop", "cancel": "drop",
        "delete": "drop", "discard": "drop",
    },
    # ---- suggestions[].risk ----
    "risk": {
        "low": "low", "small": "low", "minor": "low", "safe": "low",
        "trivial": "low",
        "medium": "medium", "moderate": "medium", "mid": "medium",
        "high": "high", "large": "high", "big": "high", "risky": "high",
        "major": "high", "severe": "high",
    },
    # ---- reasoning_tags：这个判断的依据是什么 ----
    "reasoning_tag": {
        # 日程之间互相挤
        "conflict": "conflict", "time_conflict": "conflict",
        "conflicts": "conflict", "overlap": "conflict",
        "overlapping": "conflict", "clash": "conflict", "collision": "conflict",
        # 当日排期过多（区别于上面的"两条撞车"）
        "overload": "overload", "overloaded": "overload", "busy": "overload",
        "too_busy": "overload", "too_many": "overload", "heavy": "overload",
        "heavy_load": "overload",
        # 逾期 / 往后拖
        "procrastination": "procrastination", "procrastinating": "procrastination",
        "delayed": "procrastination", "delay": "procrastination",
        "overdue": "procrastination", "postponement": "procrastination",
        "dragging": "procrastination", "stalled": "procrastination",
        # 有空档可用
        "idle": "idle", "free": "idle", "free_time": "idle", "gap": "idle",
        "empty_slot": "idle", "free_block": "idle",
        # 连续达标
        "streak": "streak", "consistency": "streak", "continuity": "streak",
        "habit_streak": "streak",
    },
    # ---- intervention.type ----
    "intervention_type": {
        "reschedule_proposal": "reschedule_proposal", "reschedule": "reschedule_proposal",
        "proposal": "reschedule_proposal", "rearrangement": "reschedule_proposal",
        "adjustment": "reschedule_proposal",
        "nudge": "nudge", "reminder": "nudge", "remind": "nudge",
        "prompt": "nudge", "encouragement": "nudge",
    },
    # ---- intervention.tone ----
    "tone": {
        "gentle": "gentle", "soft": "gentle", "warm": "gentle",
        "friendly": "gentle", "kind": "gentle", "calm": "gentle",
        "direct": "direct", "straight": "direct", "blunt": "direct",
        "firm": "direct", "plain": "direct",
        "playful": "playful", "fun": "playful", "humorous": "playful",
        "cheerful": "playful", "light": "playful", "cute": "playful",
    },
    # ---- intervention.actions[].kind：卡片按钮点下去会发生什么 ----
    "action_kind": {
        # 采纳：真的去改日程
        "accept": "accept", "apply": "accept", "confirm": "accept",
        "ok": "accept", "yes": "accept", "agree": "accept",
        "do_it": "accept", "adopt": "accept", "execute": "accept",
        # 稍后再说：一次性的推迟
        "snooze": "snooze", "later": "snooze", "dismiss": "snooze",
        "not_now": "snooze", "remind_later": "snooze", "postpone": "snooze",
        "defer": "snooze", "skip": "snooze", "maybe_later": "snooze",
        # 永久不再提醒：只有明确表达"永远/别再"才进这一档
        "mute": "mute", "never": "mute", "dont_ask_again": "mute",
        "no_more": "mute", "block": "mute", "disable": "mute", "silence": "mute",
    },
}

#: **刻意不映射**的词，以及为什么不映射。
#: 它们共同的特点是：在两个合法值之间摇摆，而猜错有实际代价。
#: 留着不映射，校验会失败并把原文回传给模型 —— 模型通常一次就改对，
#: 这比我们替它猜一个可能错的答案安全得多。
_AMBIGUOUS: tuple[tuple[str, str], ...] = (
    ("ignore", "在『忽略一次』与『永久不想看』之间摇摆；且与 P5 的 intervention_ignored "
               "事件同名。猜成 mute 的代价是永久屏蔽，不可逆"),
    ("stop", "『别停』既可能指这一次，也可能指以后都别提"),
    ("normal", "作为 risk 既可能指『正常=低』也可能指『中等』，无共识"),
    ("habit", "streak 是连续达标天数，habit 只是『习惯』，两者不是一回事"),
    ("later", "（仅 intervention.actions 之外）在『稍后提醒』与『改到更晚的时间』之间摇摆；"
              "在 action_kind 里语义明确，故只对 actions 映射"),
)

#: prompt 里的"不要这样写 → 应该这样写"对照。
#: 只挑**真实出现过**或**代价最高**的几条，不是全表 —— prompt 里堆太多反而稀释重点。
ALIAS_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("time_conflict", "conflict"),
    ("schedule_conflict", "conflict"),
    ("overlap", "conflict"),          # 只在 reasoning_tags 语境下
    ("dismiss", "snooze"),            # 不是 mute：dismiss 是"这次先不了"
    ("later", "snooze"),
    ("ignore", "snooze 或 mute 都不对，请明确写其中一个"),
    ("postpone", "（actions 里）snooze；（suggestions 里）move"),
)


def _key(value: str) -> str:
    """归一化比较用的键：去空白、小写、连字符/空格/点统一成下划线。"""
    return value.strip().lower().replace("-", "_").replace(" ", "_").replace(".", "_")


def _map_value(table: dict[str, str], value: Any, path: str, notes: list) -> Any:
    """单个值的映射。非字符串、或不在表里 -> 原样返回（交给校验去判）。"""
    if not isinstance(value, str):
        return value
    canonical = table.get(_key(value))
    if canonical is None or canonical == value:
        return value
    # 原始值必须留下痕迹：否则"模型到底写了什么"这件事会彻底消失，
    # 下次再跑偏时无从查起。日志 + 返回给上层写进 issues。
    logger.warning("枚举归一化 %s: %r -> %r（模型原始输出）", path, value, canonical)
    notes.append((path, value, canonical))
    return canonical


def normalize_enums(raw: dict) -> tuple[dict, list[tuple[str, str, str]]]:
    """把模型输出里"同义但非法"的枚举值改成合法值。

    返回 `(原 dict, [(路径, 原始值, 归一化值), ...])`。
    dict 是**就地修改**的 —— 调用方拿到的是同一份对象，不必重新赋值。
    """
    notes: list[tuple[str, str, str]] = []

    if not isinstance(raw, dict):
        return raw, notes

    if "decision_type" in raw:
        raw["decision_type"] = _map_value(
            ALIASES["decision_type"], raw["decision_type"], "decision_type", notes
        )

    tags = raw.get("reasoning_tags")
    if isinstance(tags, list):
        raw["reasoning_tags"] = [
            _map_value(ALIASES["reasoning_tag"], t, f"reasoning_tags[{i}]", notes)
            for i, t in enumerate(tags)
        ]

    suggestions = raw.get("suggestions")
    if isinstance(suggestions, list):
        for i, s in enumerate(suggestions):
            if not isinstance(s, dict):
                continue
            if "kind" in s:
                s["kind"] = _map_value(
                    ALIASES["suggestion_kind"], s["kind"], f"suggestions[{i}].kind", notes
                )
            if "risk" in s:
                s["risk"] = _map_value(
                    ALIASES["risk"], s["risk"], f"suggestions[{i}].risk", notes
                )

    iv = raw.get("intervention")
    if isinstance(iv, dict):
        if "type" in iv:
            iv["type"] = _map_value(
                ALIASES["intervention_type"], iv["type"], "intervention.type", notes
            )
        if "tone" in iv:
            iv["tone"] = _map_value(
                ALIASES["tone"], iv["tone"], "intervention.tone", notes
            )
        actions = iv.get("actions")
        if isinstance(actions, list):
            for i, a in enumerate(actions):
                if isinstance(a, dict) and "kind" in a:
                    a["kind"] = _map_value(
                        ALIASES["action_kind"], a["kind"],
                        f"intervention.actions[{i}].kind", notes,
                    )

    return raw, notes


__all__ = ["ALIASES", "ALIAS_EXAMPLES", "normalize_enums"]
