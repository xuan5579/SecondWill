import { useEffect, useRef, useState } from 'react'
import ConflictList from './components/ConflictList'
import DayTimeline from './components/DayTimeline'
import InterventionCard from './components/InterventionCard'
import InterventionStream from './components/InterventionStream'
import PetPanel from './components/PetPanel'
import QuickAdd from './components/QuickAdd'
import RecordForm from './components/RecordForm'
import RecordList, { type RecordDraft, type StreamHit } from './components/RecordList'
import { exportCsvUrl } from './lib/api'
import { api, type RecordInput } from './lib/api'
import type { PetPulseKind } from './lib/petPulse'
import type { DecisionEnvelope, RecordItem } from './types/api'
import { applyTheme, readTheme, type Theme } from './lib/theme'
import { useAppData } from './lib/useAppData'
import { useInterventionStream } from './lib/useInterventionStream'

/** 拍摄/预览模式：`?stream=demo`。模块级求值一次，服务端渲染不存在（纯 Vite SPA）。 */
const STREAM_DEMO =
  typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('stream')

/** 包一层统一的错误上报：任何写操作失败都走这里，避免每个回调重复 try/catch 的流程代码。 */
function useActions(reload: () => Promise<void>, setError: (msg: string | null) => void) {
  const run = async (fn: () => Promise<void>) => {
    try {
      await fn()
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  return run
}

export default function App() {
  const {
    records,
    state,
    stateSeq,
    error,
    loading,
    channel,
    decision,
    decisionEvent,
    setDecision,
    setError,
    reload,
  } = useAppData()
  const [theme, setTheme] = useState<Theme>(readTheme)
  /** 闸门/降权拦下干预时的说明文案。它不是错误，所以不走 error 通道。 */
  const [hint, setHint] = useState<string | null>(null)
  /** 模型往返实测约 9 秒。这期间界面毫无变化，会被当成"点了没反应" ——
   *  所以按钮必须自己说清楚在忙，而不是靠一张全屏遮罩（那会挡住演示画面）。 */
  const [asking, setAsking] = useState(false)
  const run = useActions(reload, setError)

  /* ---- 宠物脉冲（A1）：光核对用户操作的即时回应。
   * 这是纯视觉层：脉冲先于请求结果发出 —— 用户按下的一瞬间就得到反馈；
   * 若请求随后失败，error 横幅自己会说明，视觉上不必回收这个脉冲。 */
  const [petPulse, setPetPulse] = useState<{ kind: PetPulseKind; id: number } | null>(null)
  const pulseSeq = useRef(0)
  const emitPulse = (kind: PetPulseKind) => setPetPulse({ kind, id: (pulseSeq.current += 1) })

  /* ---- 干预光流：SSE 决策 → 猫头中心 → 目标日程卡片中心 → 卡片出现。
   *
   * 卡片不再"一到达就渲染"，而是等光点真的落在卡片上再放出来。
   * 这样"系统把建议递给某条日程"才是看得见的因果，而不是一张凭空出现的弹窗。
   * 全部时序在 useInterventionStream 里，这里只负责把结果挂回界面。 */
  const [revealedDecision, setRevealedDecision] = useState<DecisionEnvelope | null>(null)
  const [streamHit, setStreamHit] = useState<StreamHit | null>(null)
  const hitTimer = useRef<number | null>(null)

  const { flight, submit, arrive } = useInterventionStream({
    mood: state?.pet.mood ?? 'normal',
    stateSeq,
    records,
    // 起飞瞬间先收掉上一张卡片，避免旧建议与新光流同时占着右栏
    onStart: () => setRevealedDecision(null),
    onHit: (recordId, color) => {
      if (hitTimer.current !== null) window.clearTimeout(hitTimer.current)
      setStreamHit({ id: recordId, color: color.glow })
      // 与 CSS 的 iv-hit 对齐：闪光结束即摘类，卡片上不残留命中态
      hitTimer.current = window.setTimeout(() => setStreamHit(null), 950)
    },
    onReveal: setRevealedDecision,
  })

  // 只有 SSE 送达的新决策才触发动画；刷新回看的历史决策不进这条通道
  useEffect(() => {
    if (decisionEvent) submit(decisionEvent.decision)
  }, [decisionEvent, submit])

  // 刷新后仍能看见最近一张卡片（既有能力），但不重放光流 —— 它不是"刚发生"。
  // 预览模式跳过：否则旧卡片会先出现，把"卡片在光点抵达后才出现"的时序演示搞乱。
  const restored = useRef(false)
  useEffect(() => {
    if (STREAM_DEMO) return
    if (!restored.current && decision && !decisionEvent) {
      restored.current = true
      setRevealedDecision(decision)
    }
  }, [decision, decisionEvent])

  /* ---- 拍摄/演示用：地址栏加 `?stream=demo` --------------------------------
   * 用一条**合成决策**预览光流，不调模型、不写库、不参与任何判定。
   * 没有它就得先制造一次真冲突才能看到动画，调色和拍摄都不可控。
   * 边界：只在带参时生效；id 固定为 demo-preview（StrictMode 下重复触发会被
   * `submit` 按 id 去重）；target 只取列表里第一条真实记录。
   */
  const demoOnce = useRef(false)
  useEffect(() => {
    if (demoOnce.current || records.length === 0) return
    if (!new URLSearchParams(window.location.search).has('stream')) return

    const timer = window.setTimeout(() => {
      demoOnce.current = true
      // 预览也走一次真实的 state 刷新：光流要等"新情绪落定"，
      // 没有这一步它就只能在超时后才起飞，看起来像卡了一下。
      void reload()
      const target = records[0]
      const demo: DecisionEnvelope = {
        source: 'llm',
        attempts: 1,
        issues: [],
        intervention_id: 'demo-preview',
        delivered: true,
        trigger: 'demo',
        payload: {
          schema_version: '1.0',
          decision_type: 'reschedule',
          confidence: 0.86,
          summary: '预览：把这条日程挪开 40 分钟',
          reasoning_tags: ['demo'],
          suggestions: [
            {
              id: 'demo-s1',
              kind: 'move',
              target_record_id: target.id,
              proposed_start: target.start_at,
              proposed_end: target.end_at,
              rationale: '合成建议，仅用于预览光流',
              risk: 'low',
            },
          ],
          intervention: {
            type: 'reschedule_proposal',
            title: '预览：干预光流',
            message: '这条卡片是在光点抵达日程之后才出现的 —— 时序就是产品要讲的那条因果链。',
            pet_line: null,
            tone: 'gentle',
            expires_at: null,
            actions: [],
          },
          caveats: ['合成数据，仅用于预览'],
        },
        rhythm: { used_today: 0, remaining_today: 3, blocked_by: '', next_allowed_at: null },
      }
      submit(demo)
    }, 600)

    return () => window.clearTimeout(timer)
  }, [records, submit, reload])

  useEffect(
    () => () => {
      if (hitTimer.current !== null) window.clearTimeout(hitTimer.current)
    },
    [],
  )

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  const onCreate = (input: RecordInput) => {
    emitPulse('record_added')
    return run(() => api.createRecord(input).then(() => undefined))
  }

  const onUpdate = (id: string, draft: RecordDraft) => {
    // 完成走"心跳"反应；只动时间的编辑走"信号扰动"反应
    emitPulse(draft.status === 'done' ? 'task_done' : 'record_moved')
    return run(async () => {
      await api.updateRecord(id, draft)
      // 状态变为 done 时补一条行为事件 —— 这是 LLM 唯一的输入来源
      if (draft.status === 'done') {
        await api.reportEvents([{ kind: 'task_completed', record_id: id }])
      }
    })
  }

  const onComplete = (item: RecordItem) => {
    emitPulse('task_done')
    return run(async () => {
      await api.updateRecord(item.id, { status: 'done' })
      await api.reportEvents([{ kind: 'task_completed', record_id: item.id }])
    })
  }

  const onRemove = (id: string) => run(() => api.deleteRecord(id))

  /** 手动问一次决策器。手动触发绕过触发判定，但仍受节奏闸门约束。 */
  const onAskDecision = () => {
    if (asking) return
    setAsking(true)
    void run(async () => {
      const res = await api.createDecision('manual')
      if (res.delivered === false) {
        // 被闸门拦下时后端没有落干预记录，也就没有可反馈的卡片。
        // 但"为什么没给建议"必须说出来 —— 沉默比拒绝更让人困惑。
        setHint(res.reason ?? '本次未送达')
        setDecision(null)
        setRevealedDecision(null)
      } else {
        setHint(null)
        setDecision(res)
        // 手动问的这条也走光流；若 SSE 已先一步广播同一条，submit 会按 id 去重
        submit(res)
      }
    }).finally(() => setAsking(false))
  }

  /** 采用建议：直接改日程，走与手动编辑完全相同的链路 —— 不另起一条。 */
  const onApplySuggestion = (recordId: string, start: string, end: string) => {
    emitPulse('record_moved')
    return run(() =>
      api.updateRecord(recordId, { start_at: start, end_at: end }).then(() => undefined),
    )
  }

  const conflicts = state?.signals.conflicts ?? []
  const conflictIds = new Set(conflicts.flatMap((c) => c.record_ids))
  const hardCount = conflicts.filter((c) => c.hard).length

  /** 模型被允许说"这次不用打扰"（`decision_type = no_action`）。
   *
   * 这种返回后端照常落库、照常广播，但 `InterventionCard` 刻意不渲染卡片 ——
   * 否则等于替模型撤销它自己的判断。代价是：点一次「问决策器」，等 9 秒，
   * 画面纹丝不动（连 hint 都不会出现，因为它是"送达了"，不是"被拦下"）。
   * 沉默本身是对的，但**沉默必须被看见**，否则用户只会以为程序坏了。
   * 这里只转述模型的原话，不替它做任何决定，也不给它任何按钮。 */
  const silent =
    decision && decision.delivered !== false && decision.payload?.decision_type === 'no_action'
      ? decision.payload.summary || '模型判断本轮不需要打扰'
      : null

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>SecondWill · 第二意志</h1>
          <p className="subtitle">AI 执行教练 · v1.0 最小闭环 · P5 节奏控制与反馈回流</p>
        </div>
        <div className="header-actions">
          <span
            className={`conn conn-${channel}`}
            title={
              channel === 'live'
                ? '实时通道已连接：任一标签页改动都会立刻同步到这里'
                : channel === 'polling'
                  ? '实时通道中断，已降级为轮询（每 5 秒刷新），恢复后会自动切回'
                  : '正在建立实时通道…'
            }
          >
            {channel === 'live' ? '● 实时' : channel === 'polling' ? '◌ 轮询' : '… 连接中'}
          </span>
          <button
            className="btn small"
            onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
            title="切换深色 / 浅色"
          >
            {theme === 'dark' ? '☾ 深色' : '☀ 浅色'}
          </button>
          <a className="btn small" href={exportCsvUrl}>
            导出 CSV
          </a>
          <button
            className="btn small"
            onClick={onAskDecision}
            disabled={asking}
            title="手动问一次决策器。模型往返约需 10 秒，期间按钮会显示「问决策器中…」"
          >
            {asking ? '问决策器中…' : '问决策器'}
          </button>
          <button className="btn small" onClick={() => void reload()} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </button>
        </div>
      </header>

      {error && (
        <div className="banner-error">
          后端连接失败：{error}
          <div className="banner-hint">
            后端未启动？在项目根目录执行{' '}
            <code>uvicorn app.main:app --host 127.0.0.1 --port 8000</code>
          </div>
        </div>
      )}

      {/* 双栏：左侧是「日程台账」，右侧是「执行教练舞台」。
          宠物与决策卡片同居右栏，是为了让"猫在说话"这件事在视觉上成立 ——
          决策不再是一张飘在页面里的卡片，而是这只猫给出的判断。 */}
      <div className="stage">
        <main className="col-main">
          {/* 录入区合成一块"控制台"玻璃卡：一句话解析与手动表单放在同一个容器里，
              否则两排裸控件直接铺在背景上，页面上半截会显得没有骨架。 */}
          <section className="compose">
            <QuickAdd onCreate={onCreate} />
            <RecordForm onCreate={onCreate} />
          </section>
          <div className="col-head">
            <h2>今日日程</h2>
            <span className="col-head-count">{records.length} 条</span>
          </div>
          <RecordList
            records={records}
            conflictIds={conflictIds}
            onUpdate={onUpdate}
            onComplete={onComplete}
            onRemove={onRemove}
            hit={streamHit}
          />
          {/* 把冲突一并传下去：时间轴要画的不只是"排了什么"，
              更是"哪里撞上了"。冲突本身只给 record_ids + 分钟数，
              时间段由组件自己反算 —— 不新增请求。 */}
          <DayTimeline records={records} conflicts={conflicts} />
        </main>

        <aside className="col-side">
          <PetPanel state={state} thinking={asking} pulse={petPulse} onPulse={emitPulse} />

          {hint && (
            <div className="banner-info" role="status">
              暂时没有新建议：{hint}
            </div>
          )}

          {silent && (
            <div className="banner-info" role="status">
              <div className="banner-row">
                <span>
                  本轮决策：<strong>不打扰</strong>
                </span>
                <button
                  className="btn small ghost"
                  onClick={() => setDecision(null)}
                  title="关掉这条提示（不会改变任何数据）"
                >
                  关闭
                </button>
              </div>
              <div className="banner-hint">{silent}</div>
            </div>
          )}

          {/* 等待期间不渲染上一轮的卡片：它马上会被替换，
              留在原地既抢注意力，又把右栏顶出视口。去掉之后宠物
              会自动恢复成大尺寸并亮起思考环，转场就是"猫抬起头开始想"。 */}
          {!asking && (
            <InterventionCard
              decision={revealedDecision}
              records={records}
              onApply={onApplySuggestion}
              onDismiss={() => {
                setRevealedDecision(null)
                setDecision(null)
              }}
              onFeedback={(kind) => {
                // 反馈已经写进事件流与反馈画像，下一轮决策会读到。
                // 这里只做一件事：让用户知道系统记下了。
                if (kind === 'mute') setHint('已记下：这类建议被忽略够次数后将不再出现')
                else setHint(null)
              }}
            />
          )}

          {hardCount > 0 && (
            <div className="banner-warn">
              检测到 {hardCount} 处时间重叠。日程暂时还能保存，决策器已在后台给出重排建议。
            </div>
          )}

          <ConflictList conflicts={conflicts} records={records} />
        </aside>
      </div>

      {/* 页脚：把"数据在哪、跑的是什么"写在明面上。
          它同时承担一个版式职责 —— 让页面底部有收口，而不是内容说完就断在空白里。 */}
      <footer className="app-footer">
        <span className="foot-item">
          <i className={`foot-dot foot-${channel}`} />
          {channel === 'live' ? '实时通道已连接' : channel === 'polling' ? '轮询降级中' : '连接中'}
        </span>
        <span className="foot-item">DeepSeek · 决策契约 v1.0</span>
        <span className="foot-item">本地 SQLite · 日程不出这台机器</span>
        <span className="foot-grow" />
        <span className="foot-item dim">SecondWill v1.0</span>
      </footer>

      {/* 光流覆盖层：flight 为 null 时组件直接返回 null，DOM 上什么都不留。
          它挂在最外层，坐标才能用视口绝对定位，不被任何栏的溢出裁掉。 */}
      <InterventionStream flight={flight} onArrive={arrive} />
    </div>
  )
}
