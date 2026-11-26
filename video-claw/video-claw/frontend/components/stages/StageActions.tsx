'use client';

import React, { useState, useCallback } from 'react';
import { CheckCircle, Edit3, Save, RefreshCw, Check, Loader2, Play } from 'lucide-react';
import type { StageStatus } from './types';

// 需要"继续生成"的阶段（2、3、4、5）
const STAGES_WITH_CONTINUE = ['character_design', 'storyboard', 'reference_generation', 'prompt_rewrite', 'video_generation'];
// 需要检查后续阶段的阶段（1）
const STAGES_WITH_NEXT_CHECK = ['script_generation'];

interface StageActionsProps {
  status: StageStatus;
  onConfirm: (options?: { allowFailedItems?: boolean }) => void;
  onEdit?: () => void;
  onSave?: () => void;
  onSaveSelections?: () => Promise<void>;
  onExecute?: () => void;
  onRegenerateStage?: () => void;
  /** 仅控制阶段级重做按钮；开始/重试/恢复由 onExecute 独立控制。 */
  allowRegenerate?: boolean;
  /** 阶段ID */
  stageId?: string;
  /** 是否有待生成的项（用于阶段2、3、4、5） */
  hasPendingItems?: boolean;
  /** 后续阶段是否已开始（用于阶段1） */
  hasNextStageStarted?: boolean;
  /** 是否显示"确认并继续"按钮（后续阶段已执行过时隐藏） */
  showConfirm?: boolean;
  /**
   * 后端阶段状态为 reconfirm：产物已完成但上游输入已变。
   * 本地状态仍按 waiting 呈现，这里只用于把两种动作的文案区分开。
   */
  reconfirm?: boolean;
  isRunning: boolean;
  confirmDisabled?: boolean;
  confirmDisabledReason?: string;
  executeDisabled?: boolean;
  executeDisabledReason?: string;
  regenerateDisabled?: boolean;
  regenerateDisabledReason?: string;
  confirmLabel?: string;
}

export default function StageActions({
  status,
  onConfirm,
  onEdit,
  onSave,
  onSaveSelections,
  onExecute,
  onRegenerateStage,
  allowRegenerate = true,
  stageId = '',
  hasPendingItems = true,
  hasNextStageStarted = false,
  showConfirm = true,
  reconfirm = false,
  isRunning,
  confirmDisabled = false,
  confirmDisabledReason = '',
  executeDisabled = false,
  executeDisabledReason = '',
  regenerateDisabled = false,
  regenerateDisabledReason = '',
  confirmLabel = '确认并继续',
}: StageActionsProps) {
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved'>('idle');
  const [showRegenerateConfirm, setShowRegenerateConfirm] = useState(false);

  // 是否是"继续生成"阶段（2、3、4、5）
  const isContinueStage = STAGES_WITH_CONTINUE.includes(stageId);
  // 是否是"重新生成"阶段（1）
  const isRegenStage = STAGES_WITH_NEXT_CHECK.includes(stageId);

  // 执行/恢复只在尚未完成或仍有待处理项时出现；阶段级重做独立显示。
  const showExecute = !!onExecute && status !== 'running' && (
    status !== 'completed' || hasPendingItems
  );
  const showRegenerate = allowRegenerate && !!onRegenerateStage && status !== 'running' && status !== 'pending';
  const isExecuteDisabled = isRunning || executeDisabled;
  const isRegenerateDisabled = isRunning || regenerateDisabled || (isRegenStage && hasNextStageStarted);

  // 其余按钮在 waiting、running、completed、stopped 状态显示
  const showActions = status === 'waiting' || status === 'running' || status === 'completed' || status === 'stopped';
  // 保存选项：在 waiting、completed 和 stopped 状态下都显示
  const showSaveSelections = onSaveSelections && (status === 'waiting' || status === 'completed' || status === 'stopped');
  // "确认并继续"在 showConfirm=true 且 waiting/running/completed 时显示（stopped 状态不显示确认）
  const showConfirmBtn = showConfirm && (status === 'waiting' || status === 'running' || status === 'completed');

  const handleSaveClick = useCallback(async () => {
    if (!onSaveSelections || saveState !== 'idle') return;
    setSaveState('saving');
    try {
      await onSaveSelections();
      setSaveState('saved');
      setTimeout(() => setSaveState('idle'), 1500);
    } catch {
      setSaveState('idle');
    }
  }, [onSaveSelections, saveState]);

  if (!showExecute && !showRegenerate && !showActions && !showSaveSelections) return null;

  return (
    <div className="border-t border-gray-200 bg-white px-4 sm:px-6 py-4 flex flex-wrap items-center justify-between gap-3 flex-shrink-0">
      {/* 左侧：重新生成/继续生成按钮（+ 输入已变更的提示） */}
      <div className="flex flex-wrap items-center gap-3">
        {(showExecute || showRegenerate) && reconfirm && (
          <span className="text-xs text-amber-700" title="上游输入已变更：确认=接受现有结果并继续；继续生成=补缺失项；重新生成=重做本阶段并清理下游">
            上游输入已变更，本阶段结果待重新确认
          </span>
        )}
        {showExecute && (
          <button
            onClick={onExecute}
            disabled={isExecuteDisabled}
            className="flex items-center gap-2 px-4 py-2 bg-white border border-orange-300 text-orange-600 rounded-lg text-sm font-medium hover:bg-orange-50 transition-colors disabled:border-gray-200 disabled:bg-gray-100 disabled:text-gray-400 disabled:opacity-100 disabled:cursor-not-allowed disabled:hover:bg-gray-100 disabled:hover:text-gray-400"
            title={isExecuteDisabled ? executeDisabledReason : undefined}
          >
            <Play className="w-4 h-4" />
            {status === 'error' ? '重试' : status === 'stopped' || isContinueStage ? '继续生成' : '开始生成'}
          </button>
        )}
        {showRegenerate && (
          <button
            onClick={() => setShowRegenerateConfirm(true)}
            disabled={isRegenerateDisabled}
            className="flex items-center gap-2 px-4 py-2 bg-white border border-gray-200 text-gray-700 rounded-lg text-sm font-medium hover:bg-gray-50 transition-colors disabled:border-gray-200 disabled:bg-gray-100 disabled:text-gray-400 disabled:opacity-100 disabled:cursor-not-allowed disabled:hover:bg-gray-100 disabled:hover:text-gray-400"
            title={isRegenerateDisabled ? (regenerateDisabledReason || (isRegenStage && hasNextStageStarted ? '后续阶段已开始，无法重新生成' : '')) : '重做本阶段并清理下游产物'}
          >
            <RefreshCw className="w-4 h-4" />
            重新生成
          </button>
        )}
      </div>

      {/* 右侧：操作按钮组 */}
      <div className="flex flex-wrap items-center justify-end gap-3">
        {showActions && onEdit && (
          <button
            onClick={onEdit}
            disabled={isRunning}
            className="flex items-center gap-2 px-4 py-2 bg-white border border-gray-200 text-gray-700 rounded-lg text-sm font-medium hover:bg-gray-50 transition-colors disabled:border-gray-200 disabled:bg-gray-100 disabled:text-gray-400 disabled:opacity-100 disabled:cursor-not-allowed disabled:hover:bg-gray-100 disabled:hover:text-gray-400"
          >
            <Edit3 className="w-4 h-4" />
            修改
          </button>
        )}
        
        {showActions && onSave && (
          <button
            onClick={onSave}
            disabled={isRunning}
            className="flex items-center gap-2 px-4 py-2 bg-white border border-blue-300 text-blue-600 rounded-lg text-sm font-medium hover:bg-blue-50 transition-colors disabled:border-gray-200 disabled:bg-gray-100 disabled:text-gray-400 disabled:opacity-100 disabled:cursor-not-allowed disabled:hover:bg-gray-100 disabled:hover:text-gray-400"
          >
            <Save className="w-4 h-4" />
            保存
          </button>
        )}

        {showSaveSelections && (
          <button
            onClick={handleSaveClick}
            disabled={saveState !== 'idle' || isRunning}
            className="flex items-center gap-2 px-4 py-2 bg-white border border-blue-600 text-blue-600 rounded-lg text-sm font-medium hover:bg-blue-50 transition-colors disabled:border-gray-200 disabled:bg-gray-100 disabled:text-gray-400 disabled:opacity-100 disabled:cursor-not-allowed disabled:hover:bg-gray-100 disabled:hover:text-gray-400 min-w-[120px] justify-center"
          >
            {saveState === 'saving' ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : saveState === 'saved' ? (
              <Check className="w-4 h-4 text-green-600" />
            ) : (
              <Save className="w-4 h-4" />
            )}
            {saveState === 'saving' ? '正在保存...' : saveState === 'saved' ? '保存成功' : '保存选择'}
          </button>
        )}
        
        {showConfirmBtn && (
          <button
            onClick={() => onConfirm()}
            disabled={isRunning || confirmDisabled}
            title={confirmDisabled ? confirmDisabledReason : (reconfirm ? '确认 = 接受本阶段现有结果并继续（不重新生成）；要按新输入重做请点「继续生成」' : undefined)}
            className="flex items-center gap-2 px-6 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors disabled:bg-gray-200 disabled:text-gray-400 disabled:opacity-100 disabled:cursor-not-allowed disabled:hover:bg-gray-200 disabled:hover:text-gray-400"
          >
            <CheckCircle className="w-4 h-4" />
            {confirmLabel}
          </button>
        )}
      </div>
      {showRegenerateConfirm && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 backdrop-blur-sm">
          <div className="w-[min(520px,calc(100vw-2rem))] rounded-2xl bg-white p-6 shadow-2xl">
            <h3 className="text-lg font-bold text-gray-800 mb-3">确认重做本阶段？</h3>
            <p className="text-sm text-gray-600 leading-relaxed">
              将清空本阶段及下游阶段的产物引用和执行记录；已生成的媒体文件会保留，但需要重新生成下游产物。
            </p>
            <div className="mt-6 flex justify-end gap-3">
              <button
                onClick={() => setShowRegenerateConfirm(false)}
                className="px-5 py-2.5 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-xl text-sm font-bold"
              >
                取消
              </button>
              <button
                onClick={() => {
                  setShowRegenerateConfirm(false);
                  onRegenerateStage?.();
                }}
                className="px-6 py-2.5 bg-orange-600 text-white rounded-xl text-sm font-bold hover:bg-orange-700"
              >
                确认重做
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
