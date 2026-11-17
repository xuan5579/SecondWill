/** 干预光流 —— 决策事件到日程卡片之间的因果可视化。
 *
 * 它只画"决策已经发生"这件事，不参与任何判定：
 * 情绪、冲突、闸门全在后端算完，这里只是把那条因果链变成一束光。
 *
 * 三条硬约束（改动前请先读完）：
 * 1. **时序**：光流必须等宠物换档动画播完才出发。猫先变脸，光再动 ——
 *    顺序反了，观众会读成"光会让猫变脸"。
 * 2. **一次性**：结束即摘除，不常驻、不叠层。同一事件重复到达由
 *    `useInterventionStream` 按 id 去重。
 * 3. **不改状态**：本文件不读也不写 pet / decision 的任何判定字段，
 *    只从 payload 里取 `decision_type` 与 `target_record_id` 用于"要不要播 / 飞到哪"。
 */

import type { DecisionEnvelope, RecordItem } from '../types/api'

export interface StreamColor {
  /** 线体主色（取自六档 --fur-mid） */
  line: string
  /** 光点/亮芯色（取自六档 --iris） */
  core: string
  /** 抵达时卡片边缘的闪光色（带透明度） */
  glow: string
}

/** 六档光流配色。数值与 index.css 中 `.cat-*` 档位的
 *  `--fur-mid` / `--iris` 一一对应 —— 光流与光核讲同一种颜色语言。
 * 用字面量而不是 var()：SVG 渐变 stop 需要在 JS 里按档位取值。 */
export const MOOD_STREAM_COLOR: Record<string, StreamColor> = {
  alert: { line: '#c9a15e', core: '#e8cf9a', glow: 'rgba(201, 161, 94, 0.55)' },
  normal: { line: '#6e7ff3', core: '#9ed2f0', glow: 'rgba(110, 127, 243, 0.55)' },
  tired: { line: '#8581a6', core: '#aaa5cd', glow: 'rgba(133, 129, 166, 0.45)' },
  guilty: { line: '#b0769f', core: '#d1a4c4', glow: 'rgba(176, 118, 159, 0.5)' },
  great: { line: '#5aa89b', core: '#8fcfc4', glow: 'rgba(90, 168, 155, 0.5)' },
  dormant: { line: '#5f6b85', core: '#7c89a5', glow: 'rgba(95, 107, 133, 0.38)' },
}

const FALLBACK_COLOR: StreamColor = {
  line: '#8b7cf6',
  core: '#a5b4fc',
  glow: 'rgba(139, 124, 246, 0.5)',
}

export function colorForMood(mood: string): StreamColor {
  return MOOD_STREAM_COLOR[mood] ?? FALLBACK_COLOR
}

export interface StreamTiming {
  /** 等 state 重新拉取、宠物真正换档的最长等待 */
  moodWaitMs: number
  /** 换档扫光结束后再留的余量，避免"光与扫光同时出发" */
  shiftGapMs: number
  /** 光点从猫头飞到卡片的时间 */
  flightMs: number
  /** 卡片边缘闪光持续时间（与 CSS 动画一致） */
  hitMs: number
  /** 闪光亮起后，干预卡片再出现的延迟 */
  revealGapMs: number
  /** 目标卡片在视口外时，滚动到位后的静置时间 */
  scrollSettleMs: number
}

export const STREAM_TIMING: StreamTiming = {
  moodWaitMs: 600,
  shiftGapMs: 160,
  flightMs: 1000,
  hitMs: 900,
  revealGapMs: 260,
  scrollSettleMs: 320,
}

export interface Point {
  x: number
  y: number
}

/** 三次贝塞尔：p0 起点、p1/p2 控制点、p3 终点。 */
export type Cubic = [Point, Point, Point, Point]

/** 从猫头中心飞向卡片中心的路径。
 *
 * 形状刻意做成"先向上抬、再平缓落"的拱形：
 * 直线会像一条指示线，纯 S 形又太像流程图连线。
 * 拱顶高度随水平距离缩放，窄屏时不会顶出视口。 */
export function buildStreamPath(from: Point, to: Point): { d: string; cubic: Cubic } {
  const dx = to.x - from.x
  const dy = to.y - from.y
  const lift = Math.min(96, Math.max(38, Math.abs(dx) * 0.14))
  const drop = Math.min(72, Math.abs(dy) * 0.22 + 26)

  const p1: Point = { x: from.x + dx * 0.3, y: from.y + dy * 0.04 - lift }
  const p2: Point = { x: from.x + dx * 0.74, y: to.y - drop }
  const cubic: Cubic = [from, p1, p2, to]

  const d = `M ${round(from.x)} ${round(from.y)} C ${round(p1.x)} ${round(p1.y)}, ${round(p2.x)} ${round(p2.y)}, ${round(to.x)} ${round(to.y)}`
  return { d, cubic }
}

function round(n: number): number {
  return Math.round(n * 10) / 10
}

/** 贝塞尔求点 —— 光点位置由它算，不依赖 DOM 的 getPointAtLength。 */
export function cubicPoint([p0, p1, p2, p3]: Cubic, t: number): Point {
  const u = 1 - t
  const a = u * u * u
  const b = 3 * u * u * t
  const c = 3 * u * t * t
  const d = t * t * t
  return {
    x: a * p0.x + b * p1.x + c * p2.x + d * p3.x,
    y: a * p0.y + b * p1.y + c * p2.y + d * p3.y,
  }
}

/** 一次飞行的全部静态参数。组件只按 progress 采样，不再读任何外部状态。 */
export interface StreamFlight {
  /** 单调递增：同一次挂载内换 key 用，也用于生成渐变 id */
  id: number
  d: string
  cubic: Cubic
  from: Point
  to: Point
  color: StreamColor
  duration: number
}

/** 这次决策该不该有光流。
 *
 * 三种情况直接放行到卡片、不做动画：
 * - `delivered === false`：节奏闸门/降权拦下了，后端没落干预记录，没有卡片可指；
 * - `no_action`：模型明确说"这次不打扰"，画一条飞向卡片的线是自相矛盾；
 * - 没有 intervention：nudge 之外的半成品响应，没有可展示的实体。
 */
export function shouldAnimateDecision(decision: DecisionEnvelope): boolean {
  if (decision.delivered === false) return false
  const payload = decision.payload
  if (!payload || !payload.intervention) return false
  if (payload.decision_type === 'no_action') return false
  return true
}

/** 光流终点：第一条可落到真实卡片上的建议。
 *
 * 契约里 `target_record_id` 可能指向已被删除/筛选掉的记录 ——
 * 找不到对应卡片时必须放弃动画（飞向空白比不飞更糟），
 * 所以这里同时要求 id 在 `records` 里存在。 */
export function resolveTargetRecordId(
  decision: DecisionEnvelope,
  records: RecordItem[],
): string | null {
  const ids = new Set(records.map((r) => r.id))
  for (const s of decision.payload?.suggestions ?? []) {
    if (s.target_record_id && ids.has(s.target_record_id)) return s.target_record_id
  }
  return null
}

/** 去重键：同一条干预可能同时经 SSE 广播与 POST 响应抵达。
 *
 * 优先用 intervention_id；万一落库失败没有 id，就用"类型 + 摘要 + 目标记录"
 * 拼一个内容指纹 —— 广播与响应两条路的字段不完全一致（一个带 request_id、
 * 一个不带），拿 request_id 做键反而会漏掉重复。
 */
export function decisionKey(decision: DecisionEnvelope): string {
  if (decision.intervention_id) return decision.intervention_id
  const payload = decision.payload
  const targets = (payload?.suggestions ?? []).map((s) => s.target_record_id).join(',')
  return `${payload?.decision_type ?? 'unknown'}|${payload?.summary ?? ''}|${targets}`
}
