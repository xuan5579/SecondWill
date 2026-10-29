"""提示词模板。

单独成一个包而不是塞进 `orchestrator.py`，理由：prompt 是**会被反复调的东西**，
而调用它的编排逻辑不应该跟着一起改。答辩前想换语气，只动这里。
"""

from app.llm.prompts.decision import (
    SYSTEM_PROMPT,
    render_user_prompt,
)

__all__ = ["SYSTEM_PROMPT", "render_user_prompt"]
