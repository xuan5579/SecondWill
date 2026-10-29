import { useState } from 'react'
import { api, type RecordInput } from '../lib/api'
import { fromLocalInput, toLocalInput } from '../lib/datetime'
import type { NLParseResult } from '../types/api'

/** 演示用例句 —— 与后端 `services/nl_spec.py` 的《支持清单》一致。
 *  演示时只点这些，因为清单外的写法我们明确不承诺能解析。 */
const DEMO_SENTENCES = [
  '明天上午9点到11点开会',
  '后天下午2点到3点面试高优先级',
  '今天晚上11点到明天凌晨1点值班',
  '11月20日23:30到11月21日1:00上线',
  '明天9:00到10:30写项目方案',
  '后天晚上8点看电影',
  '月底前交季度报告',
]

const SOURCE_LABEL: Record<string, string> = {
  rule: '本地规则',
  llm: '模型',
  cache: 'DEMO 回放',
}

interface Props {
  onCreate: (input: RecordInput) => Promise<void>
}

/**
 * 一句话添加 + 草稿确认。
 *
 * 这里是四条硬约束的落点：
 * - ② 解析结果只做草稿，**必须点确认才入库**
 * - ①/③ 解析不出来的字段留空（missing）或标黄告知（assumptions），绝不静默填充
 * - ④ 确认走的是既有的 `createRecord`（POST /api/records），自动触发冲突检测与宠物更新
 */
export default function QuickAdd({ onCreate }: Props) {
  const [text, setText] = useState('')
  const [result, setResult] = useState<NLParseResult | null>(null)
  const [title, setTitle] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [priority, setPriority] = useState(3)
  const [parsing, setParsing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setResult(null)
    setTitle('')
    setStart('')
    setEnd('')
    setError(null)
  }

  const parse = async (raw: string) => {
    const sentence = raw.trim()
    if (!sentence) return
    setParsing(true)
    setError(null)
    setResult(null)
    try {
      const r = await api.parseText(sentence)
      setResult(r)
      // 解析失败时不切到草稿态：让用户直接看到 error 卡片并改句子
      if (r.status !== 'error') {
        setTitle(r.draft.title ?? '')
        setStart(toLocalInput(r.draft.start_at))
        setEnd(toLocalInput(r.draft.end_at))
        setPriority(r.draft.priority)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setParsing(false)
    }
  }

  const confirm = async () => {
    if (!result) return
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
    setSaving(true)
    try {
      // 走的就是常规建记录接口 —— 冲突检测、宠物更新由后端既有链路负责
      await onCreate({ title: name, start_at: startIso, end_at: endIso, priority })
      setText('')
      reset()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const missing = new Set(result?.missing ?? [])

  return (
    <section className="quick-add">
      <div className="qa-input-row">
        <input
          className="qa-input"
          value={text}
          onChange={(e) => {
            setText(e.target.value)
            setError(null)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              void parse(text)
            }
          }}
          placeholder="一句话添加：明天下午3点到5点评审PR"
          aria-label="自然语言输入"
        />
        <button
          className="btn primary"
          onClick={() => void parse(text)}
          disabled={parsing || !text.trim()}
        >
          {parsing ? '解析中…' : '解析'}
        </button>
      </div>

      <div className="qa-chips">
        <span className="qa-chips-label">演示例句</span>
        {DEMO_SENTENCES.map((s) => (
          <button
            key={s}
            className="chip"
            onClick={() => {
              setText(s)
              void parse(s)
            }}
            title="点击直接解析"
          >
            {s}
          </button>
        ))}
      </div>

      {error && <div className="field-error wide">{error}</div>}

      {result?.status === 'error' && result.error && (
        <div className="qa-card qa-card-error">
          <div className="qa-card-head">
            <span className="qa-badge err">{result.error.code}</span>
            <strong>这句话解析不了</strong>
          </div>
          <p className="qa-msg">{result.error.message}</p>
          {result.error.hint && <p className="qa-hint">建议：{result.error.hint}</p>}
        </div>
      )}

      {result && result.status !== 'error' && (
        <div className="qa-card">
          <div className="qa-card-head">
            <span className={`qa-badge ${missing.size ? 'warn' : 'ok'}`}>
              {missing.size ? '待补充' : '可直接确认'}
            </span>
            <span className="qa-source">
              来源：{SOURCE_LABEL[result.source] ?? result.source} · 置信度{' '}
              {Math.round(result.confidence * 100)}%
            </span>
          </div>

          <div className="qa-grid">
            <label className={missing.has('title') ? 'qa-field missing' : 'qa-field'}>
              <span>标题{missing.has('title') && <em>未识别</em>}</span>
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="要做什么？"
              />
            </label>
            <label className={missing.has('start_at') ? 'qa-field missing' : 'qa-field'}>
              <span>开始{missing.has('start_at') && <em>未识别</em>}</span>
              <input
                type="datetime-local"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </label>
            <label className={missing.has('end_at') ? 'qa-field missing' : 'qa-field'}>
              <span>
                结束
                {missing.has('end_at') && <em>请填写 —— 不替你假设时长</em>}
              </span>
              <input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} />
            </label>
            <label className="qa-field">
              <span>优先级</span>
              <select value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
                {[1, 2, 3, 4, 5].map((p) => (
                  <option key={p} value={p}>
                    P{p}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {result.assumptions.length > 0 && (
            <div className="qa-assumptions">
              <span className="qa-assumptions-label">以下为系统推断，请确认</span>
              {result.assumptions.map((a) => (
                <span className="chip assume" key={a.code}>
                  {a.label}
                </span>
              ))}
            </div>
          )}

          <div className="qa-actions">
            <button className="btn primary" onClick={() => void confirm()} disabled={saving}>
              {saving ? '添加中…' : '确认添加'}
            </button>
            <button className="btn small" onClick={reset}>
              取消
            </button>
            <span className="qa-note">
              确认后走常规建记录流程，冲突检测与宠物状态会自动更新
            </span>
          </div>
        </div>
      )}
    </section>
  )
}
