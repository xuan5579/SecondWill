/** 主题切换。

v1.0 只做深色 / 浅色两套，偏好存 localStorage。
样式本身是 index.css 里的 CSS 变量，这里只负责在 <html> 上打 data-theme。
*/

export type Theme = 'dark' | 'light'

const KEY = 'dpa.theme'

export function readTheme(): Theme {
  const saved = localStorage.getItem(KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return 'dark' // 沿用桌面版的深色基调
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme
  localStorage.setItem(KEY, theme)
}
