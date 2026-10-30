import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/** 最后一道防线：把渲染错误关在局部，别让它掀翻整页。
 *
 * 为什么需要它：React 里任何一处渲染抛错且无人捕获，整棵树会被卸载，
 * 屏幕上只剩 body 的底色 —— 深色主题下就是一片纯黑，既看不到原因也点不动，
 * 演示时这是最糟糕的一种失败方式（观众只会认为"程序崩了"）。
 *
 * 它不修复错误，只做三件事：说清出了什么事、保留一个可复位的出口、
 * 把堆栈打进控制台供排查。
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 保留组件堆栈：定位"是哪个组件炸的"比"炸在哪一行"更省时间
    console.error('[SecondWill] 界面渲染出错：', error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="app">
        <div className="banner-error" role="alert">
          界面出错了：{error.message}
          <div className="banner-hint">
            已阻止它扩散到整页。可以先重试，详细堆栈已打印到浏览器控制台（F12）。
          </div>
          <button className="btn small" onClick={() => this.setState({ error: null })}>
            重试
          </button>
        </div>
      </div>
    )
  }
}
