/**
 * 列表型能力参数（tasks）的共享口径（openspec: review-fifth-minor-followups M-1）。
 * 与后端 models/custom_common.parse_task_list 使用同一分隔符集合，避免
 * “前端校验通过、后端解析为空”的偏差。
 */
export const TASK_SEPARATORS = /[,，|、]+/;

/** 视频模型 tasks 合法值（与后端 _supported_tasks 的过滤口径一致）。 */
export const VIDEO_TASK_VALUES = ['t2va', 'fl2va', 'ref2va'] as const;

export type VideoTaskValue = (typeof VIDEO_TASK_VALUES)[number];

/** 按共享分隔符拆分：去空白、转小写、去重、保序。 */
export function splitTaskTokens(raw: string | null | undefined): string[] {
  const seen = new Set<string>();
  return String(raw ?? '')
    .split(TASK_SEPARATORS)
    .map(token => token.trim().toLowerCase())
    .filter(token => {
      if (!token || seen.has(token)) return false;
      seen.add(token);
      return true;
    });
}

/** 返回不在合法枚举内的 token（去重、保序）。 */
export function invalidTaskTokens(raw: string | null | undefined): string[] {
  const allowed = new Set<string>(VIDEO_TASK_VALUES);
  return Array.from(new Set(splitTaskTokens(raw).filter(token => !allowed.has(token))));
}
