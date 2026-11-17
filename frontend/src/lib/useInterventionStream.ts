/** 干预光流的编排器 —— 把"决策到达"翻译成一次有时序的飞行动画。
 *
 * 它负责的全部事情，就是下面这条时间轴：
 *
 *   决策事件 → [等 state 重拉、宠物换档] → [等换档扫光 1300ms]
 *            → 测量猫头中心与卡片中心 → 飞行 1000ms
 *            → 卡片边缘闪光 → 260ms 后干预卡片出现 → 光流元素销毁
 *
 * 为什么时序必须写在这里而不是组件里：
 * 光流组件只认 progress（0→1），不知道"猫还没变完脸"。把等待放在编排器，
 * 视觉组件就能保持纯函数式，也方便整体取消。
 *
 * 与后端的边界：不请求、不判定、不改 state。失败即静默跳过动画，
 * 卡片照常出现 —— 动画是增益，不是链路的一环。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { DecisionEnvelope, RecordItem } from '../types/api'
import { MOOD_SHIFT_MS } from './petPulse'
import {
  STREAM_TIMING,
  buildStreamPath,
  colorForMood,
  decisionKey,
  resolveTargetRecordId,
  shouldAnimateDecision,
  type StreamColor,
  type StreamFlight,
} from './interventionStream'

interface Options {
  /** 当前档位（来自 /api/state）。用于取色，也用于判断"脸变完了没有" */
  mood: string
  /** /api/state 的版本号。用它等"新情绪已经落定"，而不是干等一个固定延时 */
  stateSeq: number
  /** 当前日程列表（TanStack Query 的 data 或现有 useAppData 的 records 都行） */
  records: RecordItem[]
  /** 起飞瞬间：先把上一张干预卡片收起来，避免旧卡片抢镜 */
  onStart?: () => void
  /** 光点抵达：给目标卡片挂闪光 */
  onHit?: (recordId: string, color: StreamColor) => void
  /** 闪光之后：真正把干预卡片放出来 */
  onReveal: (decision: DecisionEnvelope) => void
}

const sleep = (ms: number) => new Promise<void>((r) => window.setTimeout(r, ms))

export function useInterventionStream({
  mood,
  stateSeq,
  records,
  onStart,
  onHit,
  onReveal,
}: Options) {
  const [flight, setFlight] = useState<StreamFlight | null>(null)

  /* 回调与数据的"最新值"通道：异步流程里不能闭包旧 props。
     放在 effect 里同步而不是渲染期直写 —— 渲染期写 ref 违反 React 的
     纯渲染约定，也会让并发渲染下的值不可预测。 */
  const moodRef = useRef(mood)
  const stateSeqRef = useRef(stateSeq)
  const recordsRef = useRef(records)
  const startRef = useRef(onStart)
  const hitRef = useRef(onHit)
  const revealRef = useRef(onReveal)
  useEffect(() => {
    moodRef.current = mood
    stateSeqRef.current = stateSeq
    recordsRef.current = records
    startRef.current = onStart
    hitRef.current = onHit
    revealRef.current = onReveal
  })

  /** 卸载后不再 setState / 继续往下走 */
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  /** 最近一次"观察到换档"的时刻。
   *
   * 只比较 `mood` 是否变化是不够的：/api/state 可能在 React 副作用之前就已经
   * 刷新完（本机 5ms 级别），等到 submit 执行时新档位早已就位，"有没有变过"
   * 就再也看不出来了。记下时刻，才能算出换档扫光还剩多久没播完。 */
  const shiftAt = useRef<number | null>(null)
  const lastMood = useRef(mood)
  useEffect(() => {
    if (mood !== lastMood.current) {
      lastMood.current = mood
      shiftAt.current = Date.now()
    }
  }, [mood])

  /** 同一时刻只允许一条光流；期间到达的新决策排队等这一条播完 */
  const busy = useRef(false)
  const queue = useRef<DecisionEnvelope[]>([])
  /** 已处理过的决策 id：SSE 广播与 POST 响应会各来一次 */
  const handled = useRef(new Set<string>())
  const seq = useRef(0)
  /** 光点抵达的回调（由组件在 progress 到 1 时调用） */
  const arrival = useRef<(() => void) | null>(null)

  const arrive = useCallback(() => {
    arrival.current?.()
  }, [])

  /** 等 /api/state 落到新版本：SSE 收到决策后紧接着会 load()，
   *  在它回来之前读 mood 只会读到上一档，动画就会抢在换脸之前出发。 */
  const waitStateSettled = useCallback(async (baseSeq: number): Promise<boolean> => {
    const deadline = Date.now() + STREAM_TIMING.moodWaitMs
    while (Date.now() < deadline) {
      if (stateSeqRef.current !== baseSeq) return true
      await sleep(30)
    }
    return false
  }, [])

  const playOne = useCallback(
    async (decision: DecisionEnvelope) => {
      const bailToCard = () => revealRef.current(decision)

      // 不该有光流的决策：模型说"不打扰"、闸门拦截、找不到可指的卡片
      if (!shouldAnimateDecision(decision)) {
        bailToCard()
        return
      }
      const targetId = resolveTargetRecordId(decision, recordsRef.current)
      if (!targetId) {
        bailToCard()
        return
      }
      // 系统级"减少动态效果"：不播光流，直接给卡片
      if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
        bailToCard()
        return
      }

      // 1) 猫先变脸：等 state 落定，再按"换档已经开始了多久"补足剩余扫光时间。
      //    两个竞态都要覆盖：换档可能发生在 submit 之前（state 抢先刷新），
      //    也可能发生在等待期间（后端慢一点）。
      const baseSeq = stateSeqRef.current
      const settled = await waitStateSettled(baseSeq)
      if (!alive.current) return
      const shiftStarted = shiftAt.current
      if (settled || shiftStarted !== null) {
        const elapsed = shiftStarted === null ? Infinity : Date.now() - shiftStarted
        // 扫光还在播 → 补足剩余时长；已经播完 → 只留一帧余量
        const remain = Math.max(0, MOOD_SHIFT_MS - elapsed) + STREAM_TIMING.shiftGapMs
        await sleep(Math.min(remain, MOOD_SHIFT_MS + STREAM_TIMING.shiftGapMs))
      } else {
        await sleep(120)
      }
      if (!alive.current) return

      // 2) 测量锚点。用 DOM 属性而不是 ref：宠物与日程列表在两条独立子树里，
      //    传 ref 会把 App 变成"什么都要知道"的中转站。
      const anchor = document.querySelector<HTMLElement>('[data-pet-core]')
      const card = document.querySelector<HTMLElement>(
        `[data-record-id="${cssEscape(targetId)}"]`,
      )
      if (!anchor || !card) {
        bailToCard()
        return
      }

      // 卡片在视口外时先滚进来，否则光点会飞出屏幕
      const rect0 = card.getBoundingClientRect()
      const offscreen =
        rect0.top < 8 || rect0.bottom > window.innerHeight - 8 || rect0.left < 0
      if (offscreen) {
        card.scrollIntoView({ block: 'center', behavior: 'smooth' })
        await sleep(STREAM_TIMING.scrollSettleMs)
        if (!alive.current) return
      }

      const a = anchor.getBoundingClientRect()
      const b = card.getBoundingClientRect()
      const from = { x: a.left + a.width / 2, y: a.top + a.height / 2 }
      const to = { x: b.left + b.width / 2, y: b.top + b.height / 2 }
      const color = colorForMood(moodRef.current)
      const { d, cubic } = buildStreamPath(from, to)

      // 3) 起飞。先收旧卡片，再挂光流
      startRef.current?.()
      seq.current += 1
      setFlight({
        id: seq.current,
        d,
        cubic,
        from,
        to,
        color,
        duration: STREAM_TIMING.flightMs,
      })

      // 4) 等抵达。组件正常时 onArrive 触发；万一组件被卸载，
      //    兜底计时器保证流程不会永远停在这里。
      const arrived = new Promise<void>((resolve) => {
        arrival.current = resolve
      })
      const safety = window.setTimeout(() => arrive(), STREAM_TIMING.flightMs + 600)
      await arrived
      window.clearTimeout(safety)
      arrival.current = null
      if (!alive.current) return

      // 5) 抵达：先销毁光流 DOM（不残留），再点亮卡片边缘，最后放卡片
      setFlight(null)
      hitRef.current?.(targetId, color)
      await sleep(STREAM_TIMING.revealGapMs)
      if (!alive.current) return
      revealRef.current(decision)
    },
    [arrive, waitStateSettled],
  )

  /** 队列串行：播放期间到达的新决策只保留最新一条，绝不叠第二条光流 */
  const drain = useCallback(async () => {
    if (busy.current) return
    busy.current = true
    try {
      while (alive.current) {
        const next = queue.current.shift()
        if (!next) break
        await playOne(next)
      }
    } finally {
      busy.current = false
    }
  }, [playOne])

  /** 对外唯一入口：SSE 事件与手动决策响应都投到这里，由 id 去重。 */
  const submit = useCallback(
    (decision: DecisionEnvelope | null | undefined) => {
      if (!decision || !alive.current) return
      const key = decisionKey(decision)
      if (handled.current.has(key)) return
      handled.current.add(key)

      // 不需要动画的决策不进队列：直接给卡片，避免挡住后面真正要飞的那条
      if (!shouldAnimateDecision(decision)) {
        revealRef.current(decision)
        return
      }
      if (busy.current) {
        // 只留最新一条，旧的丢弃 —— 光流不允许排长队
        queue.current = [decision]
        return
      }
      queue.current.push(decision)
      void drain()
    },
    [drain],
  )

  return { flight, submit, arrive }
}

/** document.querySelector 的属性值转义；CSS.escape 不可用时退化为最小转义 */
function cssEscape(value: string): string {
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') return CSS.escape(value)
  return value.replace(/["\\]/g, '\\$&')
}
