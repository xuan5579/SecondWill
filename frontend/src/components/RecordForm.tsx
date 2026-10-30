import { useState } from 'react'
import { fromLocalInput } from '../lib/datetime'
import type { RecordInput } from '../lib/api'

interface Props {
  onCreate: (input: RecordInput) => Promise<void>
}

/** 新建记录表单。
 *
 * 校验放在前端是为了即时反馈；后端 Pydantic 仍有同样的约束作为最终防线。
 * 结束早于开始这种错误必须在这里拦下 —— 否则会静默存进一条 0 时长的日程，
 * 而冲突检测根本不会去看它。
 */
export default function RecordForm({ onCreate }: Props) {
  const [title, setTitle] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [priority, setPriority] = useState(3)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setTitle('')
    setStart('')
    setEnd('')
    setPriority(3)
    setError(null)
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    const name = title.trim()
    if (!name) {
      setError('请填写要做什么')
      return
    }
    const startIso = fromLocalInput(start)
    const endIso = fromLocalInput(end)
    if (startIso && endIso && new Date(endIso) <= new Date(startIso)) {
      setError('结束时间必须晚于开始时间')
      return
    }
    setBusy(true)
    try {
      await onCreate({ title: name, start_at: startIso, end_at: endIso, priority })
      reset()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="record-form" onSubmit={submit}>
      <input
        value={title}
        onChange={(e) => {
          setTitle(e.target.value)
          setError(null)
        }}
        placeholder="要做什么？"
        aria-label="标题"
      />
      {/* 开始/结束/优先级原本只有 aria-label（屏幕阅读器能读、人眼看不见），
          空值的 datetime-local 会让浏览器画出 yyyy-mm-dd 占位，看上去像没做完。
          这里补上可见标签，与 QuickAdd 的 qa-field 保持一致。 */}
      <label className="rf-field">
        <span>开始</span>
        <input
          type="datetime-local"
          value={start}
          onChange={(e) => setStart(e.target.value)}
          aria-label="开始时间"
        />
      </label>
      <label className="rf-field">
        <span>结束</span>
        <input
          type="datetime-local"
          value={end}
          onChange={(e) => setEnd(e.target.value)}
          aria-label="结束时间"
        />
      </label>
      <label className="rf-field">
        <span>优先级</span>
        <select
          value={priority}
          onChange={(e) => setPriority(Number(e.target.value))}
          aria-label="优先级"
        >
          {[1, 2, 3, 4, 5].map((p) => (
            <option key={p} value={p}>
              P{p}
            </option>
          ))}
        </select>
      </label>
      <button className="btn primary" type="submit" disabled={busy}>
        {busy ? '添加中…' : '添加'}
      </button>
      {error && <div className="field-error wide">{error}</div>}
    </form>
  )
}
