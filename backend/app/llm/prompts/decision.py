"""决策提示词。

三条从 DeepSeek 文档与实测教训里来的硬要求，写在这里以免被后人删掉：

1. **必须出现 "json" 字样并给出格式示例**，否则 JSON Output 模式可能不生效。
2. **必须允许模型说"不干预"**（`no_action`）。契约里没有这个出口，
   模型就会为了"有产出"而硬造建议 —— 那正是过度干预的来源。
3. **不许让模型决定宠物情绪。** 它只写台词；档位是确定性仪表盘（文档 §5.3）。
"""

from __future__ import annotations

import json
import re
from typing import get_args

from app.core.constants import (
    DECISION_MAX_SUGGESTIONS,
    INTERVENTION_MUTE_THRESHOLD,
    LLM_PROMPT_MAX_EVENTS,
    LLM_PROMPT_MAX_RECORDS,
)
from app.core.time import utc_to_local
from app.llm.contract import (
    PAYLOAD_EXAMPLE,
    DecisionPayload,
    DecisionRequest,
    Intervention,
    InterventionAction,
    Suggestion,
)
from app.llm.normalize import ALIAS_EXAMPLES

SYSTEM_PROMPT = """你是一个「执行教练」的决策器，帮助用户把一天的安排变得可执行。

你的职责边界（违反任何一条都会被后端拒绝）：
- **只做判断与表达**：在已知冲突与信号的基础上，判断该不该干预、建议怎么调整、用什么语气说。
- **不要做算术**：逾期天数、完成率、负载比都已算好并给了你，不要重算，也不要臆造数字。
- **不要决定宠物情绪**：情绪由后端确定性计算，你只写 intervention.pet_line 这一句台词。
- **不许编造日程 id**：所有 target_record_id 必须来自下面给出的日程列表，一个字都不能改。
- **不许给不可变日程提建议**：immutable_record_ids 里的 id 一律不许出现在建议中。
- **建议时间必须落在工作时间内**，且结束时间必须晚于开始时间。
- **允许不干预**：如果当前情况不值得打扰用户，就返回 decision_type = "no_action"。沉默是合法且常被期望的回答。

语气要求：陈述事实 + 给出下一步，不评价、不说教、不制造负罪感。用户已经在拖延的时候，不要再说"你怎么又没做"。

输出要求：只输出一个 JSON 对象，符合下面的格式示例，不要输出任何解释性文字或代码块围栏。"""

#: 单个枚举取值的形状（纯小写字母/下划线），用来判断要不要给对照值加引号
_TOKEN_RE = re.compile(r"^[a-z_]+$")


def _enum_choices(model, field: str) -> str:
    """从契约的 `Literal` 直接取允许值，不手写第二份 —— 手写必然漂移。

    `list[X]` 这类容器要再剥一层：字段是 `list[ReasoningTag]`，
    真正的允许值在 `ReasoningTag` 里。
    """
    ann = model.model_fields[field].annotation
    args = get_args(ann)
    if len(args) == 1 and get_args(args[0]):
        args = get_args(args[0])
    return " | ".join(f'"{a}"' for a in args)


def _render_enum_hint() -> str:
    """枚举允许值清单 + "不要这样写"的对照。

    为什么值得占这段 token：实测 6 次裸调用里 3 次因为枚举跑偏被判失败
    （模型写 `time_conflict`、`dismiss`）。**让模型一次写对，比事后重试一次便宜** ——
    后者要多花一次 10 秒级的调用，而且可能直接降级成规则兜底。

    允许值由契约生成，对照只列真实出现过或代价最高的几条（不是全表，
    堆太多反而稀释重点）。完整映射表在 `llm/normalize.py`。
    """
    rows = (
        ("decision_type", DecisionPayload, "decision_type"),
        ("reasoning_tags[]", DecisionPayload, "reasoning_tags"),
        ("suggestions[].kind", Suggestion, "kind"),
        ("suggestions[].risk", Suggestion, "risk"),
        ("intervention.type", Intervention, "type"),
        ("intervention.tone", Intervention, "tone"),
        ("intervention.actions[].kind", InterventionAction, "kind"),
    )
    lines = ["所有枚举字段**只能**从下列取值里选，禁止自造同义词（自造会被后端拒绝并要求重答）："]
    for label, model, field in rows:
        lines.append(f"- {label:<30s}: {_enum_choices(model, field)}")
    lines.append("")
    lines.append("最容易写错的几个（左边是错的，右边才对）：")
    for wrong, right in ALIAS_EXAMPLES:
        # 只给"单个取值"加引号；整句说明加引号会读起来像取值
        shown = f'"{right}"' if _TOKEN_RE.match(right) else right
        lines.append(f'- ✗ "{wrong}" → ✓ {shown}')
    lines.append("")
    lines.append(
        '尤其注意：actions 里的 "dismiss" 是「这次先不了」= "snooze"，'
        '不是永久屏蔽；永久屏蔽只有 "mute" 这一个值。'
    )
    return "\n".join(lines)


_OUTPUT_HINT = """
请严格按以下 JSON 格式输出（字段名与取值必须完全一致）：
{example}

可选的 decision_type：
- "reschedule"：有具体可执行的调整建议（同时填 suggestions 与 intervention）
- "nudge"     ：没有可执行的调整，只想提醒一句（填 intervention，suggestions 留空）
- "no_action" ：这次不该打扰用户（其余字段可留空）

{enums}
"""


def _fmt(dt) -> str:
    """UTC naive -> 本地 `10-30 14:00`。

    给模型看本地时间而不是 UTC：模型要用它推理"下午会不会太赶"，
    给 UTC 等于逼它做一次时区换算，而它大概率算错。
    """
    if dt is None:
        return "—"
    local = utc_to_local(dt)
    return local.strftime("%m-%d %H:%M")


def _fmt_iso(dt) -> str:
    if dt is None:
        return ""
    local = utc_to_local(dt)
    return local.isoformat()


def render_user_prompt(
    req: DecisionRequest,
    *,
    max_records: int = LLM_PROMPT_MAX_RECORDS,
    max_events: int = LLM_PROMPT_MAX_EVENTS,
) -> str:
    """把 DecisionRequest 渲染成人（和模型）都读得懂的一段话。

    刻意不丢完整 JSON：缩进的 JSON 会吃掉大量 token，
    而模型对"表格式列举"的理解并不比 JSON 差。
    """
    lines: list[str] = []

    lines.append(f"现在时间：{_fmt(req.triggered_at)}（{req.timezone}）")
    lines.append(f"触发原因：{req.trigger}")
    if req.horizon is not None:
        lines.append(f"关注区间：{_fmt(req.horizon.start)} ~ {_fmt(req.horizon.end)}")
    lines.append("")

    s = req.signals
    lines.append("## 已算好的信号（直接使用，不要重算）")
    lines.append(
        f"- 拖延指数 {s.procrastination_index:.2f}（0~1）"
        f"｜7 日完成率 {s.completion_rate_7d:.0%}"
        f"｜逾期 {s.overdue_count} 条，平均逾期 {s.avg_overdue_days:.1f} 天"
        f"｜连续达标 {s.streak_days} 天"
        f"｜最大单日负载 {s.load_ratio:.2f}"
    )
    if s.conflicts:
        for c in s.conflicts:
            ids = ",".join(c.record_ids) or "—"
            kind_cn = {"overlap": "时间重叠", "no_buffer": "间隔过窄", "overload": "当日过满"}[c.kind]
            lines.append(
                f"- 冲突：{kind_cn}（{'硬冲突' if c.hard else '软冲突'}）涉及 {ids}"
                f"{f'，重叠 {c.overlap_minutes} 分钟' if c.overlap_minutes else ''}"
            )
    else:
        lines.append("- 冲突：无")
    if s.idle_blocks:
        blocks = "、".join(f"{_fmt(b.start)}~{_fmt(b.end).split(' ')[-1]}" for b in s.idle_blocks[:5])
        lines.append(f"- 可用来安排的空闲：{blocks}")
    lines.append("")

    ps = req.pet_state
    if ps is not None:
        mood_cn = {
            "alert": "警觉（未来有撞车）", "dormant": "休眠（很久没完成）",
            "tired": "疲惫（已在拖延）", "guilty": "内疚（欠账累积）",
            "great": "很好（状态在线）", "normal": "平静",
        }[ps.mood]
        lines.append(f"## 宠物当前情绪：{mood_cn}（能量 {ps.energy:.2f}）")
        lines.append("这是后端算好的事实，你**不能**修改它，只能据此调整台词语气。")
        lines.append("")

    lines.append("## 今日及未来日程")
    if not req.records:
        lines.append("- （没有任何日程）")
    for r in req.records[:max_records]:
        span = f"{_fmt(r.start)}~{_fmt(r.end).split(' ')[-1]}" if r.start and r.end else "（无时间）"
        lines.append(f"- id={r.id}｜{span}｜{r.title}｜优先级{r.priority or 3}｜状态 {r.status}")
    if len(req.records) > max_records:
        lines.append(f"- （另有 {len(req.records) - max_records} 条未列出）")
    lines.append("")

    if req.behavior_events:
        lines.append("## 近期行为")
        for e in req.behavior_events[:max_events]:
            lines.append(f"- {_fmt(e.at)} {e.kind}{f' ({e.record_id})' if e.record_id else ''}")
        lines.append("")

    c = req.constraints
    lines.append("## 硬约束")
    lines.append(f"- 建议条数不超过 {c.max_suggestions} 条")
    lines.append(f"- 建议时间必须落在 {c.working_hours.get('start', '08:00')} ~ {c.working_hours.get('end', '23:00')}")
    if c.immutable_record_ids:
        lines.append(f"- 以下日程不可改动：{', '.join(c.immutable_record_ids)}")

    # ---- 用户反馈（P5 回流出口）----
    # 放在硬约束**之后**、输出格式**之前**：这是模型的注意力最集中的位置。
    # 反馈若只出现在 prompt 开头，长上下文里极易被忽略 —— 而"用户说不想再看到"是
    # 整段 prompt 里权重最高的一条信息，它不该藏在中间。
    lines.append(_render_feedback(req.user_profile))

    lines.append(
        _OUTPUT_HINT.strip().format(
            example=json.dumps(PAYLOAD_EXAMPLE, ensure_ascii=False, indent=2),
            enums=_render_enum_hint(),
        )
    )
    return "\n".join(lines)


def _render_feedback(profile) -> str:
    """把用户对干预的反馈渲染成模型必须遵守的一段话。

    这一段是 `mute` 的**唯一**落地出口：用户在卡片上点"不再提醒"，
    若它不出现在这里，那次点击就只是一个数据库里的字符串，模型下一轮照提不误。
    因此 `docs/13` 与该函数的测试都要求：静默反馈时一段都不许少。
    """
    lines: list[str] = ["## 用户反馈（真实发生过，务必尊重）"]

    if profile.muted_intervention_types:
        lines.append(
            "- **已被用户彻底降权的干预类型**："
            + "、".join(profile.muted_intervention_types)
        )
        lines.append(
            "  本轮**绝不许**输出这些 decision_type。"
            "若通盘考虑后你认为唯一合理的动作正是被降权的类型，"
            "就返回 decision_type = \"no_action\" —— 沉默比重复一次已被拒绝的建议更有用。"
        )
    else:
        lines.append("- 目前没有被降权的干预类型。")

    near = [
        f"{k}（{v}/{INTERVENTION_MUTE_THRESHOLD} 次）"
        for k, v in sorted((profile.ignore_counts or {}).items())
        if k not in profile.muted_intervention_types and v > 0
    ]
    if near:
        lines.append(
            "- 被忽略但尚未降权的类型：" + "、".join(near)
            + f"。再被忽略一次就会被彻底屏蔽，请谨慎使用，或换一种说法再提。"
        )

    accepted = [
        f"{k}×{v}" for k, v in sorted((profile.accept_counts or {}).items()) if v > 0
    ]
    if accepted:
        lines.append("- 曾被用户采纳的类型：" + "、".join(accepted) + "。说明这类建议对他有效。")

    if (profile.accepted_intervention_types
            and not profile.accept_counts):  # pragma: no cover - 兼容旧数据
        lines.append(
            "- 曾被采纳的类型：" + "、".join(profile.accepted_intervention_types)
        )

    return "\n".join(lines)


__all__ = ["SYSTEM_PROMPT", "render_user_prompt"]
