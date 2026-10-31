'use client';

import { AlertTriangle } from 'lucide-react';
import type { MissingInput } from '@/lib/workflowApi';
import { STAGES } from '../TopBar';
import type { FlaggedItem } from './types';

function stageName(stageId: string): string {
  return STAGES.find(stage => stage.id === stageId)?.name || stageId;
}

/**
 * 阶段因必需输入不可用而失败：列出缺哪一项、给哪个片段用，并按来源阶段提供跳转。
 * 只做定位与导航，不自动替换版本或重新执行。
 */
export function MissingInputsNotice({
  items,
  onNavigateStage,
}: {
  items: MissingInput[];
  onNavigateStage?: (stage: string) => void;
}) {
  const sources = Array.from(new Set(items.map(item => item.source_stage).filter(stage => stage !== 'input')));
  return (
    <div data-testid="missing-inputs-notice" className="mx-4 mt-4 rounded-xl border border-red-300 bg-red-50 p-4 space-y-2">
      <div className="flex items-center gap-2 text-sm font-medium text-red-800">
        <AlertTriangle className="w-4 h-4" />
        无法开始：以下必需的输入不可用（已在对应条目上标红）
      </div>
      <ul className="space-y-1 text-xs text-red-700">
        {items.map(item => (
          <li key={`${item.source_stage}:${item.container}:${item.item_id}:${item.field}`}>
            · {stageName(item.source_stage)}：{item.message}
          </li>
        ))}
      </ul>
      {onNavigateStage && sources.length > 0 && (
        <div className="flex flex-wrap gap-2 pt-1">
          {sources.map(stage => (
            <button
              key={stage}
              type="button"
              onClick={() => onNavigateStage(stage)}
              className="px-3 py-1.5 rounded-lg bg-white text-red-700 ring-1 ring-red-300 text-xs font-medium hover:bg-red-100 transition-colors"
            >
              前往「{stageName(stage)}」重新选择 / 重新生成
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** 条目卡片上方的缺失说明；来源在别的阶段时给出跳转。 */
export function FlaggedItemBanner({
  flag,
  currentStage,
  onNavigateStage,
}: {
  flag: FlaggedItem;
  currentStage: string;
  onNavigateStage?: (stage: string) => void;
}) {
  return (
    <div data-testid="flagged-item" className="mb-2 px-3 py-2 bg-red-50 border border-red-200 rounded-lg text-xs text-red-700 flex flex-wrap items-center gap-2">
      <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
      <span className="flex-1 min-w-0 break-all">{flag.message}</span>
      {flag.sourceStage !== currentStage && onNavigateStage ? (
        <button
          type="button"
          onClick={() => onNavigateStage(flag.sourceStage)}
          className="shrink-0 px-2 py-1 rounded-md bg-white ring-1 ring-red-300 font-medium hover:bg-red-100 transition-colors"
        >
          前往「{stageName(flag.sourceStage)}」处理
        </button>
      ) : (
        <span className="shrink-0 text-red-600">请在本卡片重新选择版本、上传或重新生成</span>
      )}
    </div>
  );
}

/** 失败阶段的缺失清单 → 各阶段需要标红的条目（来源条目 + 使用它的片段）。 */
export function flagsFromMissingInputs(
  states: Record<string, { status: string; missingInputs?: MissingInput[] | null }>,
): Record<string, Record<string, FlaggedItem>> {
  const flags: Record<string, Record<string, FlaggedItem>> = {};
  const put = (stage: string, id: string, flag: FlaggedItem) => {
    if (!stage || !id) return;
    flags[stage] = flags[stage] || {};
    const existing = flags[stage][id];
    flags[stage][id] = existing ? { ...existing, message: `${existing.message}；${flag.message}` } : flag;
  };
  for (const [stageId, state] of Object.entries(states)) {
    if (state.status !== 'error' || !state.missingInputs?.length) continue;
    for (const item of state.missingInputs) {
      if (item.source_stage === 'input') continue;
      put(item.source_stage, item.item_id, {
        message: `「${stageName(stageId)}」需要：${item.message}`,
        sourceStage: item.source_stage,
      });
      for (const consumer of item.consumers || []) {
        if (stageId === item.source_stage && consumer === item.item_id) continue;
        put(stageId, consumer, { message: item.message, sourceStage: item.source_stage });
      }
    }
  }
  return flags;
}
