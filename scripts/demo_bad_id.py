#!/usr/bin/env python3
"""现场演示「规则 5：目标必须存在」—— 用**真实**校验器拦一条手工构造的违规建议。

**为什么不伪造日志**：这条规则平时由模型自己触发（模型输出了一个库里没有的
`target_record_id`），但拍摄时不可能等模型恰好犯错，也不能事后补一行假日志。
所以这里**手工构造**输入、**真实调用** `validate_payload()` ——
拦截是真拦截，规则是真规则，屏幕上那行文本与线上完全一致，
区别只在于"这次的坏输入是我们喂的，不是模型自己说的"。脚本独立运行，
不在生产链路里留任何钩子。

**顺带演示了一条重要设计**：业务性规则（5/6/7/9/10）是**就地修复**，
不是整包判死 —— 违规的那条被丢掉，合法的那条照常保留。

用法::

    python scripts/demo_bad_id.py
    python scripts/demo_bad_id.py --bad-id rec_故意写错的ID
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.logging import setup_logging  # noqa: E402
from app.db.base import utcnow  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llm.contract import DECISION_SCHEMA_VERSION  # noqa: E402
from app.llm.orchestrator import build_request  # noqa: E402
from app.llm.validator import validate_payload  # noqa: E402
from app.services.state import compute_state, load_inputs  # noqa: E402


def _iso(dt_utc_naive: datetime) -> str:
    """库里存的是 UTC naive，转成本地带偏移的 ISO 串再喂给契约。"""
    return dt_utc_naive.replace(tzinfo=timezone.utc).astimezone().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="演示规则 5：目标记录必须存在")
    parser.add_argument("--bad-id", default="rec_000000000000", help="伪造的不存在记录 ID")
    args = parser.parse_args()

    setup_logging()
    now = utcnow()
    db = SessionLocal()
    try:
        records, events = load_inputs(db)
        signals, pet = compute_state(db, now=now)
        req = build_request(records, events, signals, pet, trigger="manual", now=now)
    finally:
        db.close()

    if not records:
        print("库里没有记录，先跑 `python scripts/seed_video.py` 造数据。")
        return 1

    # 合法目标：取**硬冲突里的一条**，把它挪到"所有日程结束之后 +30 分钟"。
    # 这样应用之后硬冲突 1 → 0，规则 8（应用后不得产生新硬冲突）就不会掺进来
    # 干扰镜头 —— 否则屏幕上会出现两条 issue，第 5 章就讲不清了。
    hard = next((c for c in signals.conflicts if getattr(c, "hard", False)), None)
    if hard is None:
        print("当前没有硬冲突，先跑 `python scripts/seed_video.py` 造一组重叠日程。")
        return 1

    by_id = {r.id: r for r in records}
    target = by_id[hard.record_ids[-1]]
    duration = (target.end_at - target.start_at) if (target.start_at and target.end_at) else timedelta(hours=1)
    latest_end = max(r.end_at for r in records if r.end_at)
    new_start = latest_end + timedelta(minutes=30)
    start, end = _iso(new_start), _iso(new_start + duration)
    good_id = target.id

    raw = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "decision_type": "reschedule",
        "confidence": 0.71,
        "summary": "把复盘材料挪到明天上午，避开与架构文档的重叠",
        "reasoning_tags": ["conflict"],
        "suggestions": [
            {
                "id": "s1",
                "kind": "move",
                "target_record_id": args.bad_id,
                "proposed_start": start,
                "proposed_end": end,
                "rationale": "（这条是故意喂错的 ID）",
                "risk": "low",
            },
            {
                "id": "s2",
                "kind": "move",
                "target_record_id": good_id,
                "proposed_start": start,
                "proposed_end": end,
                "rationale": "复盘材料已完成前置依赖，挪到上午不会被打断",
                "risk": "low",
            },
        ],
        "caveats": [],
    }

    print("=== 输入：手工构造的 2 条建议（交给真实 validate_payload）===")
    print(f"  s1  move → {args.bad_id}   ← 库里没有这个 ID")
    print(f"  s2  move → {good_id}   ← 真实存在的记录\n")

    result = validate_payload(raw, req)

    print("=== 校验器输出 ===")
    print(f"  level : {result.level}")
    print(f"  ok    : {result.ok}")
    if result.issues:
        print("  issues:")
        for issue in result.issues:
            print(f"    {issue}")
    else:
        print("  issues: （无）")

    kept = list(result.payload.suggestions) if result.payload else []
    print(f"\n=== 结果：保留 {len(kept)} / 2 条 ===")
    for s in kept:
        print(f"  {s.id}  move → {s.target_record_id}")
    print("\n第 5 章口播点：违规的那条被丢掉，合法的那条照常留下 —— "
          "业务护栏是「就地修复」，不是整包判死。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
