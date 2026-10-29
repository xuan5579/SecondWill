/** 六档猫 —— 执行力仪表盘的可视化。

为什么用 SVG 而不是位图 / sprite：

- **无素材依赖**。桌面版的 `assets/pet/` 是按形态分目录的多帧 PNG，
  换档位要加载不同文件，还得处理缺失回退；SVG 是代码，跟着组件走。
- **任意缩放且不失真**，不需要为不同 DPI 准备多套图。
- **颜色可以用 CSS 变量**，于是深色 / 浅色主题自动生效，不必出两套图。

情绪与动画的对应关系是有意设计的：每种状态都对应一个能"一眼读懂"的动作。
alert 在抖（有安排撞车了要提醒你）、dormant 在慢呼吸（好几天没动静）、
great 在轻微跳动（节奏在线）。
*/

export type Mood = 'alert' | 'dormant' | 'tired' | 'guilty' | 'great' | 'normal'

/** 六档清单。演示每一档都需要它。 */
export const MOOD_LIST: Mood[] = ['alert', 'dormant', 'tired', 'guilty', 'great', 'normal']

interface Props {
  mood: string
  size?: number
}

/** 每档的配色，全部走 CSS 变量，随主题自动切换。 */
const PALETTE: Record<Mood, { fur: string; line: string; accent: string }> = {
  alert: { fur: 'var(--warn)', line: 'var(--warn)', accent: 'var(--warn)' },
  dormant: { fur: 'var(--text-dim)', line: 'var(--text-dim)', accent: 'var(--text-dim)' },
  tired: { fur: 'var(--text-dim)', line: 'var(--text-dim)', accent: 'var(--text-dim)' },
  guilty: { fur: 'var(--danger)', line: 'var(--danger)', accent: 'var(--danger)' },
  great: { fur: 'var(--done)', line: 'var(--done)', accent: 'var(--done)' },
  normal: { fur: 'var(--accent)', line: 'var(--accent)', accent: 'var(--accent)' },
}

function Eyes({ mood, line }: { mood: Mood; line: string }) {
  const common = { stroke: line, strokeWidth: 2.2, strokeLinecap: 'round' as const, fill: 'none' }
  switch (mood) {
    case 'alert':
      // 瞪圆了看你 —— 有安排撞车
      return (
        <>
          <circle cx="24" cy="31" r="4.2" {...common} />
          <circle cx="40" cy="31" r="4.2" {...common} />
          <circle cx="24" cy="31" r="1.4" fill={line} />
          <circle cx="40" cy="31" r="1.4" fill={line} />
        </>
      )
    case 'dormant':
      // 闭着眼睡觉
      return (
        <>
          <path d="M20 31q4 3.5 8 0" {...common} />
          <path d="M36 31q4 3.5 8 0" {...common} />
        </>
      )
    case 'tired':
      // 半眯 + 眼袋
      return (
        <>
          <path d="M20 30.5h8" {...common} />
          <path d="M36 30.5h8" {...common} />
          <path d="M20.5 35q3.5 2 7 0" {...common} strokeWidth={1.4} opacity={0.65} />
          <path d="M36.5 35q3.5 2 7 0" {...common} strokeWidth={1.4} opacity={0.65} />
        </>
      )
    case 'guilty':
      // 下垂的眼（不好意思）
      return (
        <>
          <path d="M20.5 29.5l7 3" {...common} />
          <path d="M43.5 29.5l-7 3" {...common} />
        </>
      )
    case 'great':
      // 笑成月牙
      return (
        <>
          <path d="M20 32q4 -4.5 8 0" {...common} />
          <path d="M36 32q4 -4.5 8 0" {...common} />
        </>
      )
    default:
      return (
        <>
          <path d="M20 31q4 -3 8 0q-4 3 -8 0z" {...common} />
          <path d="M36 31q4 -3 8 0q-4 3 -8 0z" {...common} />
        </>
      )
  }
}

export default function CatAvatar({ mood, size = 64 }: Props) {
  const key = (mood in PALETTE ? mood : 'normal') as Mood
  const theme = PALETTE[key]

  return (
    <svg
      className={`cat cat-${key}`}
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label={`宠物状态：${mood}`}
    >
      {/* 耳 */}
      <path
        d="M17 24 L13 10 L27 18 Z"
        fill={theme.fur}
        opacity={0.9}
        className="cat-ear-left"
      />
      <path
        d="M47 24 L51 10 L37 18 Z"
        fill={theme.fur}
        opacity={0.9}
        className="cat-ear-right"
      />
      {/* 头 */}
      <ellipse cx="32" cy="34" rx="20" ry="18" fill={theme.fur} opacity={0.16} />
      <ellipse
        cx="32"
        cy="34"
        rx="20"
        ry="18"
        fill="none"
        stroke={theme.line}
        strokeWidth="2"
      />
      <Eyes mood={key} line={theme.line} />
      {/* 鼻 */}
      <path d="M30.5 38h3l-1.5 1.8z" fill={theme.line} />
      {/* 嘴 */}
      <path
        d={
          key === 'great'
            ? 'M32 41q-3 3 -5.5 0.5 M32 41q3 3 5.5 0.5'
            : key === 'guilty'
              ? 'M27 43.5q5 -1.5 10 0'
              : key === 'tired' || key === 'dormant'
                ? 'M28.5 43h7'
                : 'M27 42.5q5 3 10 0'
        }
        stroke={theme.line}
        strokeWidth="2"
        strokeLinecap="round"
        fill="none"
      />
      {/* 胡须 */}
      <g stroke={theme.line} strokeWidth="1.2" opacity="0.5" strokeLinecap="round">
        <path d="M13 36h5 M13 40h5" />
        <path d="M51 36h-5 M51 40h-5" />
      </g>
      {/* dormant 的 Zzz */}
      {key === 'dormant' && (
        <text className="cat-zzz" x="44" y="16" fontSize="9" fill={theme.line}>
          z
        </text>
      )}
    </svg>
  )
}
