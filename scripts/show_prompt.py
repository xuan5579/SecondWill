#!/usr/bin/env python3
"""打印「下一次真的会发给模型的 prompt」—— 第 6 章的镜头素材。

**为什么需要它**：视频要证明「用户点过 3 次『不再提醒』之后，模型真的看见了」。
"模型有没有看见"这件事无法靠界面证明，唯一的证据是**渲染出来的 prompt 文本**
—— `tests/test_feedback_loop.py` 里就是这么断言的，这里把它变成能给镜头拍的输出。

**只读**：不调用模型、不写库、不改任何状态。走的就是生产链路的同一批函数
（`load_inputs` → `compute_state` → `load_traces` → `build_request` →
`render_user_prompt`），所以屏幕上这行字与真实请求里的那行字逐字一致。

用法::

    python scripts/show_prompt.py                  # 只打印与「降权」相关的段落
    python scripts/show_prompt.py --grep 忽略       # 换个关键词
    python scripts/show_prompt.py --full            # 打印整份 user prompt
    python scripts/show_prompt.py --context 4       # 关键词上下多带 4 行
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.logging import setup_logging  # noqa: E402
from app.db.base import utcnow  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llm.orchestrator import build_request  # noqa: E402
from app.llm.prompts import render_user_prompt  # noqa: E402
from app.services.state import compute_state, load_inputs  # noqa: E402

# _profile_dict 是私有函数，但生产链路（services/decision_flow.run_decision）
# 用的就是它把反馈画像转成 user_profile —— 这里照抄同一条路径，
# 才能保证打印出来的 prompt 与真实请求**逐字一致**，而不是"大致相同"。
from app.services.decision_flow import _profile_dict, load_traces  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="打印下一次决策的 user prompt")
    parser.add_argument("--grep", default="降权", help="只打印命中该关键词的段落（默认：降权）")
    parser.add_argument("--context", type=int, default=2, help="命中行的上下额外行数")
    parser.add_argument("--full", action="store_true", help="打印整份 prompt，不做过滤")
    args = parser.parse_args()

    setup_logging()
    now = utcnow()
    db = SessionLocal()
    try:
        records, events = load_inputs(db)
        signals, pet = compute_state(db, now=now)
        traces = load_traces(db, now=now)

        req = build_request(
            records,
            events,
            signals,
            pet,
            trigger="manual",
            now=now,
            user_profile=_profile_dict(traces),
        )
        prompt = render_user_prompt(req)
    finally:
        db.close()

    lines = prompt.splitlines()

    if args.full:
        print(prompt)
        print(f"\n[共 {len(lines)} 行]")
        return 0

    hits = [i for i, ln in enumerate(lines) if args.grep in ln]
    if not hits:
        print(f"prompt 里没有出现「{args.grep}」。")
        print("→ 说明降权尚未发生：先按手册 A4 连点 3 次「不再提醒」，再跑本脚本。")
        print(f"→ 想看整份 prompt：python scripts/show_prompt.py --full（共 {len(lines)} 行）")
        return 1

    print(f"命中「{args.grep}」{len(hits)} 处（共 {len(lines)} 行，只显示命中段落）：\n")
    shown: set[int] = set()
    for i in hits:
        lo, hi = max(0, i - args.context), min(len(lines), i + args.context + 1)
        for j in range(lo, hi):
            if j in shown:
                continue
            shown.add(j)
            mark = ">>" if j == i else "  "
            print(f"{mark} {lines[j]}")
        print("  " + "-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
