'use client';

import React from 'react';

/** 后端已收敛的白名单评估字段（Q9），UI 不读取 issues/suggestion 原文。 */
export interface AssetEvaluation {
  evaluated?: boolean | null;
  eval_status?: string;
  score?: number;
  selection?: {
    source?: string;
    candidates?: number;
    index?: number;
  };
}

export type EvaluationState = 'missing' | 'evaluated' | 'unavailable' | 'parse_failed' | 'unknown';

export interface EvaluationView {
  state: EvaluationState;
  text: string;
  className: string;
}

/**
 * 四态口径：
 * - 整个 evaluation 缺失（旧产物）→ 未评估
 * - evaluated=false + unavailable → 评估不可用
 * - evaluated=false + parse_failed → 解析失败
 * - evaluated=true → 已评估（有 score 才显示分数）
 */
export function evaluationView(evaluation?: AssetEvaluation | null): EvaluationView {
  if (!evaluation || typeof evaluation !== 'object') {
    return { state: 'missing', text: '未评估', className: 'bg-gray-100 text-gray-500' };
  }
  if (evaluation.evaluated === true) {
    const score = typeof evaluation.score === 'number' ? evaluation.score : undefined;
    return {
      state: 'evaluated',
      text: score === undefined ? '已评估' : `已评估 ${score}分`,
      className: 'bg-emerald-50 text-emerald-700',
    };
  }
  if (evaluation.eval_status === 'unavailable') {
    return { state: 'unavailable', text: '评估不可用', className: 'bg-amber-50 text-amber-700' };
  }
  if (evaluation.eval_status === 'parse_failed') {
    return { state: 'parse_failed', text: '解析失败', className: 'bg-amber-50 text-amber-700' };
  }
  if (evaluation.evaluated === null || evaluation.evaluated === undefined) {
    // 旧形态：有评估字段但缺 evaluated（含未写键的 undefined），与“整体缺键”分开展示
    return { state: 'unknown', text: '未评估（旧记录）', className: 'bg-gray-100 text-gray-500' };
  }
  return { state: 'missing', text: '未评估', className: 'bg-gray-100 text-gray-500' };
}

/** 只展示候选数量，不暴露内部 index。 */
export function selectionText(evaluation?: AssetEvaluation | null): string {
  const candidates = evaluation?.selection?.candidates;
  if (typeof candidates !== 'number' || candidates <= 0) return '';
  return `自动选优（候选 ${candidates} 个）`;
}

export default function AssetEvaluationBadge({ evaluation }: { evaluation?: AssetEvaluation | null }) {
  const view = evaluationView(evaluation);
  const selection = selectionText(evaluation);
  return (
    <span className="inline-flex items-center gap-1 shrink-0">
      <span className={`text-[10px] px-1.5 py-0.5 rounded ${view.className}`}>{view.text}</span>
      {selection ? <span className="text-[10px] text-gray-400 whitespace-nowrap">{selection}</span> : null}
    </span>
  );
}
