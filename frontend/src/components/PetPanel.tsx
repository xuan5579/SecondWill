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
  const mood = pet?.mood ?? 'normal'
  const hardCount = signals?.conflicts.filter((c) => c.hard).length ?? 0

  return (
    <section className="pet-bar">
      <div className="pet-face" data-mood={mood}>
        <CatAvatar mood={mood} size={64} />
      </div>
      <div className="pet-meta">
        <div className="pet-mood">{MOOD_LABEL[mood] ?? mood}</div>
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
