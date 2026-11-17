/** 宠物脉冲 —— 用户操作 → 光核的即时反应（A 层交互）。
 *
 * 这是一个**纯视觉信号**：它不落库、不进后端、不影响任何判定，
 * 只是让"我完成了任务 / 我改了日程 / 我点了它"这三个瞬间
 * 在光核上被看见。宠物因此从"仪表盘"变成"会回应的实体"。
 */

export type PetPulseKind =
  /** 完成了一个任务 —— 心跳式双脉冲 + 胡须数据线外喷 */
  | 'task_done'
  /** 新建了一条日程 —— 波形线抖动，像信号被扰动 */
  | 'record_added'
  /** 改动/采纳了日程时间 —— 与新建共用"扰动"反应 */
  | 'record_moved'
  /** 用户点了一下光核（A2 的涟漪回应） */
  | 'tap'

export interface PetPulse {
  kind: PetPulseKind
  /** 单调递增序号：靠它触发 React key 换新，动画才会重播 */
  id: number
}

/** 档位切换过渡时长（rise / settle 扫光）。
 *
 * 提到这里是因为「干预光流」必须等它播完才能出发 ——
 * 光流组件与 CatAvatar 读同一个常量，时序才不会各写一个魔数后走偏。 */
export const MOOD_SHIFT_MS = 1300

/** 六档的"能量序"。档位切换动画（A3）靠它判断方向：
 * 升档 = 光从底部涌上来（rise），降档 = 光缓缓沉下去（settle）。 */
export const MOOD_RANK: Record<string, number> = {
  dormant: 0,
  guilty: 1,
  tired: 2,
  normal: 3,
  great: 4,
  alert: 5,
}
