import CatAvatar, { MOOD_LIST } from './CatAvatar'
import type { AppState } from '../types/api'

const MOOD_LABEL: Record<string, { name: string; desc: string }> = {
  alert: { name: '警觉', desc: '有安排会撞车' },
  dormant: { name: '休眠', desc: '好几天没动了' },
  tired: { name: '疲惫', desc: '积压了几天' },
  guilty: { name: '愧疚', desc: '欠得有点多' },
  great: { name: '很好', desc: '节奏在线' },
  normal: { name: '平常', desc: '一切如常' },
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
  /** 正在等模型返回（6~9 秒）。这段时间界面必须自己有动作，
   *  否则用户看到的就是"点了没反应"。 */
  thinking?: boolean
}

/** 宠物舞台 —— 执行力仪表盘。
 *
 * 情绪由后端确定性计算，此处只负责渲染。
 * 版式（v2）：竖版舞台，宠物居中放大到 168px，情绪名做主标题、
 * 模型那句拟人化台词做正文，下面是六档刻度尺与四项信号。
 *
 * 为什么不是横条：横条里的 64px 头像永远是配角，
 * 而这只猫是整个产品唯一的情感载体 —— 它必须在画面上有分量。
 */
export default function PetPanel({ state, thinking = false }: Props) {
  const pet = state?.pet
  const signals = state?.signals
  // 真实判定值始终来自后端；override 只在该参数存在时**顶替显示**。
  const override = debugMoodOverride()
  const mood = override ?? pet?.mood ?? 'normal'
  const label = MOOD_LABEL[mood] ?? { name: mood, desc: '' }
  const hardCount = signals?.conflicts.filter((c) => c.hard).length ?? 0
  const energy = Math.round((pet?.energy ?? 0.5) * 100)

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

      <div className="pet-figure">
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
        <CatAvatar mood={mood} size={252} />
      </div>

      <h2 className="pet-mood-name">{label.name}</h2>
      <p className="pet-mood-desc">{label.desc}</p>

      <p className="pet-line">{thinking ? '正在重新排这一天的行程…' : (pet?.line ?? '—')}</p>

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
        {MOOD_LIST.map((m) => (
          <span
            key={m}
            className={`pet-meter-tick${m === mood ? ' is-on' : ''}`}
            data-m={m}
            title={MOOD_LABEL[m]?.name ?? m}
          />
        ))}
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

      <div className="pet-reason">判定依据：{state?.pet_reason ?? '—'}</div>
    </section>
  )
}
