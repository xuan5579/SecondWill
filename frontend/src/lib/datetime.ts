/** 时间转换工具。

后端统一用 ISO8601 带时区字符串，而 `<input type="datetime-local">` 要求
"YYYY-MM-DDTHH:mm" 这种本地无时区格式。两者必须显式转换，
不能用 `new Date(iso)` 直接塞给 input —— 那样拿到的是 UTC 时刻的字符串，
在东八区会整体偏移 8 小时（这正是本项目已在 P1 踩过的坑）。
*/

const pad = (n: number) => String(n).padStart(2, '0')

/** ISO8601 → datetime-local 输入框所需的本地字符串。 */
export function toLocalInput(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`
}

/** datetime-local 的值 → ISO8601（带本地时区偏移）。 */
export function fromLocalInput(value: string): string | null {
  if (!value) return null
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return null
  return d.toISOString()
}

const WEEKDAY = ['日', '一', '二', '三', '四', '五', '六']

/** 相对今天的天数差；用于把 '2026-10-29' 显示成「明天」。 */
function dayDiff(d: Date, today: Date): number {
  const a = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())
  const b = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate())
  return Math.round((a - b) / 86400000)
}

/** 人类可读的区间文案，跨天时补上结束日期。 */
export function formatRange(startIso: string | null, endIso: string | null, now = new Date()): string {
  if (!startIso) return '未定时'
  const s = new Date(startIso)
  const tail = `${pad(s.getHours())}:${pad(s.getMinutes())}`

  const diff = dayDiff(s, now)
  const dayName =
    diff === 0 ? '今天' : diff === 1 ? '明天' : diff === 2 ? '后天' : `周${WEEKDAY[s.getDay()]}`
  const head = `${dayName} ${pad(s.getMonth() + 1)}-${pad(s.getDate())}`

  if (!endIso) return `${head} ${tail} 起`
  const e = new Date(endIso)
  const sameDay = dayDiff(e, s) === 0
  const endText = sameDay
    ? `${pad(e.getHours())}:${pad(e.getMinutes())}`
    : `${pad(e.getMonth() + 1)}-${pad(e.getDate())} ${pad(e.getHours())}:${pad(e.getMinutes())}`
  return `${head} ${tail} → ${endText}`
}
