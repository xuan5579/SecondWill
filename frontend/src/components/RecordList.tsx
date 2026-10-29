import { useState } from 'react'
import { fromLocalInput, formatRange, toLocalInput } from '../lib/datetime'
import type { RecordItem, RecordStatus } from '../types/api'

const STATUS_LABEL: Record<RecordStatus, string> = {
  pending: '待办',
  in_progress: '进行中',
  done: '已完成',
  dropped: '已放弃',
}

const STATUS_KEYS: RecordStatus[] = ['pending', 'in_progress', 'done', 'dropped']

interface Props {
  records: RecordItem[]
  conflictIds: Set<string>
  onUpdate: (id: string, draft: RecordDraft) => Promise<void>
  onComplete: (item: RecordItem) => Promise<void>
  onRemove: (id: string) => Promise<void>
}

export interface RecordDraft {
  title: string
  start_at: string | null
  end_at: string | null
  priority: number
  status: RecordStatus
}

function Row({
  item,
  isConflict,
  onUpdate,
  onComplete,
  onRemove,
}: {
  item: RecordItem
  isConflict: boolean
  onUpdate: Props['onUpdate']
  onComplete: Props['onComplete']
  onRemove: Props['onRemove']
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<RecordDraft>({
    title: item.title,
    start_at: item.start_at,
    end_at: item.end_at,
    priority: item.priority,
    status: item.status,
  })
  const [busy, setBusy] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)

  const open = () => {
    setDraft({
      title: item.title,
      start_at: item.start_at,
      end_at: item.end_at,
      priority: item.priority,
      status: item.status,
    })
    setLocalError(null)
    setEditing(true)
  }

  const patch = (next: Partial<RecordDraft>) => {
    setDraft((d) => ({ ...d, ...next }))
    setLocalError(null)
  }

  const save = async () => {
    const title = draft.title.trim()
    if (!title) {
      setLocalError('标题不能为空')
      return
    }
    if (draft.start_at && draft.end_at && new Date(draft.end_at) <= new Date(draft.start_at)) {
      setLocalError('结束时间必须晚于开始时间')
      return
    }
    setBusy(true)
    try {
      await onUpdate(item.id, { ...draft, title })
      setEditing(false)
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (editing) {
    return (
      <article className="record is-editing">
        <div className="edit-grid">
          <input
            autoFocus
            value={draft.title}
            onChange={(e) => patch({ title: e.target.value })}
            aria-label="标题"
            placeholder="标题"
          />
          <input
            type="datetime-local"
            value={toLocalInput(draft.start_at)}
            onChange={(e) => patch({ start_at: fromLocalInput(e.target.value) })}
            aria-label="开始时间"
          />
          <input
            type="datetime-local"
            value={toLocalInput(draft.end_at)}
            onChange={(e) => patch({ end_at: fromLocalInput(e.target.value) })}
            aria-label="结束时间"
          />
          <select
            value={draft.status}
            onChange={(e) => patch({ status: e.target.value as RecordStatus })}
            aria-label="状态"
          >
            {STATUS_KEYS.map((k) => (
              <option key={k} value={k}>
                {STATUS_LABEL[k]}
              </option>
            ))}
          </select>
          <select
            value={draft.priority}
            onChange={(e) => patch({ priority: Number(e.target.value) })}
            aria-label="优先级"
          >
            {[1, 2, 3, 4, 5].map((p) => (
              <option key={p} value={p}>
                P{p}
              </option>
            ))}
          </select>
          <div className="edit-actions">
            <button className="btn small primary" onClick={() => void save()} disabled={busy}>
              {busy ? '保存中…' : '保存'}
            </button>
            <button className="btn small" onClick={() => setEditing(false)} disabled={busy}>
              取消
            </button>
          </div>
        </div>
        {localError && <div className="field-error">{localError}</div>}
      </article>
    )
  }

  return (
    <article
      key={item.id}
      className={`record status-${item.status}${isConflict ? ' has-conflict' : ''}`}
    >
      <div className="record-main">
        <span className="record-title">{item.title}</span>
        <span className="record-time">{formatRange(item.start_at, item.end_at)}</span>
      </div>
      <div className="record-side">
        <span className={`badge badge-${item.status}`}>
          {STATUS_LABEL[item.status] ?? item.status}
        </span>
        <span className="badge muted">P{item.priority}</span>
        {isConflict && <span className="badge badge-soft">冲突</span>}
        {item.tags.map((t) => (
          <span key={t} className="badge tag">
            {t}
          </span>
        ))}
        <button className="btn small" onClick={open}>
          编辑
        </button>
        {item.status !== 'done' && (
          <button className="btn small" onClick={() => void onComplete(item)}>
            完成
          </button>
        )}
        <button className="btn small danger" onClick={() => void onRemove(item.id)}>
          删除
        </button>
      </div>
    </article>
  )
}

export default function RecordList(props: Props) {
  const { records, conflictIds, onUpdate, onComplete, onRemove } = props

  if (records.length === 0) {
    return (
      <section className="record-list">
        <p className="empty">还没有记录。用下面的表单添加一条，或运行 scripts/seed.py 导入示例数据。</p>
      </section>
    )
  }

  return (
    <section className="record-list">
      {records.map((r) => (
        <Row
          key={r.id}
          item={r}
          isConflict={conflictIds.has(r.id)}
          onUpdate={onUpdate}
          onComplete={onComplete}
          onRemove={onRemove}
        />
      ))}
    </section>
  )
}
