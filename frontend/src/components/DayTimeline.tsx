import type { ConflictItem, RecordItem } from '../types/api'

/** 今日时间轴 —— 把"这一天有多满"画出来。
 *
 * 为什么值得占掉左栏一块版面：
 * 日程列表只回答"有哪些事"，回答不了"排得下吗"。冲突检测和负载比
 * 都是这件事的结论，但结论应该长在证据上 —— 这根轴就是证据。
 *
 * 纯前端派生：位置只由 start_at / end_at 与一组本地窗口参数算出，
 * 不请求后端、不参与任何判定。
 *
 * v2 两处变化：
 * ① 叠加"冲突热力带"。后端只给 record_ids + 分钟数，没有起止时刻，
 *    所以用记录自身的区间反算（两两求交 / 求空档）—— 仍是纯派生，不存在第二套口径。
 * ② 时间窗口改成自适应。固定 08:00–23:00 时，如果日程都挤在晚上，
 *    九成画面是空的，色块挤在角落里连标题都看不清。现在窗口贴着日程走
 *    （前后各留一点余量、至少 4 小时），冲突带也就能真的被看见。
 */

const DEFAULT_START_MIN = 8 * 60
const DEFAULT_END_MIN = 23 * 60
/** 窗口最小跨度：只有一条 20 分钟的任务时，别把轴放大成"整整一天都很忙" */
const MIN_SPAN_MIN = 240
const PAD_BEFORE_MIN = 60
const PAD_AFTER_MIN = 30
const DAY_MIN = 24 * 60

interface Props {
  records: RecordItem[]
  /** 后端算好的冲突。缺省时退化成纯日程分布，不画热力带。 */
  conflicts?: ConflictItem[]
}

interface Slot {
  r: RecordItem
  s: number
  e: number
  /** 这一段在轴上真实占据的区间，只给 hover 提示用。
   *  跨午夜拆段之后"整条记录的起止"与"这一段的位置"不是一回事：
   *  次日段画在 00:00，提示却写 22:30–00:30，等于指着 0 点说这是 22:30。 */
  label: string
}

interface Band {
  from: number
  to: number
  hard: boolean
  label: string
}

function minutesOfDay(iso: string): number {
  const d = new Date(iso)
  return d.getHours() * 60 + d.getMinutes()
}

/** 日期键。必须补零：否则 "2026-11-9" > "2026-11-17" 按字符串比较成立，
 *  9 号这天会被判成"比 17 号还晚"，取哪一天来画就错了。 */
function dayKey(iso: string): string {
  const d = new Date(iso)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

const hhmm = (min: number) => {
  const h = Math.floor(min / 60)
  const m = Math.round(min % 60)
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

/** 段内区间的可读写法。1440 正好是 "24:00"，跨午夜拆出来的两段各自成立。 */
const span = (from: number, to: number) => `${hhmm(from)}–${hhmm(to)}`

/** 日期键 +1 天。纯字符串运算：render 期不碰 Date，否则触发
 *  react(purity) 告警（"Cannot call impure function during render"）。 */
function nextDayKey(key: string): string {
  const [y, m, d] = key.split('-').map(Number)
  const leap = (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0
  const dim = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
  const p = (n: number) => String(n).padStart(2, '0')
  if (d < dim) return `${y}-${p(m)}-${p(d + 1)}`
  if (m < 12) return `${y}-${p(m + 1)}-01`
  return `${y + 1}-01-01`
}

/** 贴着日程取窗口：最早开始前留 1 小时、最晚结束后留 30 分钟，
 *  取整到整点，并保证至少 4 小时跨度。 */
function windowOf(slots: Slot[]): [number, number] {
  if (slots.length === 0) return [DEFAULT_START_MIN, DEFAULT_END_MIN]
  const minS = Math.min(...slots.map((x) => x.s))
  const maxE = Math.max(...slots.map((x) => x.e))
  const start = Math.max(0, Math.floor((minS - PAD_BEFORE_MIN) / 60) * 60)
  let end = Math.min(DAY_MIN, Math.ceil((maxE + PAD_AFTER_MIN) / 60) * 60)
  if (end - start < MIN_SPAN_MIN) {
    end = start + MIN_SPAN_MIN
    if (end > DAY_MIN) return [DAY_MIN - MIN_SPAN_MIN, DAY_MIN]
  }
  return [start, end]
}

/** 把冲突翻译成轴上的区间。
 *
 * 后端是两两比较产出的，所以一条冲突通常恰好涉及两条记录；
 * 遇到三条以上就两两求一遍，宁可多画一段也不漏。
 * `overload` 不是"某段时间"，没有横坐标可言，跳过。
 */
function buildBands(conflicts: ConflictItem[], byId: Map<string, Slot>): Band[] {
  const out: Band[] = []
  for (const c of conflicts) {
    if (c.kind === 'overload') continue
    const items = c.record_ids
      .map((id) => byId.get(id))
      .filter((x): x is Slot => Boolean(x))
    if (items.length < 2) continue

    for (let i = 0; i < items.length - 1; i += 1) {
      for (let j = i + 1; j < items.length; j += 1) {
        const a = items[i]
        const b = items[j]
        const from = Math.max(a.s, b.s)
        const to = Math.min(a.e, b.e)

        if (to > from) {
          /* 段是按天拆过的：跨午夜的重叠被午夜切成两半，本日段只有半截。
             标签以"整条记录的绝对重叠"为准，段内数只是本日可见的部分 ——
             否则列表说 60 分钟、轴上写 30，两个数字并排像口径打架。 */
          const absFrom = Math.max(new Date(a.r.start_at!).getTime(), new Date(b.r.start_at!).getTime())
          const absTo = Math.min(new Date(a.r.end_at!).getTime(), new Date(b.r.end_at!).getTime())
          const absMin = Math.max(1, Math.round((absTo - absFrom) / 60_000))
          const segMin = Math.round(to - from)
          out.push({
            from,
            to,
            hard: c.hard,
            label:
              absMin > segMin
                ? `时间重叠 ${absMin} 分钟，本日段 ${segMin} 分钟（其余跨到次日）`
                : `时间重叠 ${segMin} 分钟`,
          })
          continue
        }
        if (c.kind !== 'no_buffer') continue

        // 空档：按开始时间分先后，取前一段的结束 → 后一段的开始。
        // 首尾相接时两者相等（宽度 0），渲染时会给最小宽度，不至于整段消失。
        const first = a.s <= b.s ? a : b
        const second = a.s <= b.s ? b : a
        out.push({
          from: first.e,
          to: Math.max(second.s, first.e),
          hard: false,
          label: `间隔 ${Math.max(0, Math.round(second.s - first.e))} 分钟，切换没有缓冲`,
        })
      }
    }
  }
  return out
}

export default function DayTimeline({ records, conflicts = [] }: Props) {
  const all = records
    .filter((r) => r.start_at && r.end_at && r.status !== 'dropped')
    .flatMap((r) => {
      const s = minutesOfDay(r.start_at!)
      let e = minutesOfDay(r.end_at!)
      const day = dayKey(r.start_at!)
      // 零时长（起止同刻）给 30 分钟兜底，否则宽 0 看不见
      if (e === s) e = s + 30
      if (e > s) return [{ r, s, e, day, label: span(s, e) }]
      /* 跨午夜（23:00 → 01:00）分钟数回绕：e=60 < s=1380。
         之前把 e 兜成 s+30，真实时长被改成 30 分钟，冲突带直接消失。
         只把 e 顺延到"下一天的分钟数"（60+1440）也不够 ——
         day 按 start_at 分天，这条记录只属于今天，它 00:00 之后的部分
         和"明天才开始"的记录永远画不在同一根轴上，两组记录的交集照样算不出来。
         所以跨天的记录要**拆成两段**：今天画到 24:00，明天从 00:00 接着画。
         这样冲突的两条落在同一根轴上，重叠才有得算。 */
      e += DAY_MIN
      const nextDay = dayKey(new Date(new Date(r.start_at!).getTime() + 86_400_000).toISOString())
      return [
        { r, s, e: DAY_MIN, day, label: span(s, DAY_MIN) },
        { r, s: 0, e: e - DAY_MIN, day: nextDay, label: span(0, e - DAY_MIN) },
      ]
    })

  // 取哪一天来画：冲突优先 —— 从今天往后找第一根"画得出冲突带"的轴。
  // 只认"今天"会在晚间踩坑：测试数据按当前时刻生成，晚 9 点后冲突
  // 全部落在午夜之后，今天的轴只剩一条光杆、带子一条没有。
  // 冲突在哪天，证据就画在哪天；标题如实写日期，不冒充"今天"。
  // 没有任何冲突时退回：今天有条目 → 今天，否则之后最近的一天。
  const todayKey = dayKey(new Date().toISOString())
  const days = Array.from(new Set(all.map((x) => x.day))).sort()
  const later = days.filter((d) => d >= todayKey)
  const bandDay = later.find((d) => {
    const slots = all.filter((x) => x.day === d)
    return buildBands(conflicts, new Map(slots.map((x) => [x.r.id, x]))).length > 0
  })
  const shownDay =
    bandDay ?? (days.includes(todayKey) ? todayKey : (later[0] ?? days[days.length - 1] ?? todayKey))
  const isToday = shownDay === todayKey

  const daySlots = all.filter((x) => x.day === shownDay)
  const [winStart, winEnd] = windowOf(daySlots)
  const spanMin = Math.max(1, winEnd - winStart)

  // 只画落在窗口里的条目（自适应之后这条过滤是必须的，不能省）
  const timed = daySlots.filter((x) => x.e > winStart && x.s < winEnd)

  const pct = (m: number) => ((m - winStart) / spanMin) * 100

  const now = new Date()
  const nowMin = now.getHours() * 60 + now.getMinutes()
  const nowPct = pct(nowMin)
  const showNow = isToday && nowMin >= winStart && nowMin <= winEnd

  // 刻度间隔随窗口宽度变：窄窗口按小时，宽窗口每 2~3 小时一格，
  // 否则 15 小时里塞 15 个标签会糊成一片。
  const spanH = spanMin / 60
  const tickStepH = spanH <= 5 ? 1 : spanH <= 10 ? 2 : 3
  const ticks: number[] = []
  for (let h = Math.ceil(winStart / 60); h * 60 <= winEnd; h += tickStepH) ticks.push(h)
  if (winEnd % 60 === 0 && ticks[ticks.length - 1] !== winEnd / 60) ticks.push(winEnd / 60)

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

  // 只画"这一天"的冲突：别的日子的撞车画在这根轴上会是假信息。
  const bands = buildBands(conflicts, new Map(placed.map((x) => [x.r.id, x])))
  const hardBands = bands.filter((b) => b.hard).length
  const softBands = bands.length - hardBands

  const [, mm, dd] = shownDay.split('-')
  const tomorrowKey = nextDayKey(todayKey)

  return (
    <section className={`day-axis${bands.length > 0 ? ' has-bands' : ''}`}>
      <div className="day-axis-head">
        {/* 轴跳到别的日子时，标题必须如实写清是哪天 —— 画的是次日凌晨的
            冲突却在标题里写"今天"，观众第一眼就会觉得对不上。 */}
        <h2>
          {isToday
            ? '今天的时间分布'
            : shownDay === tomorrowKey
              ? '明天的时间分布'
              : `${Number(mm)}-${dd} 的时间分布`}
        </h2>
        <div className="day-axis-legend">
          {hardBands > 0 && (
            <span className="day-axis-flag is-hard" title="时间真的重叠了，必须重排">
              <i />
              撞车 {hardBands}
            </span>
          )}
          {softBands > 0 && (
            <span className="day-axis-flag is-soft" title="没有重叠，但两件事之间没有缓冲">
              <i />
              太紧 {softBands}
            </span>
          )}
          <span className="day-axis-range">
            {hhmm(winStart)} — {hhmm(winEnd)}
          </span>
        </div>
      </div>

      {/* 网格按 30 分钟一格，格宽随窗口跨度算 —— 写死 30 格只在 15 小时窗口下才对 */}
      <div
        className="day-axis-track"
        style={{
          height: 20 + laneCount * 15,
          backgroundSize: `${(30 / spanMin) * 100}% 100%`,
        }}
      >
        {/* 密度底色：每条日程按自身区间叠一层极淡的紫，
            重叠或紧邻的地方自然叠得更深 —— 不额外算任何东西，
            "哪里满、哪里空"就有了底色，而不是只有几根色条浮在黑板上。
            画在最底层：网格、冲突带、色块都要压在它上面。 */}
        {placed.map(({ r, s, e }) => {
          const left = Math.max(0, pct(s))
          const width = Math.max(0.6, Math.min(100 - left, pct(e) - left))
          return (
            <span
              key={`den-${r.id}-${s}`}
              className="day-axis-density"
              style={{ left: `${left}%`, width: `${width}%` }}
            />
          )
        })}

        {/* 冲突热力带：压在最上层并用 screen 混合，
            画在色块下面会被不透明的色条完全盖住（第一版就是这么白做的） */}
        {bands.map((b, i) => {
          const left = Math.max(0, Math.min(100, pct(b.from)))
          const right = Math.max(left, Math.min(100, pct(b.to)))
          return (
            <span
              key={`band-${i}`}
              className={`day-axis-band${b.hard ? ' is-hard' : ' is-soft'}`}
              style={{ left: `${left}%`, width: `${Math.max(0.4, right - left)}%` }}
              title={b.label}
            />
          )
        })}

        {/* 整点刻度 */}
        {ticks.map((h) => (
          <span key={h} className="day-axis-tick" style={{ left: `${pct(h * 60)}%` }} />
        ))}

        {placed.map(({ r, s, e, lane, label }) => {
          const left = Math.max(0, pct(s))
          const width = Math.max(1.2, Math.min(100 - left, pct(e) - left))
          return (
            <span
              key={`${r.id}-${s}`}
              className={`day-axis-bar pri-${r.priority}${r.status === 'done' ? ' is-done' : ''}`}
              style={{ left: `${left}%`, width: `${width}%`, top: 5 + lane * 15 }}
              title={`${r.title}  ${label}`}
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
        {ticks.map((h) => (
          <span key={h} style={{ left: `${pct(h * 60)}%` }}>
            {h}:00
          </span>
        ))}
      </div>

      {timed.length === 0 && (
        <p className="day-axis-empty">
          {isToday ? '今天' : `${Number(mm)}-${dd}`}还没有排定时间的日程。
        </p>
      )}
    </section>
  )
}
