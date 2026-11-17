/** 今日情绪轨迹 —— 前端本地的轻量采样（A2 点击浮层的数据源）。
 *
 * 诚实声明：后端没有 mood 历史表，这里**不做假数据**。
 * 每次前端拉到新的 /api/state，若档位或 energy 有实质变化，就记一个点，
 * 存 localStorage（按天保留 3 天、总量封顶，防止无限增长）。
 * 浮层标题因此写"本页记录"—— 它是这台浏览器今天真实观察到的变化，
 * 不是编出来的历史曲线。演示时点开它，每完成一个任务就会多一个点。
 */

export interface TrailPoint {
  /** 采样时刻（ms epoch） */
  t: number
  mood: string
  /** 0 ~ 1，与 pet.energy 一致 */
  energy: number
}

const KEY = 'secondwill.pet.trail.v1'
const KEEP_DAYS = 3
const MAX_POINTS = 200
/** 同一点位的最短间隔：短于它的采样视作同一次刷新，不记 */
const MIN_GAP_MS = 30_000
/** energy 变化小于该值视为没变（energy 本身是离散重算的结果） */
const ENERGY_EPS = 0.03

function isPoint(v: unknown): v is TrailPoint {
  if (typeof v !== 'object' || v === null) return false
  const p = v as Record<string, unknown>
  return typeof p.t === 'number' && typeof p.mood === 'string' && typeof p.energy === 'number'
}

export function readTrail(): TrailPoint[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(isPoint)
  } catch {
    // localStorage 被禁用 / 数据损坏：轨迹是纯增益功能，静默放弃
    return []
  }
}

function writeTrail(points: TrailPoint[]): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(points))
  } catch {
    // 同上：写不进去就算了，不影响任何主流程
  }
}

/** 采一个点。去重规则：与上一点同档且 energy 变化 < 3%，或间隔 < 30s。 */
export function recordTrailPoint(mood: string, energy: number): void {
  if (typeof window === 'undefined') return
  const trail = readTrail()
  const now = Date.now()
  const last = trail[trail.length - 1]
  if (last) {
    if (now - last.t < MIN_GAP_MS) return
    if (last.mood === mood && Math.abs(last.energy - energy) < ENERGY_EPS) return
  }
  trail.push({ t: now, mood, energy })
  // 只留最近 3 天，且总量封顶（保留尾部 = 最近的点）
  const horizon = now - KEEP_DAYS * 86_400_000
  const kept = trail.filter((p) => p.t >= horizon).slice(-MAX_POINTS)
  writeTrail(kept)
}

/** 今日零点（本地时区）。轨迹图只画今天的点。 */
export function startOfToday(now = Date.now()): number {
  const d = new Date(now)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}
