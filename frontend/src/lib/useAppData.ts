import { useCallback, useEffect, useRef, useState } from 'react'
import { api, streamUrl } from './api'
import type {
  AppState,
  DecisionEnvelope,
  DecisionPayload,
  LatestDecision,
  RecordItem,
} from '../types/api'

export type ChannelMode = 'connecting' | 'live' | 'polling'

const POLL_MS = Number(import.meta.env.VITE_POLL_MS ?? 5000)

/** 决策数据的形状归一 —— 这是两个来源进 state 前唯一的收口点。
 *
 * 后端有两处给出的是**扁平**结构（payload 的字段摊平在顶层，没有 `payload` 这一层）：
 *   1. SSE 的 `decision` 广播（`services/events.py::publish_decision`）
 *   2. `GET /api/decisions/latest`（它把 `row.payload` 直接展开返回）
 * 而 POST /api/decisions 的响应是**嵌套**的（有 `payload`）。
 * 卡片组件按 DecisionEnvelope 读 `decision.payload.intervention`，
 * 扁平结构下 `payload` 是 undefined，一解引用就抛 TypeError ——
 * 在没有任何 ErrorBoundary 的年代，这一抛就是整页黑屏。
 *
 * 放在这里而不是改后端，是为了不动对外契约：广播与 /latest 的历史形状保持不变。
 */
function normalizeDecision(raw: unknown): DecisionEnvelope {
  const d = (raw ?? {}) as Record<string, unknown>
  /* /latest 恢复的历史决策：落库的 payload 就是完整 envelope（有顶层 payload），
     所以走这条路 —— 但 envelope 是在**落库之前**生成的，里面没有 intervention_id；
     接口把它放在外层的 `id` 里。不补回来的话，刷新后的卡片上「采用」还能用
     （只走 recordId），三个反馈按钮却因 `!decision.intervention_id` 全被锁死，
     点下去零请求、零提示，看起来像坏了。 */
  if (d.payload) {
    const env = d as unknown as DecisionEnvelope
    if (!env.intervention_id && typeof d.id === 'string') env.intervention_id = d.id
    return env
  }

  const pick = <T,>(key: string, fallback: T): T =>
    (d[key] === undefined || d[key] === null ? fallback : d[key]) as T

  return {
    source: pick<DecisionEnvelope['source']>('source', 'fallback'),
    attempts: pick('attempts', 0),
    issues: pick<string[]>('issues', []),
    payload: {
      schema_version: pick('schema_version', '1.0'),
      decision_type: pick<DecisionPayload['decision_type']>('decision_type', 'no_action'),
      confidence: pick('confidence', 0),
      summary: pick('summary', ''),
      reasoning_tags: pick<string[]>('reasoning_tags', []),
      suggestions: pick<DecisionPayload['suggestions']>('suggestions', []),
      intervention: pick<DecisionPayload['intervention']>('intervention', null),
      caveats: pick<string[]>('caveats', []),
    },
    /* /latest 恢复的历史决策顶层只有 `id`（Intervention 主键），
       没有 `intervention_id` —— 那是实时 envelope 的字段名。
       不兜底的话，刷新后卡片的「采用」还能用（只走 recordId），
       而三个反馈按钮会因为 `!decision.intervention_id` 全被锁死，
       点下去零请求、零提示，看起来像坏了。 */
    intervention_id: pick<string | null>('intervention_id', pick<string | null>('id', null)),
    delivered: d.delivered === false ? false : true,
    reason: pick<string | undefined>('reason', undefined),
    triggered: pick<boolean | undefined>('triggered', undefined),
    trigger: pick<string | undefined>('trigger', undefined),
    request_id: pick<string | undefined>('request_id', undefined),
    rhythm: pick<DecisionEnvelope['rhythm']>('rhythm', undefined),
    blocked_by: pick<DecisionEnvelope['blocked_by']>('blocked_by', undefined),
  }
}

/** 应用数据加载 + 实时通道。
 *
 * **降级策略是这里唯一复杂的地方**，值得说明：
 *
 * SSE 断开时我们不 `close()` EventSource，而是**留着它自己重连**
 * （浏览器内置的指数退避比手写的好）。与此同时启动轮询兜底，
 * 让界面在数据上始终不落后。一旦收到服务端的 `hello` 帧（新连接的第一帧），
 * 说明 SSE 已恢复，此时才停掉轮询。
 *
 * 于是三种情况都不会卡：
 * - SSE 正常    → 纯推送，零多余请求
 * - SSE 暂时断开 → 轮询顶上，用户几乎无感
 * - 后端整体挂了 → 轮询同样失败，`error` 会被设置并展示提示
 */
export function useAppData() {
  const [records, setRecords] = useState<RecordItem[]>([])
  const [state, setState] = useState<AppState | null>(null)
  /** state 的版本号：每成功拉到一次 /api/state 就 +1。
   *
   * 干预光流要靠它判断"这一次决策带来的新情绪已经落定了没有" ——
   * 单看 mood 字符串无法区分"还没拉到"和"拉到了但档位没变"。 */
  const [stateSeq, setStateSeq] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [channel, setChannel] = useState<ChannelMode>('connecting')
  // 干预卡片（P4）。单独存是因为它**不是** /api/state 的一部分：
  // 决策是异步到达的事件，而 state 是随时可重算的快照，两者生命周期不同。
  const [decision, setDecision] = useState<DecisionEnvelope | null>(null)

  /** 只记"经 SSE 送达"的决策，并带一个单调序号。
   *
   * 为什么不能直接用 `decision` 判断：`loadLatestDecision()` 在刷新页面时
   * 也会写 `decision`，如果拿它当动画触发器，每次 F5 都会重放一遍光流。
   * 加这一路事件流之后，"新事件"与"回看历史"从源头就分开了 ——
   * 也顺便成了光流去重的外层保险（同一条决策只会有一个序号）。
   */
  const [decisionEvent, setDecisionEvent] = useState<{
    seq: number
    decision: DecisionEnvelope
  } | null>(null)
  const eventSeq = useRef(0)

  // 轮询定时器；恢复 SSE 时要clear，这里用 ref 保存以便跨回调取消
  const pollTimer = useRef<number | null>(null)

  const load = useCallback(async () => {
    try {
      const [list, appState] = await Promise.all([api.listRecords(), api.getState()])
      setRecords(list)
      setState(appState)
      setStateSeq((s) => s + 1)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearInterval(pollTimer.current)
      pollTimer.current = null
    }
  }, [])

  const startPolling = useCallback(() => {
    if (pollTimer.current !== null) return
    pollTimer.current = window.setInterval(() => void load(), POLL_MS)
  }, [load])

  /** 首屏恢复最近一次干预卡片。
   *
   * 刷新页面后卡片会消失 —— 除非主动回看一次。这在演示时很关键：
   * 评审随手 F5 一下，结果"刚才那张建议"不见了，会以为功能不稳定。
   */
  const loadLatestDecision = useCallback(async () => {
    try {
      const latest: LatestDecision = await api.latestDecision()
      if (latest.has_decision) setDecision(normalizeDecision(latest))
    } catch {
      // 决策是旁路：拿不到就当没有，绝不因此让整个页面报错
    }
  }, [])

  useEffect(() => {
    void load()
    void loadLatestDecision()
  }, [load, loadLatestDecision])

  useEffect(() => {
    if (typeof EventSource === 'undefined') {
      // 极老的环境没有 EventSource，直接走轮询
      startPolling()
      setChannel('polling')
      return
    }

    const es = new EventSource(streamUrl)

    // `hello` 是每条新连接的第一帧 —— 收到它代表 SSE 通道活了（含断线后重连成功）
    es.addEventListener('hello', () => {
      setChannel('live')
      stopPolling()
    })

    // 服务端只推"变了"这个信号，不含具体数据；拿到再拉全量 /api/state
    es.addEventListener('state_changed', () => {
      void load()
    })

    // 决策结果**自带卡片内容**（与 state_changed 不同）：
    // 若这里也只推"变了"再让前端去拉，会出现"宠物已经回落但卡片还在说有冲突"的错位。
    es.addEventListener('decision', (ev) => {
      try {
        const next = normalizeDecision(JSON.parse((ev as MessageEvent).data))
        setDecision(next)
        // 只有 SSE 事件才推进序号：干预光流据此判断"这是新送达的一条"
        eventSeq.current += 1
        setDecisionEvent({ seq: eventSeq.current, decision: next })
      } catch {
        void loadLatestDecision()
      }
      void load()
    })

    es.onerror = () => {
      // 不断开、不销毁，交给浏览器自带重连；这里只负责把轮询顶上来
      setChannel('polling')
      startPolling()
    }

    return () => {
      es.close()
      stopPolling()
    }
  }, [load, startPolling, stopPolling])

  return {
    records,
    state,
    /** 每次 /api/state 成功刷新的版本号（光流用它等"新情绪落定"） */
    stateSeq,
    error,
    loading,
    channel,
    decision,
    /** SSE 送达的最新一条决策（带序号）；刷新时回看的历史决策不会出现在这里 */
    decisionEvent,
    setDecision,
    setError,
    reload: load,
  }
}
