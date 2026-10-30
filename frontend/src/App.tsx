import { useEffect, useState } from 'react'
import ConflictList from './components/ConflictList'
import InterventionCard from './components/InterventionCard'
import PetPanel from './components/PetPanel'
import QuickAdd from './components/QuickAdd'
import RecordForm from './components/RecordForm'
import RecordList, { type RecordDraft } from './components/RecordList'
import { exportCsvUrl } from './lib/api'
import { api, type RecordInput } from './lib/api'
import type { RecordItem } from './types/api'
import { applyTheme, readTheme, type Theme } from './lib/theme'
import { useAppData } from './lib/useAppData'

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
    error,
    loading,
    channel,
    decision,
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

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  const onCreate = (input: RecordInput) => run(() => api.createRecord(input).then(() => undefined))

  const onUpdate = (id: string, draft: RecordDraft) =>
    run(async () => {
      await api.updateRecord(id, draft)
      // 状态变为 done 时补一条行为事件 —— 这是 LLM 唯一的输入来源
      if (draft.status === 'done') {
        await api.reportEvents([{ kind: 'task_completed', record_id: id }])
      }
    })

  const onComplete = (item: RecordItem) =>
    run(async () => {
      await api.updateRecord(item.id, { status: 'done' })
      await api.reportEvents([{ kind: 'task_completed', record_id: item.id }])
    })

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
      } else {
        setHint(null)
        setDecision(res)
      }
    }).finally(() => setAsking(false))
  }

  /** 采用建议：直接改日程，走与手动编辑完全相同的链路 —— 不另起一条。 */
  const onApplySuggestion = (recordId: string, start: string, end: string) =>
    run(() => api.updateRecord(recordId, { start_at: start, end_at: end }).then(() => undefined))

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
            title="手动问一次决策器。模型往返约需 9 秒，期间按钮会显示「问决策器中…」"
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

      <PetPanel state={state} />

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

      <InterventionCard
        decision={decision}
        records={records}
        onApply={onApplySuggestion}
        onDismiss={() => setDecision(null)}
        onFeedback={(kind) => {
          // 反馈已经写进事件流与反馈画像，下一轮决策会读到。
          // 这里只做一件事：让用户知道系统记下了。
          if (kind === 'mute') setHint('已记下：这类建议被忽略够次数后将不再出现')
          else setHint(null)
        }}
      />

      {hardCount > 0 && (
        <div className="banner-warn">
          检测到 {hardCount} 处时间重叠。日程暂时还能保存，决策器已在后台给出重排建议。
        </div>
      )}

      <ConflictList conflicts={conflicts} records={records} />

      <QuickAdd onCreate={onCreate} />

      <RecordForm onCreate={onCreate} />

      <RecordList
        records={records}
        conflictIds={conflictIds}
        onUpdate={onUpdate}
        onComplete={onComplete}
        onRemove={onRemove}
      />
    </div>
  )
}
