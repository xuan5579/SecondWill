import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import CatAvatar, { MOOD_LIST, type Mood } from './CatAvatar'
import type { PetPulse, PetPulseKind } from '../lib/petPulse'
import { readTrail, recordTrailPoint, startOfToday, type TrailPoint } from '../lib/moodTrail'
import type { AppState } from '../types/api'

const MOOD_LABEL: Record<string, { name: string; desc: string }> = {
  alert: { name: '警觉', desc: '有安排会撞车' },
  dormant: { name: '休眠', desc: '好几天没动了' },
  tired: { name: '疲惫', desc: '积压了几天' },
  guilty: { name: '愧疚', desc: '欠得有点多' },
  great: { name: '很好', desc: '节奏在线' },
  normal: { name: '平常', desc: '一切如常' },
}

/** 轨迹图配色：直接引用六档的主色，图与宠物讲同一种颜色语言 */
const MOOD_COLOR: Record<string, string> = {
  alert: '#c9a15e',
  dormant: '#5f6b85',
  tired: '#7b7899',
  guilty: '#b0769f',
  great: '#5aa89b',
  normal: '#6e7ff3',
}

// ---------------------------------------------------------------------------
// ⚠️ 拍摄用 DEBUG 开关 —— 只覆盖本组件的显示，不参与任何后端计算。
//
// 用法：地址栏加 `?pet=alert`（六档之一：alert/dormant/tired/guilty/great/normal）。
//
// 为什么要有它：视频第 4 章要把六档表情并排拍，靠"凑数据触发"既慢又不可控 ——
// 真要凑出 dormant 得让库空好几天。这里把"显示哪一档"变成可指定的参数。
//
// 边界（重要，改动前请读完）：
// - 只改本组件读到的 mood 字符串。后端 `pet.compute()`、`/api/state` 的返回值、
//   冲突计数、「判定依据」全部照旧，不受这个参数影响；
// - 生效时画面上会显示「DEBUG 覆盖」角标，避免被误认成真实判定结果；
// - 不进任何业务逻辑，不参与除渲染以外的任何路径。
// ---------------------------------------------------------------------------
const DEBUG_MOODS = ['alert', 'dormant', 'tired', 'guilty', 'great', 'normal'] as const

/** 覆盖模式下的猫台词。真实判定的台词来自后端，覆盖时继续挂在那条台词上，
 *  画面就成了"档位是疲惫、嘴里说的却是今天的安排"，跟 DEBUG 角标自相矛盾。
 *  所以覆盖时换成与各档相符的演示台词。 */
const DEBUG_LINE: Record<string, string> = {
  alert: '有安排撞在一起了，要不要现在挪开？',
  dormant: '好几天没动过了，挑一件最小的先开始？',
  tired: '积压几天了，先把最旧那条处理掉？',
  guilty: '拖了这么多件，心里不踏实。先还一件？',
  great: '节奏很好，保持住。',
  normal: '一切如常，继续。',
}

function debugMoodOverride(): string | null {
  if (typeof window === 'undefined') return null
  const v = new URLSearchParams(window.location.search).get('pet')
  return v && (DEBUG_MOODS as readonly string[]).includes(v) ? v : null
}

/** 触屏判定 —— 决定"点一下光核"落在哪个交互上。
 *
 * 桌面：悬停出气泡，点击开情绪轨迹（原有行为，不动）；
 * 触屏：没有 hover，一次点按只做一件事 —— 开合气泡，
 *       轨迹改从气泡里的按钮进。不这么分，一次点按会同时弹出两层。
 */
function isCoarsePointer(): boolean {
  if (typeof window === 'undefined') return false
  return window.matchMedia('(hover: none), (pointer: coarse)').matches
}

// ---------------------------------------------------------------------------
// 等待的形状 —— 模型往返实测 6~9 秒（采样见 docs/12-LLM决策与降级.md）。
//
// 这 9 秒里画面上原本只有一行静态字，观众会以为程序卡死。
// 这里补三样：阶段文案、流逝秒数、一条进度条。
//
// 诚实的边界（重要）：前端拿不到模型内部的真实阶段，下面几句是按实测耗时
// 切出来的**节奏**，不是后端回传的进度。所以进度条永远停在 92%，一旦超过
// 预期时长就改成"仍在等待模型返回"—— 把没完成的等待演成完成，比不演更坏。
// ---------------------------------------------------------------------------
/* 预算 12s：实测 DeepSeek 往返 9~11.5s（含一次重试会到 12s）。
 * 之前写 9s，正常调用都会越过阈值 —— "比平时久"变成常态，
 * 那句提示就废了。宁可宽一点，让琥珀色真的只代表"这次不正常"。 */
const THINK_BUDGET_MS = 12000

const THINK_STAGES: { at: number; text: string }[] = [
  { at: 0, text: '正在读取今天的日程与冲突…' },
  { at: 2500, text: '正在权衡优先级与剩余时间…' },
  { at: 5500, text: '正在生成重排方案…' },
  { at: 9000, text: '等待模型返回…' },
]

/** 等待期进度。
 *
 * 刻意做成独立组件：计时器每 100ms 一次 setState，如果放在 PetPanel 里，
 * 整棵宠物树（含 200+ 节点的光核 SVG）会跟着重渲染 90 次 —— 扫描动画会掉帧。
 * 独立出来只有这一小块在动。
 */
function ThinkProgress() {
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    const t0 = performance.now()
    const timer = window.setInterval(() => setElapsed(performance.now() - t0), 100)
    return () => window.clearInterval(timer)
  }, [])

  const stage = THINK_STAGES.filter((s) => elapsed >= s.at).pop() ?? THINK_STAGES[0]
  const overrun = elapsed > THINK_BUDGET_MS
  /* 封顶 92%：进度条填满就等于宣称"已经完成"，而这里只是"还在跑"。 */
  const pct = Math.min(92, (elapsed / THINK_BUDGET_MS) * 100)

  return (
    <>
      <p className="pet-line is-thinking-line">{stage.text}</p>
      <div className={`think-progress${overrun ? ' is-overrun' : ''}`}>
        <div
          className="think-progress-track"
          role="progressbar"
          aria-label="决策进行中"
          aria-valuenow={Math.round(pct)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <i style={{ width: `${pct}%` }} />
        </div>
        <p className="think-progress-meta">
          <span className="num">{(elapsed / 1000).toFixed(1)}s</span>
          <span>{overrun ? '比平时久 · 仍在等待模型返回' : '通常 9–12 秒'}</span>
        </p>
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// 指标可视化 —— 让"拖延指数 0.42"变成"看得见的一截条"。
//
// 评委不会逐字读四个数字的含义，但会立刻看懂"这条快满了"。
// 两处细节：
// ① 数字补间只在**值发生变化**时播；首次拿到数据直接落位。
//    否则页面一开就四个数从 0 涨上去，像加载动画，反而显得数据是假的；
// ② 条宽用 CSS transition，数字用 JS 补间 —— 纯 CSS 的 transition 对
//    textContent 无效，只能靠 rAF 逐帧写。
// ---------------------------------------------------------------------------
const TWEEN_MS = 320

function fmtNum(v: number, digits: number): string {
  return digits > 0 ? v.toFixed(digits) : String(Math.round(v))
}

/** 数字补间：逐帧直接写 textContent，不走 setState。
 *
 * 每帧 setState 会让外面四格一起重渲染 20 次 —— 和光流、思考计时器是同一个理由。
 *
 * 两个必须这么写的理由，都是踩过的：
 * ① 不用 setState，只用 rAF 写 DOM。
 * ② **span 不交给 React 渲染 children** —— 这是关键。如果写成
 *    `<span>{fmt(value)}</span>`，值一变 React 会先把它写成一帧终值，
 *    紧接着 effect 里的补间又把文本倒回起点再爬上去，肉眼看到的就是
 *    "数字突然跳一下，然后才动"。React 那边 children 恒为 null 就不会再碰这个节点，
 *    文本完全归 rAF 管。用 useLayoutEffect 是为了首帧也由我们写，避免空白一帧。
 */
function TweenNumber({
  value,
  digits = 0,
}: {
  value: number | null
  digits?: number
}) {
  const ref = useRef<HTMLSpanElement>(null)
  /** 上一次真正显示出来的值。补间被打断时从这里接着走，不跳回更早的终点。 */
  const cur = useRef<number | null>(value)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    if (value === null) {
      cur.current = null
      el.textContent = '—'
      return
    }
    const from = cur.current
    const land = () => {
      cur.current = value
      el.textContent = fmtNum(value, digits)
    }
    // 首次拿到数据 / 值没变 / 用户要求减少动效：直接落位。
    // 首次不补间很重要 —— 四个数字从 0 一起涨上去像加载动画，反而显得数据是编的。
    if (
      from === null ||
      from === value ||
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    ) {
      land()
      return
    }
    let raf = 0
    const t0 = performance.now()
    const step = (t: number) => {
      const p = Math.min(1, (t - t0) / TWEEN_MS)
      const v = from + (value - from) * (1 - Math.pow(1 - p, 3))
      cur.current = v
      el.textContent = fmtNum(v, digits)
      if (p < 1) raf = requestAnimationFrame(step)
      else land()
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [value, digits])

  return <span ref={ref} />
}

type Tone = 'low' | 'mid' | 'high' | 'flat'

/** 区间配色：低=青（无事）、中=琥珀（留意）、高=橙红（要处理）。
 *  和卡片冲突色、宠物警戒档同属一套"风险色"，不另起一套颜色语言。 */
function toneOf(ratio: number): Tone {
  if (ratio >= 0.85) return 'high'
  if (ratio >= 0.6) return 'mid'
  return 'low'
}

function StatCell({
  label,
  tip,
  value,
  digits = 0,
  ratio,
  tone,
}: {
  label: string
  /** 悬停解释。文案只写"这个数怎么来的"，不写营销话术 */
  tip: string
  value: number | null
  digits?: number
  ratio: number | null
  tone: Tone
}) {
  const w = ratio === null ? 0 : Math.max(0, Math.min(1, ratio)) * 100
  return (
    <div className="stat-cell">
      <div className="stat-cell-top">
        <dt>
          {label}
          <span className="stat-tip" role="img" aria-label={`${label}：${tip}`}>
            ?
          </span>
        </dt>
        <dd className={`stat-num tone-${tone}`}>
          <TweenNumber value={value} digits={digits} />
        </dd>
      </div>
      <div className={`stat-bar tone-${tone}`} aria-hidden="true">
        <i style={{ width: `${w}%` }} />
      </div>
      {/* 解释气泡锚在**格子**上（不是那个小问号）：
          锚在问号上的话，精简布局里格子只有 90px 宽，气泡会横向溢出被侧栏裁掉。
          锚在格子上就永远等于格宽，宽窄两种布局都不会跑出去。
          显隐用 :has 挂在问号的 hover 上 —— 整格 hover 都弹会太吵。 */}
      <span className="stat-tip-pop" role="tooltip">
        {tip}
      </span>
    </div>
  )
}

interface Props {
  state: AppState | null
  /** 正在等模型返回（6~9 秒）。这段时间界面必须自己有动作，
   *  否则用户看到的就是"点了没反应"。 */
  thinking?: boolean
  /** 用户操作触发的宠物脉冲（A1），由 App 广播下来 */
  pulse?: PetPulse | null
  /** 点击光核等本地交互要发脉冲时用它回调（A2 涟漪） */
  onPulse?: (kind: PetPulseKind) => void
}

/** 今日情绪轨迹图（A2 浮层内容）。
 *
 * x 轴 = 今天 0 点到现在；折线 = energy（0~100）；
 * 底部色带 = 档位区段，色取自六档主色。数据来自本页的本地采样
 * （见 lib/moodTrail.ts）—— 不是后端历史，浮层页脚如实标注。 */
function MoodTrailChart({ points, now }: { points: TrailPoint[]; now: number }) {
  const W = 236
  const H = 46
  const STRIP = 6
  const LABEL_H = 12
  const PAD_L = 4
  const PAD_R = 8

  const x = (t: number) => {
    const from = startOfToday(now)
    const span = Math.max(now - from, 1)
    return PAD_L + ((t - from) / span) * (W - PAD_L - PAD_R)
  }
  const y = (e: number) => H - 4 - e * (H - 12)

  /* 少于两个点时不画折线图：一条点连不成线，画出来只是一大片空白。
     改成"当前读数 + 怎么才能长出曲线"的紧凑块 —— 信息量更高，也不装样子。 */
  if (points.length < 2) {
    const p = points[0]
    return (
      <div className="pet-trail-compact">
        {p ? (
          <div className="trail-now">
            <span
              className="trail-now-dot"
              style={{ background: MOOD_COLOR[p.mood] ?? MOOD_COLOR.normal }}
            />
            <span className="trail-now-name">{MOOD_LABEL[p.mood]?.name ?? p.mood}</span>
            <span className="trail-now-energy">{Math.round(p.energy * 100)}</span>
            <span className="trail-now-unit">执行力</span>
            <span className="trail-now-time">
              {new Date(p.t).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
            </span>
          </div>
        ) : null}
        <p className="trail-note">
          {p
            ? '今天的第一条记录。完成一个任务、或档位发生变化后，曲线会在这里长出来。'
            : '今天还没有采样。完成一个任务后再来看看。'}
        </p>
      </div>
    )
  }

  const line = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${x(p.t)} ${y(p.energy)}`).join(' ')
  const totalH = H + STRIP + LABEL_H

  return (
    <svg className="pet-trail-svg" width={W} height={totalH} viewBox={`0 0 ${W} ${totalH}`}>
      {/* 参考线：50% 一条就够，多了变成图表组件 */}
      <line x1={PAD_L} x2={W - PAD_R} y1={y(0.5)} y2={y(0.5)} className="trail-grid" />
      {/* 折线 */}
      <path d={line} fill="none" className="trail-line" />
      {/* 档位色带：每个区段取左端点的档位色，持续到下一点（最后一段延到"现在"） */}
      {points.map((p, i) => {
        const x0 = x(p.t)
        const x1 = i < points.length - 1 ? x(points[i + 1].t) : Math.max(x(now), x0 + 2)
        return (
          <rect
            key={i}
            x={x0}
            y={H + 4}
            width={Math.max(x1 - x0, 2)}
            height={STRIP}
            rx={1.5}
            fill={MOOD_COLOR[p.mood] ?? MOOD_COLOR.normal}
            opacity={0.7}
          />
        )
      })}
      {/* 采样点：当前档主色，末端点加大 */}
      {points.map((p, i) => (
        <circle
          key={`d${i}`}
          cx={x(p.t)}
          cy={y(p.energy)}
          r={i === points.length - 1 ? 3.2 : 2.3}
          fill={MOOD_COLOR[p.mood] ?? MOOD_COLOR.normal}
          className={i === points.length - 1 ? 'trail-dot-last' : 'trail-dot'}
        >
          <title>{`${new Date(p.t).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })} · ${MOOD_LABEL[p.mood]?.name ?? p.mood} · ${Math.round(p.energy * 100)}`}</title>
        </circle>
      ))}
      {/* 时间轴两端：0 点 / 现在。有这个刻度，空白才不是"没画完" */}
      <text x={PAD_L} y={H + STRIP + LABEL_H - 1} className="trail-axis">
        0 点
      </text>
      <text x={W - PAD_R} y={H + STRIP + LABEL_H - 1} className="trail-axis" textAnchor="end">
        现在
      </text>
    </svg>
  )
}

/** 宠物舞台 —— 执行力仪表盘。
 *
 * 情绪由后端确定性计算，此处只负责渲染。
 * 版式（v2）：竖版舞台，宠物居中放大到 168px，情绪名做主标题、
 * 模型那句拟人化台词做正文，下面是六档刻度尺与四项信号。
 *
 * 为什么不是横条：横条里的 64px 头像永远是配角，
 * 而这只猫是整个产品唯一的情感载体 —— 它必须在画面上有分量。
 *
 * A2（v8）：光核本身可点击 —— 涟漪回应 + 弹出今日情绪轨迹浮层。
 */
export default function PetPanel({
  state,
  thinking = false,
  pulse = null,
  onPulse,
}: Props) {
  const pet = state?.pet
  const signals = state?.signals
  // 真实判定值始终来自后端；override 只在该参数存在时**顶替显示**。
  const override = debugMoodOverride()
  const mood = override ?? pet?.mood ?? 'normal'
  const label = MOOD_LABEL[mood] ?? { name: mood, desc: '' }
  const hardCount = signals?.conflicts.filter((c) => c.hard).length ?? 0
  const energy = Math.round((pet?.energy ?? 0.5) * 100)

  /* A2：每次拉到新 state 就采一个轨迹点（内部自去重）。
     DEBUG override 不入轨迹 —— 那不是真实判定，不能污染"本页记录"。 */
  useEffect(() => {
    if (pet && !override) recordTrailPoint(pet.mood, pet.energy)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pet?.mood, pet?.energy])

  const [showTrail, setShowTrail] = useState(false)
  const openTimer = useRef<number | null>(null)
  const trailNow = Date.now()
  const todayPoints = useMemo(
    () => (showTrail ? readTrail().filter((p) => p.t >= startOfToday()) : []),
    [showTrail],
  )

  /* Esc 或点浮层外关闭。挂全局监听只在浮层开着的时候。 */
  useEffect(() => {
    if (!showTrail) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setShowTrail(false)
    const onPointer = (e: PointerEvent) => {
      const t = e.target as HTMLElement | null
      // 光核与浮层都算"里面"：点浮层内不该把它关掉
      if (!t?.closest('.pet-figure') && !t?.closest('.pet-trail')) setShowTrail(false)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('pointerdown', onPointer)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('pointerdown', onPointer)
    }
  }, [showTrail])

  const onFigureClick = () => {
    onPulse?.('tap')
    // 触屏：气泡由光核自己开合，这里不再叠一个轨迹面板
    if (isCoarsePointer()) return
    if (showTrail) {
      setShowTrail(false)
      return
    }
    // 延迟 ~200ms 再弹浮层：让点击的涟漪先播一段，两个反馈都看得见
    if (openTimer.current !== null) window.clearTimeout(openTimer.current)
    openTimer.current = window.setTimeout(() => setShowTrail(true), 200)
  }

  // 组件卸载时清掉未触发的延时，避免对已卸载组件 setState
  useEffect(
    () => () => {
      if (openTimer.current !== null) window.clearTimeout(openTimer.current)
    },
    [],
  )

  return (
    <section className={`pet-stage pet-stage-${mood}${thinking ? ' is-thinking' : ''}`}>
      <div className="pet-stage-halo" aria-hidden="true" />

      <div className="pet-stage-top">
        <span className="pet-kicker">{thinking ? '决策中' : '执行教练'}</span>
        {override && (
          <span className="pet-debug" title="地址栏 ?pet= 参数生效中，画面不是真实判定结果">
            DEBUG 覆盖 · 非真实判定
          </span>
        )}
      </div>

      {/* A2：光核可点击 —— 涟漪回应 + 情绪轨迹浮层。
          用 role=button 而不是 <button>：figure 里已有思考环等装饰元素，
          原生按钮会把它们一起卷进 focus 语义里。 */}
      <div
        className="pet-figure"
        data-pet-core=""
        role="button"
        tabIndex={0}
        aria-expanded={showTrail}
        aria-label="光核：悬停查看状态，点击查看今日情绪轨迹"
        title="悬停查看状态 · 点击查看今日情绪轨迹"
        onClick={onFigureClick}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onFigureClick()
          }
        }}
      >
        {/* 思考态：两圈反向旋转的环 + 一道扫描光。
            模型往返要 6~9 秒，这段时间如果只有按钮变灰，
            观众会以为程序卡死 —— 画面必须自己说明"它在想"。 */}
        {thinking && (
          <>
            <span className="think-ring think-ring-a" aria-hidden="true" />
            <span className="think-ring think-ring-b" aria-hidden="true" />
            <span className="think-scan" aria-hidden="true" />
          </>
        )}
        <CatAvatar
          mood={mood}
          size={252}
          pulse={pulse}
          /* 气泡的数据源就在这里：全部来自 `/api/state`，
             宠物档位判定的结果，不是另算一套。 */
          bubble={{
            name: label.name,
            desc: label.desc,
            procrastination: signals?.procrastination_index ?? null,
            loadRatio: signals?.load_ratio ?? null,
          }}
          /* 面板状态归 PetPanel —— 气泡自己不持业务状态，只负责"叫一声" */
          onOpenTrail={() => setShowTrail(true)}
        />
      </div>

      {/* A2 浮层改成**在流内展开**（不是绝对定位盖上去）：
          盖上去会挡住光核、也会让下层的数据行透出来；
          展开在光核下方，视觉上像它"摊开一张自己的记录"。 */}
      {showTrail && (
        <div className="pet-trail" role="dialog" aria-label="今日情绪轨迹">
          <div className="pet-trail-head">
            <span>今日情绪轨迹</span>
            <button
              className="btn small ghost"
              onClick={() => setShowTrail(false)}
              title="关闭浮层（Esc 也行）"
            >
              关闭
            </button>
          </div>
          <MoodTrailChart points={todayPoints} now={trailNow} />
          <div className="pet-trail-foot">
            本页记录 · {todayPoints.length} 个采样点 · 曲线为执行力（0–100），色带为档位
          </div>
        </div>
      )}

      <h2 className="pet-mood-name">{label.name}</h2>
      <p className="pet-mood-desc">{label.desc}</p>

      {/* 等待期只留"阶段 + 计时 + 进度"，不显示宠物台词 ——
          那 9 秒里它确实还没说话，替它编一句台词反而更假。 */}
      {thinking ? <ThinkProgress /> : <p className="pet-line">{override ? (DEBUG_LINE[override] ?? '—') : (pet?.line ?? '—')}</p>}

      <div
        className="pet-energy"
        role="progressbar"
        aria-label="执行力"
        aria-valuenow={energy}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className="pet-energy-fill" style={{ width: `${energy}%` }} />
      </div>
      <div className="pet-energy-cap">
        <span>执行力</span>
        <span className="num">{energy}</span>
      </div>

      {/* 六档刻度尺：当前档高亮。它诚实地展示"系统只在这六个档位里判定"，
          比编一条假的历史曲线可信 —— 演示时也不会被问"这数据哪来的"。 */}
      <div className="pet-meter" role="img" aria-label={`当前情绪档位：${label.name}`}>
        {MOOD_LIST.map((m: Mood) => (
          <span
            key={m}
            className={`pet-meter-tick${m === mood ? ' is-on' : ''}`}
            data-m={m}
            title={MOOD_LABEL[m]?.name ?? m}
          />
        ))}
      </div>

      {/* 四个指标：数字 + 一条比例条 + 悬停解释。
          条的分母都取后端的真实口径（拖延指数 0..1、负载比 = 已排/10 小时），
          不做二次放大 —— 条的长度必须能对上数字，否则比没有更糟。 */}
      <dl className="pet-stats">
        <StatCell
          label="硬冲突"
          tip="时间真的重叠了，不是 0 就必须重排"
          value={hardCount}
          ratio={hardCount > 0 ? 1 : 0}
          tone={hardCount > 0 ? 'high' : 'low'}
        />
        <StatCell
          label="拖延指数"
          tip="逾期条数 × 平均逾期天数 ÷ 近七天应完成数"
          value={signals?.procrastination_index ?? null}
          digits={2}
          ratio={signals?.procrastination_index ?? null}
          tone={toneOf(signals?.procrastination_index ?? 0)}
        />
        <StatCell
          label="负载比"
          tip="最满的一天：已排时长 ÷ 10 小时上限"
          value={signals?.load_ratio ?? null}
          digits={2}
          ratio={signals?.load_ratio ?? null}
          tone={toneOf(signals?.load_ratio ?? 0)}
        />
        <StatCell
          label="连续天数"
          tip="连续有完成的天数，满条为 7 天"
          value={signals?.streak_days ?? null}
          ratio={Math.min(1, (signals?.streak_days ?? 0) / 7)}
          tone="low"
        />
      </dl>

      <div className="pet-reason">判定依据：{override ? '调试覆盖 · 非真实判定' : (state?.pet_reason ?? '—')}</div>
    </section>
  )
}
