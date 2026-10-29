import { useState } from 'react'
import { api } from '../lib/api'
import type { DecisionEnvelope, FeedbackAck, FeedbackKind, RecordItem } from '../types/api'

/** 来源角标 —— P4 最想让人看见的一样东西。
 *
 * 三条来源的含义必须一眼分得清，否则"断网也能用"这件事就无从证明：
 *   llm      = 模型给的
 *   cache    = 命中缓存/录制回放（同一场景不再烧一次调用）
 *   fallback = 模型不可用，本地规则顶上
 * 刻意**不隐藏** fallback：降级不可耻，假装没降级才是。
 */
const SOURCE_LABEL: Record<string, { text: string; hint: string }> = {
  llm: { text: '模型建议', hint: '由大模型给出的方案（已通过后端 10 条校验）' },
  cache: { text: '缓存命中', hint: '同一场景最近算过，直接复用，未再次调用模型' },
  fallback: { text: '规则兜底', hint: '模型不可用，由本地规则引擎生成，宠物情绪不受影响' },
}

const KIND_LABEL: Record<string, string> = {
  move: '挪时间',
  shrink: '缩短',
  drop: '取消',
}

/** 三个反馈按钮的文案刻意不用"好的/稍后/关闭"这类中性词。
 *
 * 「不再提醒这类」必须让用户看清它的分量：点够次数，这一整类建议就再也不会出现。
 * 含糊的文案会让用户误点，然后困惑于"它怎么不说话了"。
 */
const FEEDBACK_LABEL: Record<FeedbackKind, string> = {
  accept: '有用',
  snooze: '稍后提醒',
  mute: '不再提醒这类',
}

interface Props {
  decision: DecisionEnvelope | null
  records: RecordItem[]
  /** 采用建议：直接落一次 PATCH，走既有链路 —— 不另起一条。 */
  onApply: (recordId: string, start: string, end: string) => void
  onDismiss: () => void
  onFeedback?: (kind: FeedbackKind, ack: FeedbackAck) => void
}

function fmt(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(
    d.getHours(),
  ).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export default function InterventionCard({
  decision,
  records,
  onApply,
  onDismiss,
  onFeedback,
}: Props) {
  const [ack, setAck] = useState<FeedbackAck | null>(null)
  const [busy, setBusy] = useState(false)

  if (!decision) return null

  // 触发判定通过 ≠ 送达。节奏闸门（冷却 / 每日上限）或降权可以在最后一步拦下它，
  // 此时后端没有落干预记录，前端也就**没有可反馈的东西** ——
  // 渲染出一张点不动的卡片比不渲染更糟。
  if (decision.delivered === false) return null

  const payload = decision.payload
  const iv = payload.intervention

  // no_action 是契约里的合法出口：模型被允许说"这次不用打扰"，
  // 此时不该渲染任何卡片，否则等于替模型撤销了它的判断。
  if (!iv || payload.decision_type === 'no_action') return null

  const badge = SOURCE_LABEL[decision.source] ?? {
    text: decision.source,
    hint: '',
  }
  const titleOf = (id: string) => records.find((r) => r.id === id)?.title ?? id

  const send = async (kind: FeedbackKind) => {
    if (!decision.intervention_id || busy) return
    setBusy(true)
    try {
      const result = await api.giveFeedback(decision.intervention_id, kind)
      setAck(result)
      onFeedback?.(kind, result)
    } finally {
      setBusy(false)
    }
  }

  const applySuggestion = (recordId: string, start: string, end: string) => {
    onApply(recordId, start, end)
    // 采用即隐性采纳：不必再让用户多点一次"有用"。
    void send('accept')
  }

  // 反馈之后必须让用户看见效果，否则按钮形同虚设
  const ignored = ack?.ignore_counts?.[payload.decision_type] ?? 0
  const threshold = ack?.threshold ?? 3
  const remainingToMute = Math.max(0, threshold - ignored)

  return (
    <section className="intervention" data-source={decision.source}>
      <header className="intervention-head">
        <span className={`src src-${decision.source}`} title={badge.hint}>
          {badge.text}
        </span>
        <h2>{iv.title || payload.summary}</h2>
        <button className="btn small ghost" onClick={onDismiss} title="收起这张卡片">
          收起
        </button>
      </header>

      <p className="intervention-msg">{iv.message}</p>

      {iv.pet_line && (
        <p className="intervention-line">
          <span className="cat-tag">猫说</span>
          {iv.pet_line}
        </p>
      )}

      {payload.suggestions.length > 0 && (
        <ul className="suggestion-list">
          {payload.suggestions.map((s) => (
            <li key={s.id || s.target_record_id}>
              <div className="suggestion-main">
                <span className="kind">{KIND_LABEL[s.kind] ?? s.kind}</span>
                <strong>{titleOf(s.target_record_id)}</strong>
                <span className="when">
                  {fmt(s.proposed_start)} → {fmt(s.proposed_end)}
                </span>
              </div>
              {s.rationale && <div className="rationale">{s.rationale}</div>}
              {s.proposed_start && s.proposed_end && (
                <button
                  className="btn small"
                  onClick={() =>
                    applySuggestion(s.target_record_id, s.proposed_start!, s.proposed_end!)
                  }
                >
                  采用
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      <div className="intervention-feedback">
        <span className="fb-label">这条建议：</span>
        {(['accept', 'snooze', 'mute'] as FeedbackKind[]).map((kind) => (
          <button
            key={kind}
            className={`btn small ghost${ack?.feedback === kind ? ' active' : ''}`}
            disabled={busy || !decision.intervention_id}
            onClick={() => void send(kind)}
            title={
              kind === 'mute'
                ? `同一类建议被忽略 ${threshold} 次后将不再出现`
                : kind === 'snooze'
                  ? '冷却期内不再提醒，之后仍会出现'
                  : '记下这次采纳，模型会更愿意给同类建议'
            }
          >
            {FEEDBACK_LABEL[kind]}
          </button>
        ))}
        {ack && ignored > 0 && remainingToMute > 0 && (
          <span className="fb-hint">
            这类建议已被忽略 {ignored}/{threshold} 次，再有 {remainingToMute} 次就不再提醒
          </span>
        )}
        {ack && remainingToMute === 0 && (
          <span className="fb-hint muted">已记住：这类建议不再出现</span>
        )}
      </div>

      <footer className="intervention-foot">
        <span className="confidence">置信度 {payload.confidence.toFixed(2)}</span>
        {decision.attempts > 0 && <span className="dim">· 调用 {decision.attempts} 次</span>}
        {decision.rhythm && (
          <span className="dim" title="系统每天主动提醒的次数是有上限的">
            · 今日还剩 {decision.rhythm.remaining_today} 次主动提醒
          </span>
        )}
        {payload.caveats.map((c) => (
          <span key={c} className="caveat">
            {c}
          </span>
        ))}
      </footer>
    </section>
  )
}
