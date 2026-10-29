"""LLM 输出校验 —— **强制路径**，不是可选增强（文档 §4.5 / §6.2）。

DeepSeek 的 `json_object` 模式只保证"输出是合法 JSON"，
不保证字段名、类型、枚举、引用完整性。所以这里有 10 条规则，
分两类处理：

- **规则 1–4（结构性）** → 判 `L1`，带校验错误原文**重试**，最多 2 次；
- **规则 5–7、9–10（业务性）** → 就地修复：丢弃违规建议、截断超长文本、截断超量条目；
- **规则 8（应用后不得产生新硬冲突）** → 判 `L2`；若全部建议都不可行则转 L0 规则降级。

一句话概括这个文件的立场：**模型的输出是"待验证的提案"，不是"可信数据"。**

## 关于枚举（规则 3）

枚举跑偏是实测里**最高频**的失败：6 次裸调用 3 次栽在
`reasoning_tags` / `actions[].kind` 上写了同义但非法的词。所以这里分两层拦：

1. **prompt 先讲清楚**（主力）：把每个枚举字段的允许值显式列出来，并给
   "不要这样写 → 应该这样写"的对照（`prompts/decision.py` 的枚举段）。
2. **归一化兜底**（`llm/normalize.py`）：只接语义无歧义的同义词，
   拿不准的一律不映射 —— 猜错的代价（尤其是把 `dismiss` 当成 `mute`，
   会让用户被永久屏蔽一类建议）远大于"失败并重试一次"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Iterable, Sequence

from app.core.constants import (
    DECISION_MESSAGE_MAX_LEN,
    DECISION_PET_LINE_MAX_LEN,
    DECISION_RATIONALE_MAX_LEN,
    DECISION_SUMMARY_MAX_LEN,
    DECISION_TITLE_MAX_LEN,
)
from app.core.logging import get_logger
from app.core.time import utc_to_local
from app.llm.contract import DecisionPayload, DecisionRequest, Suggestion
from app.llm.normalize import normalize_enums
from app.services.conflict import TimeWindow, detect_conflicts

logger = get_logger(__name__)

#: 校验结论级别，与文档 §6.1 的降级分级一一对应
LEVEL_OK = "ok"
LEVEL_L1 = "L1"   # 结构性问题 -> 重试
LEVEL_L2 = "L2"   # 业务性问题 -> 丢弃不可行部分；全废则转 L0


@dataclass
class Issue:
    rule: int
    level: str
    target: str
    detail: str

    def __str__(self) -> str:
        return f"[规则{self.rule}/{self.level}] {self.target}: {self.detail}"


@dataclass
class ValidationResult:
    ok: bool
    level: str
    payload: DecisionPayload | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def error_text(self) -> str:
        """回传给模型的校验错误原文。

        与其说"你错了"，不如把**具体哪条字段错在哪**讲清楚 ——
        实测这能让第 2 次尝试的成功率显著高于含糊的重试提示。
        """
        return "\n".join(f"- {i}" for i in self.issues)

    def add(self, rule: int, level: str, target: str, detail: str) -> None:
        self.issues.append(Issue(rule, level, target, detail))


# ==================================================================
# 规则 10 的文本截断：必须在 Pydantic 校验**之前**执行
# ==================================================================

_TEXT_LIMITS: tuple[tuple[tuple[str, ...], str, int], ...] = (
    (("summary",), "summary", DECISION_SUMMARY_MAX_LEN),
    (("intervention", "title"), "intervention.title", DECISION_TITLE_MAX_LEN),
    (("intervention", "message"), "intervention.message", DECISION_MESSAGE_MAX_LEN),
    (("intervention", "pet_line"), "intervention.pet_line", DECISION_PET_LINE_MAX_LEN),
)


def _truncate_texts(raw: dict) -> tuple[dict, list[Issue]]:
    """规则 10：超长文本一律截断，不判失败。

    模型啰嗦是风格问题不是错误，为它浪费一次重试不划算。
    """
    issues: list[Issue] = []
    for path, name, limit in _TEXT_LIMITS:
        node: Any = raw
        for key in path[:-1]:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if not isinstance(node, dict):
            continue
        value = node.get(path[-1])
        if isinstance(value, str) and len(value) > limit:
            node[path[-1]] = value[:limit]
            issues.append(Issue(10, LEVEL_OK, name, f"超长已截断至 {limit} 字"))

    suggestions = raw.get("suggestions")
    if isinstance(suggestions, list):
        for idx, s in enumerate(suggestions):
            if isinstance(s, dict) and isinstance(s.get("rationale"), str):
                if len(s["rationale"]) > DECISION_RATIONALE_MAX_LEN:
                    s["rationale"] = s["rationale"][:DECISION_RATIONALE_MAX_LEN]
                    issues.append(
                        Issue(10, LEVEL_OK, f"suggestions[{idx}].rationale", "超长已截断")
                    )
    return raw, issues


# ==================================================================
# 规则 6：工作时间窗
# ==================================================================


def _parse_hhmm(value: str, default: time) -> time:
    try:
        hour, minute = value.split(":")[:2]
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return default


def within_working_hours(
    dt: datetime | None,
    day_start: time,
    day_end: time,
) -> bool:
    """本地时刻是否落在工作时间内。`None` 视为不检查（如 drop 类建议）。"""
    if dt is None:
        return True
    local = utc_to_local(dt)
    return day_start <= local.time() <= day_end


# ==================================================================
# 规则 8：应用建议后重跑冲突检测
# ==================================================================


@dataclass
class _SimRecord:
    """喂给 `detect_conflicts` 的适配器。

    冲突检测是纯函数且只认 `id/start_at/end_at/status`，
    所以这里不必惊动 ORM —— 用鸭子类型即可。
    """

    id: str
    start_at: datetime | None
    end_at: datetime | None
    status: str = "pending"
    type: str = "schedule"
    title: str = ""


def to_sim_records(records: Iterable) -> list[_SimRecord]:
    """契约里的记录 -> 冲突检测能吃的记录。两种输入都支持（ORM 与契约对象）。"""
    out: list[_SimRecord] = []
    for r in records:
        start = getattr(r, "start_at", None) or getattr(r, "start", None)
        end = getattr(r, "end_at", None) or getattr(r, "end", None)
        out.append(
            _SimRecord(
                id=str(getattr(r, "id")),
                start_at=start,
                end_at=end,
                status=getattr(r, "status", "pending"),
                title=getattr(r, "title", ""),
            )
        )
    return out


def apply_suggestions(
    sims: Sequence[_SimRecord], suggestions: Sequence[Suggestion]
) -> list[_SimRecord]:
    """把建议应用到记录副本上。

    - `move` / `shrink`：替换时间区间
    - `drop`           ：置为 `dropped`（冲突检测会因此忽略它，语义就是"别做了"）
    """
    by_id = {s.id: s for s in sims}
    out: list[_SimRecord] = []
    applied: dict[str, Suggestion] = {}
    for s in suggestions:
        if s.target_record_id in by_id:
            applied.setdefault(s.target_record_id, s)

    for sim in sims:
        s = applied.get(sim.id)
        if s is None:
            out.append(sim)
            continue
        if s.kind == "drop":
            out.append(_SimRecord(sim.id, sim.start_at, sim.end_at, "dropped", title=sim.title))
            continue
        # move / shrink：只有给了新时间才替换，否则保持原样（模型没给时间就不动）
        start = s.proposed_start or sim.start_at
        end = s.proposed_end or sim.end_at
        out.append(_SimRecord(sim.id, start, end, sim.status, title=sim.title))
    return out


def _hard_conflict_keys(sims: Sequence[_SimRecord], horizon: TimeWindow) -> set:
    """当前 hard 冲突的"身份集合"。

    用**差集**而不是计数来判"新增"：原本就撞车的两条，模型只解开其中一对时
    剩下的那对不该被算作它引入的新问题 —— 否则模型给出的部分解会被整包否掉，
    用户反而什么都看不到。
    """
    if horizon.start >= horizon.end:
        return set()
    return {
        (c.kind, tuple(sorted(c.record_ids)))
        for c in detect_conflicts(sims, horizon)
        if c.hard
    }


def _horizon_of(req: DecisionRequest, sims: Sequence[_SimRecord]) -> TimeWindow:
    """校验用的时间窗。

    优先用请求里给定的 horizon；没有时用"所有记录的最小/最大边界"外扩一小时，
    这样模型把日程挪到窗口外也不会逃过冲突检测。
    """
    if req.horizon is not None:
        return TimeWindow(req.horizon.start, req.horizon.end)
    bounds = [s.start_at for s in sims if s.start_at] + [s.end_at for s in sims if s.end_at]
    if not bounds:
        return TimeWindow(datetime.min, datetime.min)
    from datetime import timedelta

    return TimeWindow(min(bounds) - timedelta(hours=1), max(bounds) + timedelta(hours=1))


# ==================================================================
# 主入口
# ==================================================================


def validate_payload(
    raw: Any,
    req: DecisionRequest,
) -> ValidationResult:
    """跑完 10 条规则。返回的 `level` 决定编排器下一步。"""
    result = ValidationResult(ok=False, level=LEVEL_L1)

    # ---------- 规则 1：是 JSON 对象 ----------
    if not isinstance(raw, dict):
        result.add(1, LEVEL_L1, "root", f"期望 JSON 对象，实际 {type(raw).__name__}")
        return result

    # ---------- 规则 10：超长文本先截断 ----------
    raw, trunc_issues = _truncate_texts(raw)
    result.issues.extend(trunc_issues)

    # ---------- 规则 3 兜底：枚举同义词归一化 ----------
    # 主力是 prompt 里显式列出的允许值（prompts/decision.py 的枚举段）；
    # 这里只接漏网的那一小部分。归一化**不改变成功/失败的判定方向**：
    # 能救的救回来（记 LEVEL_OK 的 note），救不了的照旧判 L1 并把原文回传重试。
    raw, norm_notes = normalize_enums(raw)
    for path, original, canonical in norm_notes:
        result.add(3, LEVEL_OK, path, f"枚举归一化：{original!r} → {canonical!r}")

    # ---------- 规则 2、3、4：结构 / 枚举 / confidence ----------
    try:
        payload = DecisionPayload.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - Pydantic 的异常类型不值得逐个列
        from pydantic import ValidationError as PydanticValidationError

        if isinstance(exc, PydanticValidationError):
            for err in exc.errors()[:5]:
                loc = ".".join(str(p) for p in err.get("loc", ()))
                etype = str(err.get("type"))
                if "confidence" in loc:
                    rule = 4
                elif "literal" in etype or "enum" in etype:
                    rule = 3
                else:
                    rule = 2
                result.add(rule, LEVEL_L1, loc or "root", err.get("msg", "字段不合法"))
        else:
            result.add(2, LEVEL_L1, "root", str(exc)[:200])
        return result

    # 结构 OK 之后才做业务护栏
    result.level = LEVEL_OK

    known_ids = {r.id for r in req.records}
    immutable = set(req.constraints.immutable_record_ids)
    day_start = _parse_hhmm(req.constraints.working_hours.get("start", "08:00"), time(8, 0))
    day_end = _parse_hhmm(req.constraints.working_hours.get("end", "23:00"), time(23, 0))

    kept: list[Suggestion] = []
    for idx, s in enumerate(payload.suggestions):
        target = f"suggestions[{idx}]"

        # ---------- 规则 5：目标必须存在 ----------
        if s.target_record_id not in known_ids:
            result.add(5, LEVEL_OK, target, f"target_record_id 不存在：{s.target_record_id}")
            continue

        # ---------- 规则 7：不得改动不可变日程 ----------
        if s.target_record_id in immutable:
            result.add(7, LEVEL_OK, target, f"命中不可变日程：{s.target_record_id}")
            continue

        # ---------- 规则 6：时间可行（区间有效 + 落在工作时间内）----------
        if s.kind in ("move", "shrink"):
            if s.proposed_start is None or s.proposed_end is None:
                result.add(6, LEVEL_OK, target, f"{s.kind} 建议缺少 proposed_start/end")
                continue
            if s.proposed_end <= s.proposed_start:
                result.add(6, LEVEL_OK, target, "proposed_end 不晚于 proposed_start")
                continue
            if not (
                within_working_hours(s.proposed_start, day_start, day_end)
                and within_working_hours(s.proposed_end, day_start, day_end)
            ):
                result.add(
                    6, LEVEL_OK, target,
                    f"时间落在工作时间外（{day_start:%H:%M}~{day_end:%H:%M}）",
                )
                continue

        kept.append(s)

    # ---------- 规则 9：条数上限 ----------
    max_n = req.constraints.max_suggestions
    if len(kept) > max_n:
        result.add(9, LEVEL_OK, "suggestions", f"超过 {max_n} 条，已截断")
        kept = kept[:max_n]

    # ---------- 规则 8：应用后不得产生（残留）硬冲突 ----------
    sims = to_sim_records(req.records)
    horizon = _horizon_of(req, sims)
    baseline_hard = _hard_conflict_keys(sims, horizon)
    accepted: list[Suggestion] = []
    if kept:
        for s in kept:
            trial = apply_suggestions(sims, accepted + [s])
            after = _hard_conflict_keys(trial, horizon)
            new_hard = after - baseline_hard
            # 判据不只是"别引入新的"，还要求**确实有改善**：
            # 把一条日程挪到"仍然撞车"的位置，等于什么都没做，
            # 那种建议出现在卡片上比没有建议更糟。
            no_progress = bool(baseline_hard) and len(after) >= len(baseline_hard)
            if new_hard or no_progress:
                why = (
                    f"会引入新的硬冲突 {sorted(next(iter(new_hard))[1])[:2]}"
                    if new_hard
                    else "应用后硬冲突没有减少（等于没解决问题）"
                )
                result.add(8, LEVEL_L2, f"suggestion:{s.target_record_id}", f"{why}，已丢弃")
                continue
            accepted.append(s)

        if not accepted:
            # 一条都不剩 = 方案整体不可行 -> L2，由编排器转 L0 规则降级
            result.level = LEVEL_L2
            result.ok = False
            payload.suggestions = []
            result.payload = payload
            return result

    payload.suggestions = accepted
    result.payload = payload
    result.ok = True
    result.level = LEVEL_OK if not any(i.level == LEVEL_L2 for i in result.issues) else LEVEL_L2
    return result
