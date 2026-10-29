import type { ConflictItem, RecordItem } from '../types/api'

const KIND_LABEL: Record<ConflictItem['kind'], string> = {
  overlap: '时间重叠',
  no_buffer: '间隔过短',
  overload: '当日超载',
}

interface Props {
  conflicts: ConflictItem[]
  records: RecordItem[]
}

function titleOf(records: RecordItem[], id: string): string {
  return records.find((r) => r.id === id)?.title ?? id
}

export default function ConflictList({ conflicts, records }: Props) {
  if (conflicts.length === 0) return null

  return (
    <section className="conflict-list">
      {conflicts.map((c, i) => (
        <div key={`${c.kind}-${i}`} className={`conflict ${c.hard ? 'is-hard' : 'is-soft'}`}>
          <span className={`badge ${c.hard ? 'badge-hard' : 'badge-soft'}`}>
            {KIND_LABEL[c.kind]}
          </span>
          <span className="conflict-detail">
            {c.kind === 'overload'
              ? `负载 ${c.load_ratio?.toFixed(2)}× · 超出约 ${
                  typeof c.detail.used_minutes === 'number'
                    ? Math.round(c.detail.used_minutes / 60)
                    : '?'
                } 小时`
              : `重叠 ${c.overlap_minutes} 分钟`}
          </span>
          {c.kind !== 'overload' && (
            <span className="conflict-ids">
              {c.record_ids.map((id) => titleOf(records, id)).join('  ×  ')}
            </span>
          )}
        </div>
      ))}
    </section>
  )
}
