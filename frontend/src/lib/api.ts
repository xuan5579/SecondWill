import type {
  AppState,
  BehaviorEvent,
  DecisionEnvelope,
  EventKind,
  FeedbackAck,
  FeedbackKind,
  FeedbackProfile,
  LatestDecision,
  NLParseResult,
  PetState,
  RecordItem,
} from '../types/api'

// 用 127.0.0.1 而不是 localhost：本机 localhost 优先解析到 IPv6 ::1，
// 而后端只绑定 IPv4 127.0.0.1，走 localhost 会先连 ::1:8000 失败。
const BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}${detail ? ` — ${detail}` : ''}`)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export interface RecordInput {
  title: string
  description?: string | null
  start_at?: string | null
  end_at?: string | null
  status?: string
  priority?: number
  tags?: string[]
}

export const api = {
  listRecords: () => request<RecordItem[]>('/records'),

  createRecord: (input: RecordInput) =>
    request<RecordItem>('/records', { method: 'POST', body: JSON.stringify(input) }),

  updateRecord: (id: string, patch: Partial<RecordInput>) =>
    request<RecordItem>(`/records/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),

  deleteRecord: (id: string) => request<void>(`/records/${id}`, { method: 'DELETE' }),

  getPetState: () => request<PetState>('/pet'),

  getState: () => request<AppState>('/state'),

  reportEvents: (events: { kind: EventKind; record_id?: string | null; payload?: object }[]) =>
    request<BehaviorEvent[]>('/events', { method: 'POST', body: JSON.stringify({ events }) }),

  /** 一句话 → 草稿。**这个调用不会创建任何记录**，落库必须走 createRecord。 */
  parseText: (text: string) =>
    request<NLParseResult>('/parse', { method: 'POST', body: JSON.stringify({ text }) }),

  // ---- 决策（P4）----
  /** 触发一次决策。`manual` 绕过触发判定，用于"我就是要问一句"。 */
  createDecision: (trigger: 'manual' | 'schedule_change' | 'procrastination_signal' = 'manual') =>
    request<DecisionEnvelope>('/decisions', {
      method: 'POST',
      body: JSON.stringify({ trigger }),
    }),

  /** 最近一次决策。首屏用它恢复卡片 —— 否则刷新后建议就消失了。 */
  latestDecision: () => request<LatestDecision>('/decisions/latest'),

  // ---- 干预反馈（P5）----
  /** 对一条干预表态。这不只是记一笔：同类被忽略够多次后，
   *  该类型会进 prompt 的负面清单，模型下一轮不再提它。 */
  giveFeedback: (interventionId: string, kind: FeedbackKind) =>
    request<FeedbackAck>(`/interventions/${interventionId}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ kind }),
    }),

  /** 当前反馈画像。用于显示"再忽略 N 次就不再提醒"。 */
  feedbackProfile: () => request<FeedbackProfile>('/interventions/profile'),
}

const API_ROOT = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000/api'

export const exportCsvUrl = `${API_ROOT}/export.csv`

/** SSE 订阅地址。单独导出是因为它要用 EventSource，不能走上面的 fetch 封装。 */
export const streamUrl = `${API_ROOT}/stream`
