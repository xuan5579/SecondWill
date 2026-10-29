"""自然语言录入的提示词（P4）。

规则解析器交给模型的，是它**明确承认自己不知道**的那部分：
句子过长、出现"月底""答辩前"这类没有确定日期的词、一句话里多个动作。

这正是"规则优先"这条硬约束的价值所在：模型只在规则举手说"我不会"时才被调用，
而且它拿到的输入里已经写清了"规则为什么放弃"，避免模型重新犯同样的错。
"""

from __future__ import annotations

import json

from app.core.time import utc_to_local
from datetime import datetime

SYSTEM_PROMPT = """你是一个日程解析助手，把用户的一句话转成结构化的日程草稿。

输出要求：只输出一个 JSON 对象，不要任何解释文字或代码块围栏。

字段规则（违反会被后端拒绝）：
- title    ：去掉"提醒我""帮我"这类开头的动词，只留事情本身；不要加引号。
- start/end：必须是完整的 `YYYY-MM-DDTHH:MM:SS`，**不许省略日期**。
             拿不到就填 null，并把字段名放进 missing —— 不许猜。
- priority ：1~5，1 最高。用户没提就填 3。
- missing  ：确实无法确定的字段名数组，取值只能是 title / start_at / end_at / priority。
- assumptions：你**替用户做的推断**（例如把"今天"解释成具体日期），
             必须逐条列出，用户会在界面上看到并被要求确认。

时间基准以输入里给出的"现在时间"为准。用户说"明天"就按那个基准推算，
**不要**用你自己的日期知识。"""

OUTPUT_EXAMPLE = {
    "title": "写周报",
    "start": "2026-10-30T15:00:00",
    "end": "2026-10-30T16:00:00",
    "priority": 3,
    "missing": [],
    "assumptions": [{"code": "date_default_today", "label": "未提到日期，暂按今天", "field": "start_at"}],
}


def render_user_prompt(text: str, now: datetime, *, reason: str = "") -> str:
    """组装用户输入。`reason` 是规则层放弃的原因，交给模型作为提示。"""
    local = utc_to_local(now)
    lines = [
        f"现在时间：{local.isoformat()}",
        f"用户的一句话：{text}",
    ]
    if reason:
        lines.append(f"（本地规则放弃的原因：{reason}）")
    lines.append("")
    lines.append("请严格按以下 JSON 格式输出：")
    lines.append(json.dumps(OUTPUT_EXAMPLE, ensure_ascii=False, indent=2))
    return "\n".join(lines)


__all__ = ["SYSTEM_PROMPT", "render_user_prompt", "OUTPUT_EXAMPLE"]
