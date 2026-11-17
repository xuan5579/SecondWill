import { useState, type CSSProperties } from 'react'
import { fromLocalInput, formatRange, toLocalInput } from '../lib/datetime'
import type { RecordItem, RecordStatus } from '../types/api'

const STATUS_LABEL: Record<RecordStatus, string> = {
  pending: '待办',
  in_progress: '进行中',
  done: '已完成',
  dropped: '已放弃',
}

const STATUS_KEYS: RecordStatus[] = ['pending', 'in_progress', 'done', 'dropped']

/** 光流命中：由干预光流在抵达瞬间传入，900ms 后由 App 清除。
 *  卡片只负责"亮起来"，不关心是谁、为什么点亮。 */
export interface StreamHit {
  id: string
  color: string
}

interface Props {
  records: RecordItem[]
  conflictIds: Set<string>
  onUpdate: (id: string, draft: RecordDraft) => Promise<void>
  onComplete: (item: RecordItem) => Promise<void>
  onRemove: (id: string) => Promise<void>
  hit?: StreamHit | null
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
  index,
  isConflict,
  onUpdate,
  onComplete,
  onRemove,
  hit,
}: {
  item: RecordItem
  /** 第几张卡 —— 只用来错开首屏进场，不参与任何业务判断 */
  index: number
  isConflict: boolean
  onUpdate: Props['onUpdate']
  onComplete: Props['onComplete']
  onRemove: Props['onRemove']
  hit?: StreamHit | null
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
      <article className="record is-editing" data-record-id={item.id}>
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

  const hitNow = hit?.id === item.id

  return (
    <article
      key={item.id}
      data-record-id={item.id}
      className={`record status-${item.status}${isConflict ? ' has-conflict' : ''}${
        hitNow ? ' is-stream-hit' : ''
      }`}
      /* 进场错峰：第 n 张延后 n×55ms，封顶 8 档（440ms）——
         列表再长也不让最后一张等太久，否则看着像加载慢。
         只在首次挂载时生效（React 复用同一节点，状态变化不会重播），
         所以勾选完成不会让整列重新"跳"一遍。 */
      style={
        {
          animationDelay: `${Math.min(index, 8) * 55}ms`,
          ...(hitNow ? { '--stream-color': hit.color } : {}),
        } as CSSProperties
      }
    >
      {/* 卡片正文本身就是"打开这条"的入口 —— 复用已有编辑态，不另起一层浮层。
          右侧已经排了「编辑 / 完成 / 删除」三个按钮，再加一个弹出气泡就是两套入口，
          还会跟光流命中、宠物气泡抢同一块空间。 */}
      <div
        className="record-main"
        role="button"
        tabIndex={0}
        aria-label={`编辑：${item.title}`}
        title="点击修改这条日程"
        onClick={open}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            open()
          }
        }}
      >
        <span className="record-title">{item.title}</span>
        <span className="record-time">{formatRange(item.start_at, item.end_at)}</span>
      </div>
      <div className="record-side">
        <span className={`badge badge-${item.status}`}>
          {STATUS_LABEL[item.status] ?? item.status}
        </span>
        <span className="badge pri" data-p={item.priority}>
          P{item.priority}
        </span>
        {isConflict && <span className="badge badge-soft">冲突</span>}
        {/* 标签里如果本来就写着"冲突"，此时已经有一个冲突徽标了，不重复渲染 */}
        {item.tags
          .filter((t) => !(isConflict && t === '冲突'))
          .map((t) => (
            <span key={t} className="badge tag">
              {t}
            </span>
          ))}
        <button className="btn small edit-btn" onClick={open}>
          编辑
        </button>
        {item.status !== 'done' && (
          <button className="btn small" onClick={() => void onComplete(item)}>
            完成
          </button>
        )}
        <button className="btn small danger del-btn" onClick={() => void onRemove(item.id)}>
          删除
        </button>
      </div>
    </article>
  )
}

export default function RecordList(props: Props) {
  const { records, conflictIds, onUpdate, onComplete, onRemove, hit } = props

  if (records.length === 0) {
    return (
      <section className="record-list">
        <p className="empty">还没有记录。用下面的表单添加一条，或运行 scripts/seed.py 导入示例数据。</p>
      </section>
    )
  }

  return (
    <section className="record-list">
      {records.map((r, i) => (
        <Row
          key={r.id}
          item={r}
          index={i}
          isConflict={conflictIds.has(r.id)}
          onUpdate={onUpdate}
          onComplete={onComplete}
          onRemove={onRemove}
          hit={hit}
        />
      ))}
    </section>
  )
}
