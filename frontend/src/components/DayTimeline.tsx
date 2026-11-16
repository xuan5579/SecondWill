import type { RecordItem } from '../types/api'

/** 今日时间轴 —— 把"这一天有多满"画出来。
 *
 * 为什么值得占掉左栏一块版面：
 * 日程列表只回答"有哪些事"，回答不了"排得下吗"。冲突检测和负载比
 * 都是这件事的结论，但结论应该长在证据上 —— 这根轴就是证据。
 *
 * 纯前端派生：位置只由 start_at / end_at 与本地的 08:00–23:00 窗口算出，
 * 不请求后端、不参与任何判定。
 */

const DAY_START_HOUR = 8
const DAY_END_HOUR = 23
const SPAN_MIN = (DAY_END_HOUR - DAY_START_HOUR) * 60

interface Props {
  records: RecordItem[]
}

function minutesOfDay(iso: string): number {
  const d = new Date(iso)
  return d.getHours() * 60 + d.getMinutes()
}

function dayKey(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
}

export default function DayTimeline({ records }: Props) {
  const all = records
    .filter((r) => r.start_at && r.end_at && r.status !== 'dropped')
    .map((r) => {
      const s = minutesOfDay(r.start_at!)
      const e = minutesOfDay(r.end_at!)
      return { r, s, e: e <= s ? s + 30 : e, day: dayKey(r.start_at!) }
    })

  // 取哪一天来画：今天 → 之后最近的一天 → 条目最多的那天。
  // 一律按"今天"画是错的：明天的 09:00 会落在今天 09:00 的位置。
  const todayKey = dayKey(new Date().toISOString())
  const days = Array.from(new Set(all.map((x) => x.day))).sort()
  const later = days.filter((d) => d >= todayKey)
  const countByDay: Record<string, number> = {}
  for (const x of all) countByDay[x.day] = (countByDay[x.day] ?? 0) + 1
  const busiest = Object.entries(countByDay).sort((a, b) => b[1] - a[1])[0]?.[0]
  const shownDay = days.includes(todayKey) ? todayKey : (later[0] ?? busiest ?? todayKey)
  const isToday = shownDay === todayKey

  const timed = all
    .filter((x) => x.day === shownDay)
    .filter((x) => x.e > DAY_START_HOUR * 60 && x.s < DAY_END_HOUR * 60)

  const now = new Date()
  const nowMin = now.getHours() * 60 + now.getMinutes()
  const nowPct = ((nowMin - DAY_START_HOUR * 60) / SPAN_MIN) * 100
  const showNow = isToday && nowPct >= 0 && nowPct <= 100

  const pct = (m: number) => ((m - DAY_START_HOUR * 60) / SPAN_MIN) * 100

  // 重叠的条目错开成两行，否则同一时段的色块会互相盖住
  const lanes: number[] = []
  const placed = timed
    .sort((a, b) => a.s - b.s)
    .map((x) => {
      let lane = 0
      while (lane < lanes.length && lanes[lane] > x.s) lane += 1
      lanes[lane] = x.e
      return { ...x, lane }
    })
  const laneCount = Math.max(1, lanes.length)

  const [, mm, dd] = shownDay.split('-')

  return (
    <section className="day-axis">
      <div className="day-axis-head">
        <h2>{isToday ? '今天的时间分布' : `${Number(mm)}-${dd} 的时间分布`}</h2>
        <span className="day-axis-range">08:00 — 23:00</span>
      </div>

      <div className="day-axis-track" style={{ height: 16 + laneCount * 14 }}>
        {/* 整点刻度 */}
        {[8, 12, 16, 20, 23].map((h) => (
          <span
            key={h}
            className="day-axis-tick"
            style={{ left: `${((h - DAY_START_HOUR) / (DAY_END_HOUR - DAY_START_HOUR)) * 100}%` }}
          />
        ))}

        {placed.map(({ r, s, e, lane }) => {
          const left = Math.max(0, pct(s))
          const width = Math.max(1.2, Math.min(100 - left, pct(e) - left))
          return (
            <span
              key={r.id}
              className={`day-axis-bar pri-${r.priority}${r.status === 'done' ? ' is-done' : ''}`}
              style={{ left: `${left}%`, width: `${width}%`, top: 4 + lane * 14 }}
              title={`${r.title}  ${r.start_at ? new Date(r.start_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''}–${r.end_at ? new Date(r.end_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''}`}
            />
          )
        })}

        {showNow && (
          <span className="day-axis-now" style={{ left: `${nowPct}%` }}>
            <i />
          </span>
        )}
      </div>

      <div className="day-axis-scale">
        {[8, 12, 16, 20, 23].map((h) => (
          <span key={h} style={{ left: `${((h - DAY_START_HOUR) / (DAY_END_HOUR - DAY_START_HOUR)) * 100}%` }}>
            {h}:00
          </span>
        ))}
      </div>

      {timed.length === 0 && <p className="day-axis-empty">今天还没有排定时间的日程。</p>}
    </section>
  )
}
