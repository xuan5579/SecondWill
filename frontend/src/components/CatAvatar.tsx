/** 猫形光核 —— AI 情绪核心（全息符号，不是动物）。

设计约束（v7，纯视觉，不参与任何判定）：

- **没有实体**：不要黑猫身体、不要眼睛瞳孔、不要实体猫脸。猫只是**一条剪影轮廓**：
  两个耳尖 + 圆润腮线 + 微收下巴，一笔勾完。它是"标志"，不是"角色"。
- **浓度优先**：对比来自三件事 —— 边缘暗角压到 0.78、描边 0.9 且不糊、
  光团是**拉长的窄椭圆**（62×34）而不是大圆。大圆 + 大模糊 = 一团雾。
- **胡须是全息数据线**：两侧各三条断续线，`dashoffset` 向外流，带零星光点。
  它是"数据流的余光"，不构成脸部结构。
- **四档靠光形与节奏表达**：
  警觉 = 暖金横向展开 + 粒子外扩 + 外晕增强；平常 = 短弧环流 + 平稳呼吸；
  疲惫 = 顶部垫光向下沉 + 呼吸变慢；愧疚 = 光向中心收拢、外晕内聚。
- **表情线是四段短划**：连续的弧放在下半张脸一定会被读成"嘴"，断开的短划不会。
- **A 层交互（v8）**：
  脉冲（pulse prop）= 用户操作的即时回应 —— 完成任务心跳双脉冲 + 粒子外喷，
  新建/改动日程 = 波形线抖动，点击 = 涟漪；
  换档过渡 = 升档光自底部涌起（rise）、降档光缓缓下沉（settle）。
  全部是一次性动画：播完即摘类，呼吸/摇摆循环不受干扰。
- **竞赛大屏友好**：单一符号居中、大留白、外光晕柔，投影仪低对比度下仍读得清。

viewBox 固定 200×200。外光晕用 CSS `filter: drop-shadow`（作用在整块 alpha 上）。
*/

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
} from 'react'
import { createPortal } from 'react-dom'
import { MOOD_RANK, MOOD_SHIFT_MS, type PetPulse } from '../lib/petPulse'

export type Mood = 'alert' | 'dormant' | 'tired' | 'guilty' | 'great' | 'normal'

/** 六档清单。演示每一档都需要它。 */
export const MOOD_LIST: Mood[] = ['alert', 'dormant', 'tired', 'guilty', 'great', 'normal']

/** 悬停气泡要展示的信号。
 *
 * 数据全部由 PetPanel 从 `/api/state` 读好后传进来 —— CatAvatar 不自己取数、
 * 不参与任何判定，只负责把**已经算好的事实**摊开给用户看。
 * 这也是"不改后端、不改状态机"的落点：气泡是纯读侧。
 */
export interface CoreBubbleData {
  /** 当前档位的中文名（PetPanel 的 MOOD_LABEL[mood].name） */
  name: string
  /** 一句话解释这一档的含义 */
  desc?: string
  /** signals.procrastination_index；null = 后端尚未给出 */
  procrastination: number | null
  /** signals.load_ratio */
  loadRatio: number | null
}

interface Props {
  mood: string
  size?: number
  /** 用户操作触发的即时反应（A1）。id 单调递增，key 换新即重播动画 */
  pulse?: PetPulse | null
  /** 悬停气泡的内容。不给就不启用这套交互（小尺寸头像用不上） */
  bubble?: CoreBubbleData | null
  /** 气泡里那个按钮要做的事：轨迹面板归 PetPanel 管，气泡不持业务状态 */
  onOpenTrail?: () => void
}

/** 每档的姿态参数：全部是"光"层面的事实，不参与任何业务判定。 */
const POSE: Record<
  Mood,
  {
    /** 耳尖外旋：正值立起张开（警觉），负值向两侧压平（低落 / 休眠） */
    ear: number
    /** 整体倾斜（度）—— 头"歪"一点，光核就有了体态 */
    tilt: number
    /** 流光明暗：1 = 常态，>1 更亮，<1 暗淡 */
    glow: number
    /** 情绪波形线：四段短划，靠整组斜率表达走向，不构成五官 */
    expr: string
  }
> = {
  // 平常：平缓，几乎水平的一线微光
  normal: { ear: 0, tilt: 0, glow: 1, expr: 'M 64 108 L 76 108 M 84 107 L 96 107 M 108 108 L 120 108 M 128 107 L 138 107' },
  // 很好：更松弛的一线，右端略扬
  great: { ear: 4, tilt: -3, glow: 1.18, expr: 'M 60 108 L 72 109 M 80 107 L 92 105 M 104 104 L 116 102 M 124 103 L 136 104' },
  // 警觉：微微上扬 —— 整组短划向右上方抬，像被什么牵住了注意力
  alert: { ear: 11, tilt: 0, glow: 1.3, expr: 'M 62 111 L 74 110 M 82 108 L 94 105 M 102 103 L 114 100 M 122 98 L 134 95' },
  // 疲惫：轻轻下垂 —— 短划向中间沉下去
  tired: { ear: -6, tilt: 4, glow: 0.72, expr: 'M 64 104 L 76 107 M 84 110 L 96 113 M 104 113 L 116 111 M 124 108 L 136 104' },
  // 休眠：最短最平的一组，几乎熄灭
  dormant: { ear: -12, tilt: 7, glow: 0.42, expr: 'M 78 110 L 88 110 M 96 109 L 104 109 M 112 110 L 122 110' },
  // 愧疚：向下收拢 —— 两翼向中心坠下，跨度收窄
  guilty: { ear: -9, tilt: 5, glow: 0.6, expr: 'M 66 105 L 76 111 M 84 115 L 94 118 M 106 118 L 116 115 M 124 111 L 134 105' },
}

/** 警觉档的外扩粒子：位置在符号周边，方向沿 --dx/--dy 向外飘散。只有六颗。 */
const PARTICLES: { x: number; y: number; dx: number; dy: number; r: number; delay: string }[] = [
  { x: 58, y: 62, dx: -16, dy: -18, r: 2.4, delay: '0s' },
  { x: 142, y: 60, dx: 16, dy: -20, r: 2.1, delay: '0.7s' },
  { x: 34, y: 104, dx: -20, dy: -4, r: 1.9, delay: '1.3s' },
  { x: 166, y: 102, dx: 20, dy: -6, r: 2.3, delay: '0.4s' },
  { x: 76, y: 38, dx: -8, dy: -16, r: 1.8, delay: '1.8s' },
  { x: 126, y: 36, dx: 9, dy: -15, r: 2, delay: '1s' },
]

/** 脉冲爆发粒子：从光核中心向外飞散。只有完成主操作（task_done）才启用。 */
const BURST: { x: number; y: number; dx: number; dy: number; r: number }[] = [
  { x: 100, y: 98, dx: -34, dy: -30, r: 2.6 },
  { x: 100, y: 98, dx: 36, dy: -26, r: 2.2 },
  { x: 100, y: 98, dx: -40, dy: 6, r: 2 },
  { x: 100, y: 98, dx: 42, dy: 10, r: 2.4 },
  { x: 100, y: 98, dx: -14, dy: -38, r: 1.8 },
  { x: 100, y: 98, dx: 18, dy: 36, r: 1.7 },
]

/** 脉冲持续时间：动画播完就把瞬态类摘掉，呼吸/摇摆各自回到常态循环 */
const PULSE_MS = 1000
/** 档位切换过渡时长（rise / settle 扫光）—— 与光流共用同一个常量 */
const SHIFT_MS = MOOD_SHIFT_MS

/* ---- 悬停气泡的时序常量 ------------------------------------------------
 * 三个数字都不是随手取的，改之前先读下面的注释：
 * - HOVER_OPEN_MS：鼠标扫过光核时不该闪出气泡。120ms 是"停留"与"路过"的分界。
 * - BUBBLE_FADE_MS：必须与 CSS `.cat-bubble` 的 transition 时长一致，
 *   否则 JS 会在还没淡完时卸载 DOM，动画被硬切。
 * - LEAVE_GRACE_MS：光核到气泡之间隔着 14px 空隙，指针跨过去会先触发
 *   `pointerleave`。没有这层宽限，手还没落到按钮上气泡就没了。
 */
const HOVER_OPEN_MS = 120
const BUBBLE_FADE_MS = 250
const LEAVE_GRACE_MS = 120

/** 是否"能 hover 的精细指针"。
 *
 * 触屏必须走另一条路（点击开合）：`pointerenter` 在触摸时也会触发，
 * 两套逻辑同时生效就会"点一下开两次、关不掉"。
 */
function canHover(): boolean {
  if (typeof window === 'undefined') return false
  return window.matchMedia('(hover: hover) and (pointer: fine)').matches
}

function fmtMetric(v: number | null): string {
  return v == null ? '—' : v.toFixed(2)
}

export default function CatAvatar({
  mood,
  size = 64,
  pulse = null,
  bubble = null,
  onOpenTrail,
}: Props) {
  const key = (mood in POSE ? mood : 'normal') as Mood
  const pose = POSE[key]

  /* ---- A1/A2：脉冲的瞬态相位。pulse.id 变化 → 挂类 1 秒 → 摘掉。
     不重挂组件：根 svg 上还有呼吸等无限循环，重挂会把它们一起打断。 */
  const [beat, setBeat] = useState<'done' | 'tick' | 'tap' | null>(null)
  useEffect(() => {
    if (!pulse) return
    setBeat(
      pulse.kind === 'task_done' ? 'done' : pulse.kind === 'tap' ? 'tap' : 'tick',
    )
    const timer = window.setTimeout(() => setBeat(null), PULSE_MS)
    return () => window.clearTimeout(timer)
    // 只认 id：同一个 pulse 对象在父组件重渲染时不应重播
  }, [pulse?.id])

  /* ---- A3：档位切换的过渡方向。升档 = 光从底部涌起（rise），
     降档 = 光缓缓下沉（settle）。prevMood 记住上一档，
     相同档位的普通重渲染不会误触发。 */
  const prevMood = useRef<Mood | null>(null)
  const [shift, setShift] = useState<{ dir: 'rise' | 'settle'; n: number } | null>(null)
  const shiftSeq = useRef(0)
  useEffect(() => {
    const prev = prevMood.current
    prevMood.current = key
    if (prev === null || prev === key) return
    const dir = MOOD_RANK[key] > MOOD_RANK[prev] ? 'rise' : 'settle'
    shiftSeq.current += 1
    setShift({ dir, n: shiftSeq.current })
    const timer = window.setTimeout(() => setShift(null), SHIFT_MS)
    return () => window.clearTimeout(timer)
  }, [key])

  /* ---- 悬停气泡：三态机 + 视口避让 --------------------------------------
   *
   * 为什么是"三态"而不是一个布尔：淡出要占 250ms，这期间 DOM 必须还挂着。
   * 用布尔就会在移出瞬间把节点拆掉，动画只能硬切；移回时又新建一个节点、
   * 动画从头播 —— 那正是"动画期间重复创建气泡 DOM"的毛病。
   * 状态机让同一个节点活过 open → leaving → idle，中途移回只是退回 open。
   *
   * 全部是纯读侧：气泡只展示 PetPanel 传进来的既成事实，不写任何状态。
   */
  const [phase, setPhase] = useState<'idle' | 'open' | 'leaving'>('idle')
  /** 悬停热态独立于气泡相位：移出时光核要立刻回弹，气泡则慢慢淡 —— 两个节奏。 */
  const [hot, setHot] = useState(false)
  const [pos, setPos] = useState<{
    left: number
    top: number
    place: 'top' | 'bottom'
    arrow: number
  } | null>(null)

  const coreRef = useRef<HTMLDivElement | null>(null)
  const bubbleRef = useRef<HTMLDivElement | null>(null)
  const openTimer = useRef<number | null>(null)
  const closeTimer = useRef<number | null>(null)

  const clearOpen = useCallback(() => {
    if (openTimer.current !== null) {
      window.clearTimeout(openTimer.current)
      openTimer.current = null
    }
  }, [])
  const clearClose = useCallback(() => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current)
      closeTimer.current = null
    }
  }, [])

  /* leaving 是过渡态：BUBBLE_FADE_MS 后自己回 idle 并卸载 DOM。
     必须由 effect 兜着 —— 否则"指针移出后再没动过"时，节点会永远挂在 body 上。 */
  useEffect(() => {
    if (phase !== 'leaving') return
    const timer = window.setTimeout(() => setPhase('idle'), BUBBLE_FADE_MS)
    return () => window.clearTimeout(timer)
  }, [phase])

  /* 入场动画交给 CSS 的 `@starting-style`（见 index.css）：
   * 它在元素**刚插入**时提供一份起始样式，transition 于是有了起点，能从 0 淡到 1。
   *
   * 为什么不用"JS 记上一相位、再挂 is-fresh 类"这套：
   * ① 挂载即 open，元素没有上一帧，纯 transition 不会动；
   * ② 先后试过 keyframes + 相位判断 —— StrictMode 会把 effect 跑两遍，
   *    第二次进来时"上一相位"已经是 open，类立刻被摘掉，动画根本不播。
   * CSS 自己知道"这是新插入的元素"，比在 JS 里记录更可靠，也少一个状态。
   * 淡出途中移回时节点并未重新插入，所以不会重播淡入 —— 防抖照旧成立。
   */

  /** 悬停进入：光核立刻亮，气泡要等 HOVER_OPEN_MS（路过不算停留） */
  const onCoreEnter = () => {
    if (!bubble || !canHover()) return
    setHot(true)
    clearOpen()
    if (phase === 'open') return
    openTimer.current = window.setTimeout(() => setPhase('open'), HOVER_OPEN_MS)
  }

  /** 指针离开光核：光核立刻回弹，气泡留一段宽限给指针跨过那 14px 空隙 */
  const onCoreLeave = () => {
    setHot(false)
    if (!bubble) return
    clearOpen()
    clearClose()
    closeTimer.current = window.setTimeout(
      () => setPhase((p) => (p === 'idle' ? p : 'leaving')),
      LEAVE_GRACE_MS,
    )
  }

  /** 指针进到气泡里：撤销宽限。没有这一步，按钮根本点不到。 */
  const onBubbleEnter = () => {
    clearClose()
    setPhase('open')
  }

  const onBubbleLeave = () => {
    if (!canHover()) return
    clearClose()
    closeTimer.current = window.setTimeout(() => setPhase('leaving'), LEAVE_GRACE_MS)
  }

  /** 触屏：单击开合。桌面端不接管 —— 点光核仍是 PetPanel 的"开轨迹"。 */
  const onCoreClick = () => {
    if (!bubble || canHover()) return
    clearOpen()
    clearClose()
    setPhase((p) => (p === 'open' ? 'leaving' : 'open'))
  }

  /* 视口避让：气泡用 fixed + portal 挂到 body —— 必须这样，因为
     `.pet-stage` 有 `overflow: hidden`，放在里面会被卡片顶边裁掉。
     位置每次打开时实测一次：水平夹进视口；垂直**默认放上方**（箭头朝下指着光核
     才是"它自己说出来"的读法），只有上方确实放不下、下方还放得下时才翻下去。
     注意别退化成"看见放不下就翻"：精简态（有干预卡片时光核只有 104px）
     上方往往差几像素，一翻就正好盖住光核本身，比压住标题难看得多。 */
  const measure = useCallback(() => {
    const core = coreRef.current
    const card = bubbleRef.current
    if (!core || !card) return
    const c = core.getBoundingClientRect()
    const b = card.getBoundingClientRect()
    const vw = document.documentElement.clientWidth
    const vh = document.documentElement.clientHeight
    const GAP = 14
    const MARGIN = 10
    const centerX = c.left + c.width / 2
    const left = Math.max(MARGIN, Math.min(centerX - b.width / 2, vw - b.width - MARGIN))
    /* 顶部避让：光核在右栏顶部、贴近页头，气泡翻到上方时会直接压住
       header 里的「问决策器 / 刷新」按钮。上方可用空间不是从视口顶 0 算，
       而是从 header 底边算 —— 装不下就翻到下方。 */
    const headerBottom =
      document.querySelector('.app-header')?.getBoundingClientRect().bottom ?? 0
    const safeTop = Math.max(MARGIN, headerBottom)
    const above = c.top - GAP - b.height
    const below = c.bottom + GAP
    /* "放不下"按几何判断（above >= safeTop），不按留白判断。
       留白那几像素可以用 MARGIN 夹回来，一旦按留白判，精简体下差 1px 就会翻到
       下方、正好压住指标行；按几何判则永远只在真的装不下时才翻。 */
    const fitsAbove = above >= safeTop
    const fitsBelow = below + b.height <= vh - MARGIN
    const place: 'top' | 'bottom' = !fitsAbove && fitsBelow ? 'bottom' : 'top'
    const top =
      place === 'top'
        ? Math.max(safeTop, Math.min(above, vh - b.height - MARGIN))
        : Math.min(below, vh - b.height - MARGIN)
    setPos({ left, top, place, arrow: centerX - left })
  }, [])

  /* layout effect：在浏览器绘制前量好位置。
     首帧给的是 -9999px 的离屏坐标（仅用于测量），量完立刻落到正确位置，
     否则气泡会先在错的地方闪一下再跳过去。 */
  useLayoutEffect(() => {
    if (phase !== 'open') return
    measure()
  }, [phase, measure])

  // 视口一变（拉窗口 / 转屏 / 页面滚动）fixed 坐标就失效了，重算
  useEffect(() => {
    if (phase !== 'open') return
    const onMove = () => measure()
    window.addEventListener('resize', onMove)
    window.addEventListener('scroll', onMove, true)
    return () => {
      window.removeEventListener('resize', onMove)
      window.removeEventListener('scroll', onMove, true)
    }
  }, [phase, measure])

  /* 点页面其它地方收起。捕获阶段监听：抢在被点元素处理之前关掉，
     免得"点到别的东西了，气泡还压在上面"。 */
  useEffect(() => {
    if (phase !== 'open') return
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node | null
      if (!t) return
      if (coreRef.current?.contains(t) || bubbleRef.current?.contains(t)) return
      clearOpen()
      setPhase('leaving')
    }
    window.addEventListener('pointerdown', onDown, true)
    return () => window.removeEventListener('pointerdown', onDown, true)
  }, [phase, clearOpen])

  // 卸载时清掉未触发的延时，避免对已卸载组件 setState
  useEffect(
    () => () => {
      clearOpen()
      clearClose()
    },
    [clearOpen, clearClose],
  )

  /** 气泡里的按钮：先收气泡再开面板 —— 留着它会盖住刚展开的图表 */
  const openTrailFromBubble = () => {
    setPhase('leaving')
    setHot(false)
    onOpenTrail?.()
  }

  /* 猫头：一条完整剪影。耳尖随档位位移 —— 警觉时向外上方张开，
     低落时向两侧压平。
     比例是"像猫"的全部：耳是**三角**（内缘走直线，两耳之间只留浅 V），
     耳高约占头高的 1/4；最宽处落在脸颊而不是头顶；下巴圆而微收。
     耳内缘做成弧线就立刻变成"莲花瓣"，这是这个符号唯一不能松的地方。 */
  const earDX = pose.ear * 0.8
  const earDY = -pose.ear * 0.5
  const head = [
    `M ${56 + earDX} ${40 + earDY}`, // 左耳尖
    'L 42 88', // 左耳外缘（直线 = 三角形的一条边）
    'C 38 108 46 126 64 138', // 左颊
    'C 76 146 86 150 100 150', // 左下颌
    'C 114 150 124 146 136 138', // 右下颌
    'C 154 126 162 108 158 88', // 右颊
    `L ${144 - earDX} ${40 + earDY}`, // 右耳尖
    'L 122 66', // 右耳内缘
    'Q 100 60 78 66', // 两耳之间的浅 V（控制点在上方 = 更深的下凹）
    'Z', // 闭合 = 左耳内缘
  ].join(' ')

  /* 光核本体单独存成变量：外层要再包一层"悬停热区"与 portal 气泡，
     但 SVG 内部一行都不该因此改动缩进 —— 那是 300 行的噪声 diff。 */
  const coreSvg = (
    <svg
      className={[
        `cat cat-${key}`,
        beat ? `is-pulse-${beat}` : '',
        shift ? `is-${shift.dir}` : '',
      ]
        .filter(Boolean)
        .join(' ')}
      width={size}
      height={size}
      viewBox="0 0 200 200"
      role="img"
      aria-label={`伙伴状态：${mood}`}
      style={{ opacity: 0.55 + 0.45 * pose.glow }}
    >
      <defs>
        {/* 主流光团：窄而长 —— 光团做大做圆，整块就变成"雾" */}
        <radialGradient id="coreA" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0%" stopColor="var(--fur-mid)" stopOpacity="0.6" />
          <stop offset="55%" stopColor="var(--fur-mid)" stopOpacity="0.2" />
          <stop offset="100%" stopColor="var(--fur-mid)" stopOpacity="0" />
        </radialGradient>
        {/* 副流光团：青色，与紫团错相漂移 */}
        <radialGradient id="coreB" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0%" stopColor="var(--iris)" stopOpacity="0.55" />
          <stop offset="55%" stopColor="var(--iris)" stopOpacity="0.16" />
          <stop offset="100%" stopColor="var(--iris)" stopOpacity="0" />
        </radialGradient>
        {/* 亮芯：大面积柔光而不是一个点 —— 小亮点会被读成"眼睛" */}
        <radialGradient id="coreC" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0%" stopColor="#ffffff" stopOpacity="0.26" />
          <stop offset="40%" stopColor="var(--iris)" stopOpacity="0.26" />
          <stop offset="100%" stopColor="var(--iris)" stopOpacity="0" />
        </radialGradient>
        {/* 背后那圈淡光晕 */}
        <radialGradient id="coreHalo" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0%" stopColor="var(--fur-mid)" stopOpacity="0.15" />
          <stop offset="60%" stopColor="var(--fur-mid)" stopOpacity="0.05" />
          <stop offset="100%" stopColor="var(--fur-mid)" stopOpacity="0" />
        </radialGradient>
        {/* 轮廓描边：上青下紫，一眼看得清 —— 这一圈线是符号"立住"的一半 */}
        <linearGradient id="coreRim" x1="0.5" y1="0" x2="0.5" y2="1">
          <stop offset="0%" stopColor="var(--iris)" stopOpacity="0.9" />
          <stop offset="55%" stopColor="var(--fur-mid)" stopOpacity="0.7" />
          <stop offset="100%" stopColor="var(--fur-mid)" stopOpacity="0.9" />
        </linearGradient>
        {/* 底色：极淡，只保证腮和下巴不是空的 —— 满了就成灯罩 */}
        <radialGradient id="coreBase" cx="0.5" cy="0.52" r="0.62">
          <stop offset="0%" stopColor="var(--fur-mid)" stopOpacity="0.16" />
          <stop offset="55%" stopColor="var(--fur-mid)" stopOpacity="0.09" />
          <stop offset="100%" stopColor="var(--fur-mid)" stopOpacity="0.03" />
        </radialGradient>
        {/* 边缘暗角：这一层决定了它是"光核"还是"猫形灯罩"。
            边缘压到 0.78 才有玻璃的厚度，压轻了整块就发雾 */}
        <radialGradient id="coreVig" cx="0.5" cy="0.52" r="0.52">
          <stop offset="0%" stopColor="#000000" stopOpacity="0" />
          <stop offset="52%" stopColor="#05060a" stopOpacity="0.2" />
          <stop offset="82%" stopColor="#05060a" stopOpacity="0.44" />
          <stop offset="100%" stopColor="#03040a" stopOpacity="0.66" />
        </radialGradient>
        {/* 光丝：两端渐隐，才像"流过的光"，不像画的线 */}
        <linearGradient id="coreWire" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="var(--iris)" stopOpacity="0" />
          <stop offset="32%" stopColor="var(--iris)" stopOpacity="0.9" />
          <stop offset="70%" stopColor="var(--fur-mid)" stopOpacity="0.45" />
          <stop offset="100%" stopColor="var(--fur-mid)" stopOpacity="0" />
        </linearGradient>
        {/* 胡须数据线：两端渐隐 —— 在剪影之外最容易抢戏，所以再淡一档 */}
        <linearGradient id="coreWhisk" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="var(--whisk-ink, var(--iris))" stopOpacity="0" />
          <stop offset="50%" stopColor="var(--whisk-ink, var(--iris))" stopOpacity="0.62" />
          <stop offset="100%" stopColor="var(--fur-mid)" stopOpacity="0" />
        </linearGradient>
        {/* 疲惫档的下沉光：自上而下一层缓缓坠落的冷光 */}
        <linearGradient id="coreSink" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--iris)" stopOpacity="0.28" />
          <stop offset="100%" stopColor="var(--iris)" stopOpacity="0" />
        </linearGradient>
        {/* A3 档位切换的扫光：rise 时这层光从底部涌到顶部再散去 */}
        <linearGradient id="coreSurge" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stopColor="var(--iris)" stopOpacity="0.5" />
          <stop offset="100%" stopColor="var(--iris)" stopOpacity="0" />
        </linearGradient>
        {/* 情绪波形线专用：中段略亮，两端收 */}
        <linearGradient id="coreExpr" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="var(--iris)" stopOpacity="0" />
          <stop offset="20%" stopColor="var(--iris)" stopOpacity="0.45" />
          <stop offset="55%" stopColor="var(--iris)" stopOpacity="0.7" />
          <stop offset="100%" stopColor="var(--iris)" stopOpacity="0" />
        </linearGradient>
        <filter id="coreSoftFilter" x="-40%" y="-40%" width="180%" height="180%">
          <feGaussianBlur stdDeviation="1.1" />
        </filter>
        {/* 描边只抹锯齿，不糊线 —— 糊了就丢掉那一圈"霓虹" */}
        <filter id="coreRimSoft" x="-25%" y="-25%" width="150%" height="150%">
          <feGaussianBlur stdDeviation="0.5" />
        </filter>
        {/* 波形线只柔化一点点，保留"一道线"的读法 */}
        <filter id="coreExprSoft" x="-30%" y="-120%" width="160%" height="340%">
          <feGaussianBlur stdDeviation="1.1" />
        </filter>
        {/* 流光全部裁在剪影里：光是"从符号内部透出来"的 */}
        <clipPath id="coreClip">
          <path d={head} />
        </clipPath>
      </defs>

      <g style={{ transform: `rotate(${pose.tilt}deg)`, transformOrigin: '100px 100px' }}>
        {/* 光晕 + 一圈极细虚线轨道环：环在剪影外面，不会造成"碗口" */}
        <circle cx={100} cy={96} r={88} fill="url(#coreHalo)" className="core-halo" />
        <circle
          className="core-orbit"
          cx={100}
          cy={96}
          r={66}
          fill="none"
          stroke="var(--fur-mid)"
          strokeWidth={0.8}
          strokeDasharray="2 9"
          opacity={0.4}
        />

        {/* 内部流光：紫团与青团错相漂移 + 呼吸亮芯，全部裁进剪影 */}
        <g clipPath="url(#coreClip)">
          {/* 浅色主题垫的深墨；深色主题下透明 */}
          <path className="core-ink" d={head} fill="var(--core-ink, transparent)" />
          <path d={head} fill="url(#coreBase)" />
          {/* 两团光用 screen 叠加：紫 + 青直接相加会发灰绿，screen 只加亮不混色 */}
          <g className="core-blend">
            {/* 光团沿同一条对角线拉长：方向一致才像"流"，圆形光斑只会像一盏灯 */}
            <g transform="rotate(-18 70 78)">
              <ellipse className="core-blob core-blob-a" cx={70} cy={78} rx={62} ry={34} fill="url(#coreA)" />
            </g>
            <g transform="rotate(-18 120 112)">
              <ellipse className="core-blob core-blob-b" cx={120} cy={112} rx={50} ry={26} fill="url(#coreB)" opacity={0.55} />
            </g>
            {/* 斜向流光带：一条拉长的青光横穿剪影，静态里也读得出"在流动"。
                旋转放在 g 上，动画只改透明度 —— CSS transform 会覆盖 SVG 的 transform 属性 */}
            <g transform="rotate(-24 100 104)">
              <ellipse className="core-streak" cx={100} cy={104} rx={90} ry={20} fill="url(#coreB)" opacity={0.55} />
            </g>
            {/* 下巴处的下沉光，把剪影底部填满 */}
            <ellipse cx={100} cy={140} rx={82} ry={48} fill="url(#coreB)" opacity={0.66} />
            <ellipse cx={100} cy={150} rx={58} ry={34} fill="url(#coreA)" opacity={0.55} />
            {/* 警觉档：一团横向展开的暖金，把光"摊"向两侧 */}
            <ellipse className="core-spread" cx={100} cy={98} rx={96} ry={40} fill="url(#coreA)" />
            {/* 疲惫档：顶部先垫一层亮，下沉才有"起点" */}
            <ellipse className="core-toplight" cx={100} cy={52} rx={70} ry={36} fill="url(#coreB)" />
          </g>
          {/* 平常档：两段短弧在不同半径反向巡游 —— 不画完整圆环（那是碗口） */}
          <circle
            className="core-ring-arc"
            cx={100}
            cy={98}
            r={55}
            fill="none"
            stroke="var(--iris)"
            strokeWidth={1.6}
            strokeDasharray="26 320"
            strokeLinecap="round"
            filter="url(#coreSoftFilter)"
          />
          <circle
            className="core-ring-arc core-ring-arc-b"
            cx={100}
            cy={98}
            r={42}
            fill="none"
            stroke="var(--iris)"
            strokeWidth={1.2}
            strokeDasharray="16 248"
            strokeLinecap="round"
            filter="url(#coreSoftFilter)"
          />
          {/* 疲惫档：一层冷光自顶部缓缓下沉 */}
          <rect className="core-sink" x={8} y={-84} width={184} height={84} fill="url(#coreSink)" />
          {/* 光丝：三条斜向细光带，明暗错相 —— "流动"最直接的证据 */}
          <g
            className="core-wire"
            stroke="url(#coreWire)"
            strokeLinecap="round"
            fill="none"
            filter="url(#coreSoftFilter)"
          >
            <path d="M 22 150 C 60 134 94 116 126 100 S 158 84 180 74" strokeWidth={2.4} opacity={0.5} />
          </g>
          {/* 暗角压在光之上：边缘沉下去，中间才亮得像有光源 */}
          <path d={head} fill="url(#coreVig)" />
          {/* 亮芯包一层 beat-layer：脉冲/换档的缩放加在父级 g 上，
              与 .core-heart 自身的呼吸动画互不覆盖（transform 是乘法叠加） */}
          <g className="core-beat-layer">
            <ellipse className="core-heart" cx={100} cy={98} rx={44} ry={40} fill="url(#coreC)" />
          </g>
          {/* A3 换档扫光：一团柔光沿竖向扫过整个剪影（升档向上、降档向下）。
              放在暗角与亮芯**之上**（screen 叠加）：压在暗角下面就看不出"光在移动"。
              key 用序号 —— 同方向连续切换也要重播，React 必须看到新 key */}
          {shift && (
            <rect
              key={shift.n}
              className={shift.dir === 'rise' ? 'core-surge' : 'core-settle-sweep'}
              x={8}
              y={20}
              width={184}
              height={140}
              fill="url(#coreSurge)"
            />
          )}
          {/* 情绪波形线：四段短划，随档位改走向 —— 是"波形"，不是嘴 */}
          <path
            className="core-expr"
            d={pose.expr}
            fill="none"
            stroke="url(#coreExpr)"
            strokeWidth={2}
            strokeLinecap="round"
            filter="url(#coreExprSoft)"
          />
        </g>

        {/* 剪影轮廓：一笔渐变光描边 */}
        <path
          className="core-rim"
          d={head}
          fill="none"
          stroke="url(#coreRim)"
          strokeWidth={1.7}
          strokeLinejoin="round"
          filter="url(#coreRimSoft)"
        />

        {/* A1/A2 脉冲层：涟漪圈 + 完成任务的爆发粒子。
            key 跟 pulse.id —— 每次新事件强制重挂，CSS 动画重播一次 */}
        {pulse && (
          <g key={pulse.id} className={`core-pulse-layer core-pulse-${pulse.kind}`}>
            <circle
              className="core-ripple"
              cx={100}
              cy={100}
              r={52}
              fill="none"
              stroke="var(--iris)"
              strokeWidth={pulse.kind === 'task_done' ? 2.2 : 1.5}
            />
            {pulse.kind === 'task_done' && (
              <>
                {/* 第二条涟漪错峰出发：两次波纹叠起来才有"心跳"的层次 */}
                <circle
                  className="core-ripple core-ripple-2"
                  cx={100}
                  cy={100}
                  r={52}
                  fill="none"
                  stroke="var(--iris)"
                  strokeWidth={1.6}
                />
                {/* 一次性辉光：整块光核"亮一下" —— 心跳不能只靠几何放大 */}
                <circle className="core-flash" cx={100} cy={98} r={64} fill="url(#coreB)" />
                {BURST.map((p, i) => (
                  <circle
                    key={i}
                    className="core-burst-dot"
                    cx={p.x}
                    cy={p.y}
                    r={p.r}
                    fill="var(--iris)"
                    style={{ '--dx': `${p.dx}px`, '--dy': `${p.dy}px` } as CSSProperties}
                  />
                ))}
              </>
            )}
          </g>
        )}

        {/* 全息胡须：两侧各三条断续数据线 + 零星光点。
            从腮边向外"流出"，透明度压到似有似无 —— 是全息投影的余光，不是毛 */}
        <g className="core-whiskers" fill="none" strokeLinecap="round">
          <g stroke="url(#coreWhisk)">
            <path className="core-whisk" d="M 46 100 C 34 96 22 96 10 92" strokeWidth={1.3} />
            <path className="core-whisk" d="M 44 112 C 32 112 20 114 8 116" strokeWidth={1.2} />
            <path className="core-whisk" d="M 48 124 C 36 128 24 130 14 134" strokeWidth={1} />
            <path className="core-whisk" d="M 154 100 C 166 96 178 96 190 92" strokeWidth={1.3} />
            <path className="core-whisk" d="M 156 112 C 168 112 180 114 192 116" strokeWidth={1.2} />
            <path className="core-whisk" d="M 152 124 C 164 128 176 130 186 134" strokeWidth={1} />
          </g>
          <g fill="var(--whisk-ink, var(--iris))" stroke="none">
            <circle className="core-whisk-dot" cx={18} cy={95} r={1.8} />
            <circle className="core-whisk-dot" cx={30} cy={113} r={1.4} />
            <circle className="core-whisk-dot" cx={182} cy={95} r={1.8} />
            <circle className="core-whisk-dot" cx={170} cy={113} r={1.4} />
          </g>
        </g>

        {/* 警觉档：少量粒子向外扩散 —— "注意到了什么"的物理化表达 */}
        <g className="core-particles" fill="var(--iris)">
          {PARTICLES.map((p, i) => (
            <circle
              key={i}
              className="core-particle"
              cx={p.x}
              cy={p.y}
              r={p.r}
              style={{ '--dx': `${p.dx}px`, '--dy': `${p.dy}px`, animationDelay: p.delay } as CSSProperties}
            />
          ))}
        </g>
      </g>
    </svg>
  )

  return (
    <>
      {/* 悬停热区 = 光核本身。指针反馈（1.06 缩放、光晕增强）挂在这一层：
          `.cat` 身上已经跑着呼吸/脉冲/换档的 transform，直接在它上面写 scale 会被覆盖。 */}
      <div
        ref={coreRef}
        className={`cat-core${hot ? ' is-hot' : ''}`}
        onPointerEnter={onCoreEnter}
        onPointerLeave={onCoreLeave}
        onClick={onCoreClick}
      >
        {coreSvg}
      </div>

      {/* 气泡必须走 portal：`.pet-stage` 带 `overflow: hidden`，
          放在卡片里面一定会被顶边裁掉。fixed 坐标由 measure() 实测。 */}
      {bubble &&
        phase !== 'idle' &&
        createPortal(
          <div
            ref={bubbleRef}
            className={`cat-bubble is-${phase}`}
            data-place={pos?.place ?? 'top'}
            style={
              {
                left: pos?.left ?? -9999,
                top: pos?.top ?? 0,
                '--arrow': `${pos?.arrow ?? 0}px`,
              } as CSSProperties
            }
            role="dialog"
            aria-label={`${bubble.name} · 当前光核状态`}
            onPointerEnter={onBubbleEnter}
            onPointerLeave={onBubbleLeave}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="cat-bubble-card">
              <div className="cat-bubble-head">
                <span className="cat-bubble-dot" data-m={key} aria-hidden="true" />
                <strong className="cat-bubble-name">{bubble.name}</strong>
                {bubble.desc && <span className="cat-bubble-desc">{bubble.desc}</span>}
              </div>

              <div className="cat-bubble-stats">
                <div>
                  <span>拖延指数</span>
                  <b>{fmtMetric(bubble.procrastination)}</b>
                </div>
                <div>
                  <span>负载比</span>
                  <b>{fmtMetric(bubble.loadRatio)}</b>
                </div>
              </div>

              <button type="button" className="cat-bubble-cta" onClick={openTrailFromBubble}>
                查看今日情绪轨迹
              </button>

              {/* 箭头：指向光核中心；气泡被视口夹紧时，箭头按夹紧量反向补回来 */}
              <span className="cat-bubble-arrow" aria-hidden="true" />
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}
