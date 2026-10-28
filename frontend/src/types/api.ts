/** 与后端 API 对应的类型定义。
 *
 * 时间与后端约定一致：ISO8601 带时区字符串，由后端统一转 UTC 存储。
 */

export type RecordStatus = 'pending' | 'in_progress' | 'done' | 'dropped'

export interface RecordItem {
  id: string
  type: string
  title: string
  description: string | null
  start_at: string | null
  end_at: string | null
  status: RecordStatus
  priority: number
  tags: string[]
  created_at: string
  updated_at: string
}

export interface PetState {
  mood: string
  energy: number
  streak_days: number
  line: string | null
  computed_at: string
}

/** 三类冲突。hard=true 表示物理重叠、必须重排；软冲突只供 LLM 权衡。 */
export interface ConflictItem {
  record_ids: string[]
  kind: 'overlap' | 'no_buffer' | 'overload'
  hard: boolean
  overlap_minutes: number
  load_ratio: number | null
  detail: Record<string, unknown>
}

export interface Signals {
  conflicts: ConflictItem[]
  procrastination_index: number
  load_ratio: number
  idle_blocks: { start: string; end: string }[]
  completion_rate_7d: number
  completion_rate_today: number
  overdue_count: number
  avg_overdue_days: number
  streak_days: number
  due_total_7d: number
  computed_at: string
}

/** /api/state 一次性返回，避免前端为同一份数据发两次请求。 */
export interface AppState {
  signals: Signals
  pet: PetState
  pet_reason: string
}

// ==================================================================
// 决策与干预（P4）
//
// `source` 是本期的可讲点：同一张卡片，模型在/不在都能出现，
// 区别只在它是谁给的。前端把它渲染成角标，而不是悄悄藏起来。
// ==================================================================

export type DecisionSource = 'llm' | 'cache' | 'fallback'
export type DecisionType = 'reschedule' | 'nudge' | 'no_action'
export type SuggestionKind = 'move' | 'shrink' | 'drop'

export interface Suggestion {
  id: string
  kind: SuggestionKind
  target_record_id: string
  proposed_start: string | null
  proposed_end: string | null
  rationale: string | null
  risk: 'low' | 'medium' | 'high'
}

export interface Intervention {
  type: 'reschedule_proposal' | 'nudge'
  title: string
  message: string
  pet_line: string | null
  tone: 'gentle' | 'direct' | 'playful'
  expires_at: string | null
  actions: { label: string; kind: 'accept' | 'snooze' | 'mute' }[]
}

export interface DecisionPayload {
  schema_version: string
  decision_type: DecisionType
  confidence: number
  summary: string
  reasoning_tags: string[]
  suggestions: Suggestion[]
  intervention: Intervention | null
  caveats: string[]
}

/** POST /api/decisions 与 SSE `decision` 事件的共用结构。 */
export interface DecisionEnvelope {
  request_id?: string
  source: DecisionSource
  attempts: number
  issues: string[]
  payload: DecisionPayload
  intervention_id?: string | null
  triggered?: boolean
  /** P5：触发判定通过≠送达，节奏闸门或降权可以把它拦在最后一步。 */
  delivered?: boolean
  reason?: string
  blocked_by?: '' | 'cooldown' | 'daily_limit' | 'muted'
  rhythm?: RhythmStatus
  /** 后端回传的触发来源。手动触发不占今日额度，所以要看得见。 */
  trigger?: string
}

/** 节奏闸门状态。显示"今日还剩几次"不是为了炫技，
 *  而是让用户知道系统**在有意识地克制** —— 这本身就是产品信息。 */
export interface RhythmStatus {
  used_today: number
  remaining_today: number
  blocked_by: string
  next_allowed_at: string | null
}

/** GET /api/decisions/latest：刷新后仍能回看到最后一次建议。 */
export interface LatestDecision extends DecisionEnvelope {
  has_decision: boolean
  id?: string
  created_at?: string
}

// ==================================================================
// 干预反馈（P5）
// ==================================================================

export type FeedbackKind = 'accept' | 'snooze' | 'mute'

export interface FeedbackProfile {
  muted_intervention_types: string[]
  accepted_intervention_types: string[]
  ignore_counts: Record<string, number>
  accept_counts: Record<string, number>
  threshold: number
}

export interface FeedbackAck {
  ok: boolean
  intervention_id: string
  feedback: FeedbackKind
  muted_intervention_types: string[]
  ignore_counts: Record<string, number>
  accept_counts: Record<string, number>
  threshold: number
}

export type EventKind =
  | 'task_started'
  | 'task_delayed'
  | 'task_abandoned'
  | 'task_completed'
  | 'intervention_accepted'
  | 'intervention_ignored'

export interface BehaviorEvent {
  id: string
  kind: EventKind
  record_id: string | null
  at: string
  payload: Record<string, unknown>
}

// ==================================================================
// 自然语言解析（P3-d）
//
// 这套类型是 P3 与 P4 的**共同契约**：P4 接入 LLM 后字段一个都不变，
// 前端草稿卡片因此不需要任何改动。
// ==================================================================

export type NLStatus = 'resolved' | 'need_user' | 'error'
export type NLSource = 'rule' | 'llm' | 'cache'
export type NLField = 'title' | 'start_at' | 'end_at' | 'priority'

/** 拿不到的字段一律 null —— 解析器不许静默填默认值。 */
export interface NLDraft {
  title: string | null
  start_at: string | null
  end_at: string | null
  priority: number
}

/** 解析器替用户做的推断，必须在卡片上标黄展示。 */
export interface NLAssumption {
  code: string
  label: string
  field: NLField
}

export interface NLError {
  code: string
  message: string
  hint: string | null
}

export interface NLParseResult {
  status: NLStatus
  source: NLSource
  draft: NLDraft
  missing: NLField[]
  assumptions: NLAssumption[]
  error: NLError | null
  confidence: number
  text: string
}
