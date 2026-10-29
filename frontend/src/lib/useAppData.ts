import { useCallback, useEffect, useRef, useState } from 'react'
import { api, streamUrl } from './api'
import type { AppState, DecisionEnvelope, LatestDecision, RecordItem } from '../types/api'

export type ChannelMode = 'connecting' | 'live' | 'polling'

const POLL_MS = Number(import.meta.env.VITE_POLL_MS ?? 5000)

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
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [channel, setChannel] = useState<ChannelMode>('connecting')
  // 干预卡片（P4）。单独存是因为它**不是** /api/state 的一部分：
  // 决策是异步到达的事件，而 state 是随时可重算的快照，两者生命周期不同。
  const [decision, setDecision] = useState<DecisionEnvelope | null>(null)

  // 轮询定时器；恢复 SSE 时要clear，这里用 ref 保存以便跨回调取消
  const pollTimer = useRef<number | null>(null)

  const load = useCallback(async () => {
    try {
      const [list, appState] = await Promise.all([api.listRecords(), api.getState()])
      setRecords(list)
      setState(appState)
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
      if (latest.has_decision) setDecision(latest)
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
        setDecision(JSON.parse((ev as MessageEvent).data) as DecisionEnvelope)
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
    error,
    loading,
    channel,
    decision,
    setDecision,
    setError,
    reload: load,
  }
}
