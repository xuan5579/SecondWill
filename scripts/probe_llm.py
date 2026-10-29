"""DeepSeek 能力探测脚本（对应文档 §9.3 的 B10 待决项）。

要回答的问题只有三个，但每一个都直接决定 P4 的代码怎么写：

1. `thinking` 与 `response_format={"type":"json_object"}` **能否同时使用**？
   若能，决策质量会更高；若不能，就得二选一。
2. provider 侧 **json_schema / strict 强制**是否可用？
   架构文档 §4.5 假设它不可用，所以校验层是强制路径 —— 这里验证这个假设。
3. 空 content / 截断这两类官方提示过的故障，在真实调用中**长什么样**？

用法：
    DEEPSEEK_API_KEY=sk-xxx python backend/probe_llm.py
    python backend/probe_llm.py --json-out probe_result.json

**没有 Key 时脚本会直接退出**——它不做任何猜测，结论必须由真实响应得出。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import settings  # noqa: E402

BASE = settings.DEEPSEEK_BASE_URL.rstrip("/")
MODEL = settings.DEEPSEEK_MODEL

MESSAGES = [
    {
        "role": "system",
        "content": "你是执行教练的决策器。只输出 JSON 对象，不要任何解释文字。",
    },
    {
        "role": "user",
        "content": (
            "现在时间：2026-10-30 10:00。日程：id=rec_a 14:00~15:00 部门例会；"
            "id=rec_b 14:30~15:30 写周报（两者重叠 30 分钟）。\n"
            '请严格按以下 JSON 输出：{"decision_type":"reschedule","confidence":0.8,'
            '"summary":"...","suggestions":[{"id":"s1","kind":"move",'
            '"target_record_id":"rec_b","proposed_start":"2026-10-30T15:30:00",'
            '"proposed_end":"2026-10-30T16:30:00","rationale":"...","risk":"low"}]}'
        ),
    },
]

#: 四种组合。顺序即"最想要 -> 最保守"。
CASES: list[tuple[str, dict]] = [
    (
        "A · json_object 单独使用",
        {"response_format": {"type": "json_object"}},
    ),
    (
        "B · json_object + thinking=enabled",
        {"response_format": {"type": "json_object"}, "thinking": {"type": "enabled"}},
    ),
    (
        "C · json_schema 强制（预期不支持）",
        {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "decision",
                    "schema": {
                        "type": "object",
                        "required": ["decision_type"],
                        "properties": {"decision_type": {"type": "string"}},
                    },
                },
            }
        },
    ),
    (
        "D · 无 response_format（基线对照）",
        {},
    ),
]


def probe(api_key: str, case_name: str, extra: dict) -> dict:
    body = {
        "model": MODEL,
        "messages": MESSAGES,
        "temperature": settings.LLM_TEMPERATURE,
        "max_tokens": settings.LLM_MAX_TOKENS,
        "stream": False,
        **extra,
    }
    t0 = time.monotonic()
    out: dict = {"case": case_name, "request": extra}
    try:
        resp = httpx.post(
            f"{BASE}/chat/completions",
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=settings.LLM_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001
        out.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        return out

    elapsed = round(time.monotonic() - t0, 2)
    out["http"] = resp.status_code
    out["elapsed_sec"] = elapsed

    if resp.status_code != 200:
        out.update(ok=False, error=resp.text[:400])
        return out

    payload = resp.json()
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    out.update(
        ok=True,
        finish_reason=choice.get("finish_reason"),
        has_reasoning=bool(msg.get("reasoning_content")),
        content_len=len(content),
        usage=payload.get("usage") or {},
    )
    try:
        parsed = json.loads(content)
        out["valid_json"] = isinstance(parsed, dict)
        out["keys"] = sorted(parsed)[:10] if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError):
        out["valid_json"] = False
        out["content_head"] = content[:120]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", help="把结果写成 JSON 文件，便于贴进文档")
    args = ap.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY") or settings.DEEPSEEK_API_KEY
    if not api_key:
        print("未配置 DEEPSEEK_API_KEY，无法实测。")
        print("结论保持为『待实测』—— 这一项不接受猜测，只接受真实响应。")
        return 2

    print(f"模型：{MODEL}\n端点：{BASE}\n")
    results = [probe(api_key, name, extra) for name, extra in CASES]

    for r in results:
        print(f"── {r['case']}")
        if not r.get("ok"):
            print(f"   ❌ HTTP {r.get('http', '-')}: {str(r.get('error'))[:200]}")
            continue
        flag = "✅ JSON 合法" if r.get("valid_json") else "⚠️ 非法 JSON"
        print(
            f"   {flag}｜finish_reason={r.get('finish_reason')}"
            f"｜reasoning={r.get('has_reasoning')}｜{r.get('elapsed_sec')}s"
            f"｜tokens={ (r.get('usage') or {}).get('total_tokens') }"
        )
        if r.get("keys"):
            print(f"   字段：{r['keys']}")
        print()

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已写入 {args.json_out}")

    print("把上面的结论回填到 docs/01-架构设计.md §6.2 与 §9.3 B10。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
