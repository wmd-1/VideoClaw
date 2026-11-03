/** 正文来源/失效原因的共享文案（视频徽标与改写阶段“需重跑”共用）。 */
export const PROMPT_STALE_LABELS: Record<string, string> = {
  shot_changed: '镜头内容变化',
  duration_changed: '时长变化',
  mode_changed: '模式变化',
  references_changed: '参考图变化',
  style_changed: '风格变化',
  facts_changed: '角色/场景设定变化',
  grounding_model_changed: '识别模型变化',
  fingerprint_missing: '缺少输入记录',
  fingerprint_changed: '输入已变化',
  h3_not_ready: '上次改写未完成',
  h3_absent: '未执行改写',
};

/** 徽标只展示前 2 条 + “等”，完整列表用于 title。 */
export function formatStaleReasons(reasons?: string[]): { short: string; full: string } {
  const labels = (reasons || []).map(reason => PROMPT_STALE_LABELS[reason] || reason);
  const short = labels.length > 2 ? `${labels.slice(0, 2).join('、')}等` : labels.join('、');
  return { short, full: labels.join('、') };
}
