"""自然语言解析的对外契约。

这份 schema 是 P3 / P4 两期的**共同契约**（见架构文档 §0.1 的设计决策 D-4）：

- P3：source 恒为 "rule"（或 DEMO_MODE 下的 "cache"）
- P4：接上 LLM 后增加 "llm"，但**字段一个都不改**

换句话说，分期的切分点落在"谁来算出这个结构"，而不是"输出长什么样"。
前端的草稿卡片因此在 P4 完全不用重写。

另一个刻意之处：`draft` 里的字段全部可为 None。
这是 constraint ①「不许硬编」的直接后果 —— 解析器拿不到的信息就留空，
由前端渲染成待填输入框，而不是塞一个静默的默认值。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

NLStatus = Literal["resolved", "need_user", "error"]
NLSource = Literal["rule", "llm", "cache"]
NLField = Literal["title", "start_at", "end_at", "priority"]


class NLDraft(BaseModel):
    """草稿字段。拿不到的一律 None —— 绝不给静默默认值。"""

    title: str | None = Field(default=None, description="标题，解析不出为 None")
    start_at: str | None = Field(default=None, description="ISO8601 带时区；None = 待用户填")
    end_at: str | None = Field(default=None, description="ISO8601 带时区；None = 待用户填")
    priority: int = Field(default=3, ge=1, le=5, description="1-5，与 Record 同口径")


class NLAssumption(BaseModel):
    """解析器**替用户做的推断**，必须在卡片上标黄展示。

    missing 与 assumptions 的分工：
    - missing    = 完全没有依据，留空让用户填
    - assumptions = 有约定俗成的默认解读，预填但告知
    """

    code: str = Field(description="稳定代号，前端可按 code 做本地化")
    label: str = Field(description="给用户看的一句话说明")
    field: NLField = Field(description="该推断影响哪个字段")


class NLError(BaseModel):
    """解析失败。**必须**带 hint —— 报错的目的是让用户知道怎么改，不是显示"失败了"。"""

    code: str = Field(description="错误代号，如 NXC-1")
    message: str = Field(description="人话描述")
    hint: str | None = Field(default=None, description="怎么改才能被解析")


class NLParseResult(BaseModel):
    status: NLStatus = Field(
        description=(
            "resolved=可直接确认 / need_user=缺字段待补 / "
            "error=自相矛盾或无解，已附 hint"
        )
    )
    source: NLSource = Field(description="rule=本地规则 / llm=模型(P4) / cache=DEMO 回放")
    draft: NLDraft = Field(default_factory=NLDraft)
    missing: list[NLField] = Field(default_factory=list, description="待用户填写的字段")
    assumptions: list[NLAssumption] = Field(
        default_factory=list, description="已替用户做的推断，需标黄提示"
    )
    error: NLError | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    text: str = Field(default="", description="归一化后的输入原文，便于排查")


class NLParseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200, description="用户的一句话")
    now: str | None = Field(
        default=None,
        description="ISO8601 基准时刻（UTC naive 或带时区）。省略则用服务器当前时刻。",
    )
