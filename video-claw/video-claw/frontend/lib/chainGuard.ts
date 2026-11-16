/**
 * 执行链推进判定（纯函数，便于离线/容器外复核）。
 *
 * 语义：阶段是否已落到**正常的链停点**——即"本阶段完成"或"停在停点等待用户确认"。
 * 注意：`waiting` 返回 true **不代表自动推进**；两条多阶段链在 `waiting` 分支都是
 * "停下等用户确认"，只有 `completed/session_completed` 在自动模式下才会 continue。
 * 两者都不满足（running/stopped/error/pending/reconfirm/未知）时必须终止链：
 *  - 仍在跑时推进 → 同一会话出现两条并行执行链；
 *  - 停止/失败/未确认时推进 → 等于跳过用户意图。
 */
export function isStageSettled(status: string | null | undefined): boolean {
  return status === 'completed' || status === 'session_completed' || status === 'waiting';
}

/** 阶段是否仍在执行（决定"转为轮询继续跟踪"而不是终止）。 */
export function isStageStillRunning(status: string | null | undefined): boolean {
  return status === 'running';
}
