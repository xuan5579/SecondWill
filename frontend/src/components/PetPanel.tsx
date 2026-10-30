import CatAvatar from './CatAvatar'
import type { AppState } from '../types/api'

const MOOD_LABEL: Record<string, string> = {
  alert: '警觉 · 有安排会撞车',
  dormant: '休眠 · 好几天没动了',
  tired: '疲惫 · 积压了几天',
  guilty: '愧疚 · 欠得有点多',
  great: '很好 · 节奏在线',
  normal: '平常',
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

function debugMoodOverride(): string | null {
  if (typeof window === 'undefined') return null
  const v = new URLSearchParams(window.location.search).get('pet')
  return v && (DEBUG_MOODS as readonly string[]).includes(v) ? v : null
}

interface Props {
  state: AppState | null
}

/** 宠物面板 —— 执行力仪表盘。
 *
 * P3-a 仍是 ASCII 占位（P3-c 会换成 SVG，届时不改本文件以外的任何东西）。
 * 情绪由后端确定性计算，此处只负责渲染。
 */
export default function PetPanel({ state }: Props) {
  const pet = state?.pet
  const signals = state?.signals
  // 真实判定值始终来自后端；override 只在该参数存在时**顶替显示**。
  const override = debugMoodOverride()
  const mood = override ?? pet?.mood ?? 'normal'
  const hardCount = signals?.conflicts.filter((c) => c.hard).length ?? 0

  return (
    <section className="pet-bar">
      <div className="pet-face" data-mood={mood}>
        <CatAvatar mood={mood} size={64} />
      </div>
      <div className="pet-meta">
        <div className="pet-mood">
          {MOOD_LABEL[mood] ?? mood}
          {/* 覆盖生效时必须自曝，否则镜头里的画面会被当成真实判定结果 */}
          {override && (
            <span
              style={{
                marginLeft: 8,
                padding: '1px 6px',
                borderRadius: 4,
                fontSize: 11,
                verticalAlign: 'middle',
                background: '#f0a020',
                color: '#1a1a1a',
                fontWeight: 700,
              }}
            >
              DEBUG 覆盖 · 非真实判定
            </span>
          )}
        </div>
        <div
          className="pet-energy"
          role="progressbar"
          aria-label="执行力"
          aria-valuenow={Math.round((pet?.energy ?? 0.5) * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div className="pet-energy-fill" style={{ width: `${(pet?.energy ?? 0.5) * 100}%` }} />
        </div>
        <div className="pet-line">{pet?.line ?? '—'}</div>
        <div className="pet-reason">判定依据：{state?.pet_reason ?? '—'}</div>
      </div>
      <dl className="pet-stats">
        <div>
          <dt>硬冲突</dt>
          <dd className={hardCount > 0 ? 'stat-bad' : 'stat-ok'}>{hardCount}</dd>
        </div>
        <div>
          <dt>拖延指数</dt>
          <dd>{signals?.procrastination_index.toFixed(2) ?? '—'}</dd>
        </div>
        <div>
          <dt>负载比</dt>
          <dd>{signals?.load_ratio.toFixed(2) ?? '—'}</dd>
        </div>
        <div>
          <dt>连续天数</dt>
          <dd>{signals?.streak_days ?? 0}</dd>
        </div>
      </dl>
    </section>
  )
}
