"""录制 NL 解析的 DEMO fixture。

用法：
    python scripts/record_demo.py             # 录制《支持清单》全部例句
    python scripts/record_demo.py --clean     # 先清空旧的 nl_parse_* fixture

## 为什么录制的是相对量

直接存绝对时间戳的话，今天录的"明天上午9点"= 某个具体日期，
三天后演示就是一条**过期日程**，现场直接穿帮。

所以 `api/parse.py` 存的是 `day_offset` + `minute`（相对量），
回放时按当时的"今天"重新算绝对值。fixture 因此是常青的 ——
无论哪天演示，"明天上午9点"都还是明天上午9点。

注意：清单里的句子含有"11月3日""12月1日"这类**固定日期**，
它们的相对偏移会随录制日期变化。因此建议**在答辩前一天重录一次**，
让这些例句也落到未来的日期上。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("DEMO_RECORD", "true")
os.environ.setdefault("DEMO_MODE", "false")

from app.core.config import settings  # noqa: E402
from app.core.time import utc_to_local  # noqa: E402
from app.llm.cache import DemoRecorder  # noqa: E402
from app.llm.contract import DecisionRequest  # noqa: E402
from app.llm.orchestrator import build_request, decide  # noqa: E402
from app.services.naturallang import parse_nl  # noqa: E402
from app.services.nl_spec import SUPPORTED_EXAMPLES, UNSUPPORTED_EXAMPLES  # noqa: E402

from app.api.parse import _to_recordable  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="先删除旧的 nl_parse fixture")
    ap.add_argument("--decisions", action="store_true", help="录制决策场景（需 API Key）")
    ap.add_argument("--nl-llm", action="store_true", help="录制清单外句子的模型解析（需 API Key）")
    args = ap.parse_args()

    fixture_dir = Path(settings.DEMO_FIXTURES_DIR)
    fixture_dir.mkdir(parents=True, exist_ok=True)

    if args.clean:
        removed = 0
        for p in fixture_dir.glob("nl_parse_*.json"):
            p.unlink()
            removed += 1
        print(f"已清理 {removed} 个旧 fixture")

    now = _utc_naive_now()
    today = utc_to_local(now).date()
    recorder = DemoRecorder(base_dir=fixture_dir, replay=False, record=True)

    print(f"基准日期：{today}（本地）")
    print(f"输出目录：{fixture_dir}\n")

    ok = 0
    for ex in SUPPORTED_EXAMPLES:
        result = parse_nl(ex.text, now=now)
        recorder.save("nl_parse", {"text": ex.text}, _to_recordable(result, today))
        flag = "✅" if result.status == ex.status else "⚠️ "
        print(f"  {flag} {ex.text:34s} -> {result.status:9s} P{result.draft.priority}")
        ok += 1

    print(f"\n不支持的例句（同样录制，保证错误信息也稳定复现）：")
    for text, reason in UNSUPPORTED_EXAMPLES:
        result = parse_nl(text, now=now)
        recorder.save("nl_parse", {"text": text}, _to_recordable(result, today))
        print(f"  ⛔ {text:34s} -> {result.error.code if result.error else result.status}")

    print(f"\n已录制 {ok + len(UNSUPPORTED_EXAMPLES)} 条 fixture")

    if args.decisions or args.nl_llm:
        recorded = await _record_llm_parts(args)
        print(f"\n模型部分：录制 {recorded} 条")

    return 0


# ==================================================================
# 模型部分（需要真实 API Key）
# ==================================================================
#
# 规则解析与规则降级都是**确定性**的，现场算一遍即可，不需要录制。
# 这里只录"模型真的说过的话" —— 那才是没有 Key 时无法复现的东西。


def _utc_naive_now() -> datetime:
    """当前时刻的 **UTC naive** 形态。

    全项目的时间约定是「API 带时区 / 库与契约内部 UTC naive」（见 core/time.py）。
    录制脚本以前用 `datetime.now(timezone.utc)`（aware），NL 路径无所谓，
    但决策路径的 `triggered_at` 契约类型是 `UtcDT`（naive），
    于是指纹相对化时撞上 offset-naive 减 offset-aware 的 TypeError。
    统一从这里取，两边都不会再错。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hours(n: float):
    """相对 now 的偏移，返回 **naive UTC**。

    库里存的就是 naive UTC（见 core/time.py 约定），契约的 `UtcDT` 也是它。
    这里一旦返回 aware，指纹相对化时会撞上
    `can't subtract offset-naive and offset-aware datetimes` —— 而这个报错
    只在录制路径出现过（`now` 本身是 aware，测试用 naive 数据反而跑不到）。
    """
    return (datetime.now(timezone.utc) + timedelta(hours=n)).replace(tzinfo=None)


def _decision_scenarios(now: datetime) -> list[tuple[str, DecisionRequest]]:
    """三个典型场景。时间一律用**相对 now 的偏移**构造，
    这样今天录的 fixture 在隔天演示时指纹依然命中（见 orchestrator._relativize）。
    """
    conflict = build_request(
        records=[
            SimpleNamespace(
                id="rec_demo_a", title="部门例会", type="schedule", status="pending",
                start_at=_hours(2), end_at=_hours(3), priority=2, tags=[],
            ),
            SimpleNamespace(
                id="rec_demo_b", title="写周报", type="schedule", status="pending",
                start_at=_hours(2.5), end_at=_hours(3.5), priority=3, tags=[],
            ),
        ],
        events=[],
        signals=SimpleNamespace(
            conflicts=[], procrastination_index=0.1, load_ratio=0.4,
            idle_blocks=(), completion_rate_7d=0.6, overdue_count=0,
            avg_overdue_days=0.0, streak_days=2,
        ),
        pet_state=SimpleNamespace(mood="normal", energy=0.6, streak_days=2),
        trigger="schedule_change",
        now=now,
    )
    procrastination = build_request(
        records=[
            SimpleNamespace(
                id="rec_demo_c", title="补交上月报表", type="schedule", status="pending",
                start_at=_hours(-48), end_at=_hours(-47), priority=2, tags=[],
            )
        ],
        events=[],
        signals=SimpleNamespace(
            conflicts=[], procrastination_index=0.72, load_ratio=0.2,
            idle_blocks=(), completion_rate_7d=0.25, overdue_count=3,
            avg_overdue_days=4.5, streak_days=0,
        ),
        pet_state=SimpleNamespace(mood="tired", energy=0.3, streak_days=0),
        trigger="procrastination_signal",
        now=now,
    )
    overloaded = build_request(
        records=[
            SimpleNamespace(
                id=f"rec_demo_d{i}", title=f"连轴任务{i}", type="schedule", status="pending",
                start_at=_hours(1 + i), end_at=_hours(2.5 + i), priority=3, tags=[],
            )
            for i in range(5)
        ],
        events=[],
        signals=SimpleNamespace(
            conflicts=[], procrastination_index=0.2, load_ratio=1.35,
            idle_blocks=(), completion_rate_7d=0.5, overdue_count=0,
            avg_overdue_days=0.0, streak_days=1,
        ),
        pet_state=SimpleNamespace(mood="normal", energy=0.5, streak_days=1),
        trigger="manual",
        now=now,
    )
    return [
        ("硬冲突重排", conflict),
        ("拖延提醒", procrastination),
        ("当日过载", overloaded),
    ]


async def _record_llm_parts(args) -> int:
    """录制模型的真实响应。没有 Key 时只提示、不猜测。"""
    from app.llm.provider import get_provider
    from app.services.nl_llm import parse_with_llm

    provider = get_provider()
    if not provider.available:
        print("\n⚠️  未配置 DEEPSEEK_API_KEY，跳过模型部分。")
        print("    规则解析与规则降级都是确定性的，本来就不需要录制；")
        print("    只有模型说过的话才需要录 —— 那请在配好 Key 后再跑一次。")
        return 0

    recorder = DemoRecorder(base_dir=Path(settings.DEMO_FIXTURES_DIR), replay=False, record=True)
    now = _utc_naive_now()
    count = 0

    if args.decisions:
        print("\n决策场景：")
        for name, req in _decision_scenarios(now):
            result = await decide(req, provider=provider, recorder=recorder, use_cache=False)
            print(f"  {'✅' if result.source == 'llm' else '⚠️ '} {name:10s} -> "
                  f"source={result.source} type={result.payload.decision_type}")
            count += 1

    if args.nl_llm:
        print("\n清单外句子（规则放弃 -> 交给模型）：")
        for text, _reason in UNSUPPORTED_EXAMPLES:
            res = await parse_with_llm(text, now, provider=provider)
            recorder.save("nl_parse_llm", {"text": text}, json.loads(res.model_dump_json()))
            print(f"  {'✅' if res.status != 'error' else '⛔'} {text[:28]:30s} -> {res.status}")
            count += 1

    return count


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
