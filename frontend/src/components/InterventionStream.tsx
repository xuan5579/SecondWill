/** 干预光流 —— 一个纯视觉的 SVG 覆盖层。
 *
 * 它只做一件事：把一条三次贝塞尔曲线和若干光点，按 progress 画在屏幕上。
 * 什么时候起飞、飞多久、飞完干什么，全部由 `useInterventionStream` 决定；
 * 这里没有计时器、没有事件监听，也不触碰任何业务状态。
 *
 * 生命周期：`flight` 为 null 即整个组件返回 null —— 光流不允许常驻。
 */

import { useEffect, useRef, type CSSProperties } from 'react'
import { cubicPoint, type StreamFlight } from '../lib/interventionStream'

interface Props {
  flight: StreamFlight | null
  /** progress 到 1 时回调一次。编排器据此点亮卡片并放出决策卡片。 */
  onArrive: () => void
}

/** 三颗光点：等间距尾随，间距在进度空间里固定，飞起来才像"流"而不是"一串珠子" */
const BEADS = 3
const BEAD_GAP = 0.13
/** 每颗光点自带两道拖尾，位置按进度回退一点点 —— 拖尾是位置差，不是模糊 */
const TAIL_GAP = [0.034, 0.016]

export default function InterventionStream({ flight, onArrive }: Props) {
  const dots = useRef<(SVGCircleElement | null)[]>([])
  const spark = useRef<SVGCircleElement | null>(null)
  const scene = useRef<SVGGElement | null>(null)
  const arriveRef = useRef(onArrive)
  useEffect(() => {
    arriveRef.current = onArrive
  }, [onArrive])

  useEffect(() => {
    if (!flight) return

    let raf = 0
    let done = false
    const t0 = performance.now()
    const { cubic, duration } = flight

    const place = (el: SVGCircleElement | null, x: number, y: number, o: number) => {
      if (!el) return
      el.setAttribute('cx', String(x))
      el.setAttribute('cy', String(y))
      el.setAttribute('opacity', String(Math.max(0, Math.min(1, o))))
    }

    const tick = (now: number) => {
      const t = Math.min(1, Math.max(0, (now - t0) / duration))

      /* 线体：前 12% 渐显、后 12% 渐隐。抵达时线正好消失，
         只剩光点落在卡片上 —— 这样"抵达"才有一个明确的瞬间。 */
      const env = t < 0.12 ? t / 0.12 : t > 0.88 ? Math.max(0, (1 - t) / 0.12) : 1
      scene.current?.setAttribute('opacity', String(env))

      for (let i = 0; i < BEADS; i++) {
        // 头粒子按 t 走，后面的按固定进度差落后；不到起点的不显示
        const lead = t - i * BEAD_GAP
        if (lead <= 0 && i > 0) {
          place(dots.current[i * 3], 0, 0, 0)
          place(dots.current[i * 3 + 1], 0, 0, 0)
          place(dots.current[i * 3 + 2], 0, 0, 0)
          continue
        }
        const p = Math.max(0, lead)
        const head = cubicPoint(cubic, p)
        const decay = 1 - i * 0.24
        // 出发时淡入，避免三颗同时在起点"炸开"
        const emerge = i === 0 ? 1 : Math.min(1, Math.max(0, lead * 8))

        place(dots.current[i * 3], head.x, head.y, 0.98 * decay * emerge)
        TAIL_GAP.forEach((gap, k) => {
          const pt = cubicPoint(cubic, Math.max(0, p - gap))
          place(dots.current[i * 3 + k + 1], pt.x, pt.y, (k === 0 ? 0.34 : 0.13) * decay * emerge)
        })

        if (i === 0 && spark.current) {
          spark.current.setAttribute('cx', String(head.x))
          spark.current.setAttribute('cy', String(head.y))
          spark.current.setAttribute('opacity', String(0.55 * emerge * env))
        }
      }

      if (t >= 1) {
        if (!done) {
          done = true
          arriveRef.current()
        }
        return
      }
      raf = requestAnimationFrame(tick)
    }

    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [flight])

  if (!flight) return null

  const c = flight.color
  const uid = flight.id

  return (
    <svg
      className="iv-stream"
      aria-hidden="true"
      focusable="false"
      style={{ '--iv-glow': c.glow } as CSSProperties}
    >
      <defs>
        {/* 线体渐变沿"起点→终点"的真实方向铺，颜色才跟飞行方向一致 */}
        <linearGradient
          id={`iv-line-${uid}`}
          gradientUnits="userSpaceOnUse"
          x1={flight.from.x}
          y1={flight.from.y}
          x2={flight.to.x}
          y2={flight.to.y}
        >
          <stop offset="0%" stopColor={c.line} stopOpacity="0" />
          <stop offset="16%" stopColor={c.line} stopOpacity="0.66" />
          <stop offset="52%" stopColor={c.core} stopOpacity="0.52" />
          <stop offset="86%" stopColor={c.core} stopOpacity="0.38" />
          <stop offset="100%" stopColor="#22d3ee" stopOpacity="0.24" />
        </linearGradient>
        {/* 第二层细虚线：紫→青，缓慢流动。它是"全息数据流"的质地，
            压在实线下面，不抢档位主色 */}
        <linearGradient
          id={`iv-dash-${uid}`}
          gradientUnits="userSpaceOnUse"
          x1={flight.from.x}
          y1={flight.from.y}
          x2={flight.to.x}
          y2={flight.to.y}
        >
          <stop offset="0%" stopColor="#8b7cf6" stopOpacity="0" />
          <stop offset="34%" stopColor="#8b7cf6" stopOpacity="0.34" />
          <stop offset="72%" stopColor="#22d3ee" stopOpacity="0.28" />
          <stop offset="100%" stopColor="#22d3ee" stopOpacity="0" />
        </linearGradient>
        <radialGradient id={`iv-spark-${uid}`}>
          <stop offset="0%" stopColor="#ffffff" stopOpacity="0.7" />
          <stop offset="42%" stopColor={c.core} stopOpacity="0.34" />
          <stop offset="100%" stopColor={c.core} stopOpacity="0" />
        </radialGradient>
      </defs>

      <g ref={scene} opacity={0}>
        <path className="iv-stream-bleed" d={flight.d} stroke={`url(#iv-line-${uid})`} />
        <path className="iv-stream-line" d={flight.d} stroke={`url(#iv-line-${uid})`} />
        <path className="iv-stream-dash" d={flight.d} stroke={`url(#iv-dash-${uid})`} />
      </g>

      <circle ref={spark} className="iv-spark" r={14} fill={`url(#iv-spark-${uid})`} opacity={0} />

      {Array.from({ length: BEADS }, (_, i) => (
        <g key={i}>
          <circle
            ref={(el) => {
              dots.current[i * 3] = el
            }}
            className="iv-dot"
            r={2.4}
            fill={c.core}
            opacity={0}
          />
          <circle
            ref={(el) => {
              dots.current[i * 3 + 1] = el
            }}
            className="iv-dot"
            r={5}
            fill={c.line}
            opacity={0}
          />
          <circle
            ref={(el) => {
              dots.current[i * 3 + 2] = el
            }}
            className="iv-dot"
            r={8.6}
            fill={c.line}
            opacity={0}
          />
        </g>
      ))}
    </svg>
  )
}
